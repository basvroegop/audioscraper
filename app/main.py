import json
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, meetings, phone_context, pipeline
from .locations import classify_location, matches_region
from .netnumbers import net_area
from .sources import add_video, canonical_youtube, fetch_public, public_url, youtube_thumbnail


@asynccontextmanager
async def lifespan(app):
    db.init()
    pipeline.start()
    yield
    pipeline.shutdown()

app = FastAPI(title='Audioscraper', lifespan=lifespan)
STATIC = Path(__file__).parent / 'static'


@app.middleware('http')
async def security_headers(request: Request, call_next):
    if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
        origin = request.headers.get('origin')
        if origin and urlsplit(origin).netloc != request.headers.get('host'):
            return JSONResponse({'detail': 'Aanvraag vanaf een andere website geweigerd.'}, status_code=403)
        if request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'detail': 'Aanvraag vanaf een andere website geweigerd.'}, status_code=403)
    response = await call_next(request)
    # Authenticated audio/exports must never be cached by a shared proxy/CDN.
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['Vary'] = 'Cookie'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    return response


class AddSource(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    kind: str = 'auto'
    initial_limit: int = Field(default=3, ge=0, le=5000)
    name: str = Field(default='', max_length=120)
    description: str = Field(default='', max_length=400)


class ToggleSource(BaseModel):
    enabled: bool


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=400)
    accent: str | None = Field(default=None, max_length=20)
    enabled: bool | None = None


class ProjectOrder(BaseModel):
    ids: list[str] = Field(max_length=500)


class ProcessingControl(BaseModel):
    paused: bool


class SpeakerName(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class PhoneAttributionUpdate(BaseModel):
    state: str = Field(min_length=1, max_length=20)
    subject_type: str = Field(default='unknown', max_length=40)
    relation: str = Field(default='unclear', max_length=40)
    label: str = Field(default='', max_length=160)
    description: str = Field(default='', max_length=300)
    episode_id: str | None = Field(default=None, max_length=80)
    segment: int | None = Field(default=None, ge=0)


def worker_json(method, path, **kwargs):
    try:
        response = httpx.request(
            method, pipeline.WORKER + path,
            headers={'Authorization': 'Bearer ' + pipeline.TOKEN}, timeout=30,
            **kwargs,
        )
        if response.status_code == 404:
            raise HTTPException(404, 'Stemprofiel niet gevonden.')
        if response.status_code >= 400:
            try:
                detail = response.json().get('detail')
            except Exception:
                detail = None
            raise HTTPException(response.status_code, detail or 'De stemworker weigerde de aanvraag.')
        return response.json()
    except HTTPException:
        raise
    except httpx.HTTPError as error:
        raise HTTPException(503, 'De stemworker is tijdelijk niet bereikbaar.') from error


def speaker_catalog(profiles, project=''):
    """Join worker voice profiles to transcript segments and spoken entities."""
    profile_map = {profile['profile_id']: profile for profile in profiles}
    speakers = {}
    scope, scope_args = project_filter(project, 'source_id')
    for episode in db.rows("SELECT id,title,segments,entities FROM episodes "
                           "WHERE segments LIKE '%speaker_id%'" + scope, scope_args):
        segments = json.loads(episode['segments'] or '[]')
        entities = json.loads(episode['entities'] or '[]')
        analysis_pending = episode['entities'] is None
        segment_profiles = {}
        for index, segment in enumerate(segments):
            profile_id = segment.get('speaker_id')
            if not profile_id:
                continue
            segment_profiles[index] = profile_id
            target = speakers.setdefault(profile_id, {
                'profile_id': profile_id, 'stored_name': segment.get('speaker') or 'Onbekende stem',
                'segments': 0, 'seconds': 0.0, 'episode_ids': set(),
                'pending_episode_ids': set(), 'entities': {},
            })
            target['segments'] += 1
            target['seconds'] += max(0, float(segment.get('end') or 0) - float(segment.get('start') or 0))
            target['episode_ids'].add(episode['id'])
            if analysis_pending:
                target['pending_episode_ids'].add(episode['id'])
        for entity in entities:
            for occurrence in entity.get('occurrences', []):
                indexes = {span.get('segment') for span in occurrence.get('spans', [occurrence])}
                profile_ids = {segment_profiles.get(index) for index in indexes}
                for profile_id in profile_ids - {None}:
                    key = (entity.get('name', ''), entity.get('type', 'other'))
                    if not key[0]:
                        continue
                    value = speakers[profile_id]['entities'].setdefault(key, {
                        'name': key[0], 'type': key[1], 'count': 0,
                    })
                    value['count'] += 1
    result = []
    for profile_id, speaker in speakers.items():
        profile = profile_map.get(profile_id, {})
        result.append({
            'profile_id': profile_id,
            'name': profile.get('name') or '',
            'display_name': profile.get('display_name') or speaker['stored_name'],
            'suggested_name': profile.get('suggested_name') or '',
            'sample_available': bool(profile.get('sample_available')),
            'segments': speaker['segments'],
            'seconds': round(speaker['seconds'], 1),
            'episodes': len(speaker['episode_ids']),
            'analysis_pending': len(speaker['pending_episode_ids']),
            'entities': sorted(speaker['entities'].values(), key=lambda value: (-value['count'], value['name'].casefold())),
        })
    return sorted(result, key=lambda speaker: (not bool(speaker['name']), speaker['display_name'].casefold()))


# Every source is exactly one project: its own environment with the episodes,
# transcripts, entities, voices and phone numbers discovered inside it.
PROJECT_FIELDS = ('id', 'url', 'kind', 'title', 'name', 'description', 'accent',
                  'enabled', 'initial_limit', 'initialized', 'created', 'checked',
                  'error', 'syncing', 'priority')


def project_or_404(ident):
    project = db.one('SELECT * FROM sources WHERE id=?', (ident,))
    if not project:
        raise HTTPException(404, 'Project niet gevonden.')
    return project


def project_filter(project, column='source_id'):
    """Limit a query to one project, or to every project when unset."""
    if not project:
        return '', ()
    project_or_404(project)
    return f' AND {column}=?', (project,)


def project_metrics():
    """Per-project counters in grouped queries instead of one query per card."""
    metrics = {}

    def slot(source_id):
        return metrics.setdefault(source_id, {
            'episode_count': 0, 'completed': 0, 'active': 0, 'failed': 0,
            'transcripts': 0, 'entities': 0, 'phone_numbers': 0, 'updated': None,
        })

    for row in db.rows("""SELECT source_id,status,COUNT(*) n,MAX(updated) updated,
        SUM(transcript IS NOT NULL) transcripts FROM episodes GROUP BY source_id,status"""):
        target = slot(row['source_id'])
        target['episode_count'] += row['n']
        target['transcripts'] += row['transcripts'] or 0
        if row['status'] == 'completed':
            target['completed'] += row['n']
        elif row['status'] == 'failed':
            target['failed'] += row['n']
        else:
            target['active'] += row['n']
        if row['updated'] and (target['updated'] is None or row['updated'] > target['updated']):
            target['updated'] = row['updated']
    names = {}
    for row in db.rows('SELECT source_id,entities FROM episodes WHERE entities IS NOT NULL'):
        bucket = names.setdefault(row['source_id'], set())
        for entity in json.loads(row['entities']):
            bucket.add((entity['name'].casefold(), entity['type']))
    for source_id, unique in names.items():
        slot(source_id)['entities'] = len(unique)
    for row in db.rows("""SELECT e.source_id, COUNT(DISTINCT p.phone) n FROM phone_mentions p
        JOIN episodes e ON e.id=p.episode_id GROUP BY e.source_id"""):
        slot(row['source_id'])['phone_numbers'] = row['n']
    return metrics


def project_payload(row, metrics):
    values = metrics.get(row['id'], {})
    payload = {field: row[field] for field in PROJECT_FIELDS}
    payload['display_name'] = row['name'] or row['title']
    payload['episode_count'] = values.get('episode_count', 0)
    payload['completed'] = values.get('completed', 0)
    payload['active'] = values.get('active', 0)
    payload['failed'] = values.get('failed', 0)
    payload['transcripts'] = values.get('transcripts', 0)
    payload['entities'] = values.get('entities', 0)
    payload['phone_numbers'] = values.get('phone_numbers', 0)
    payload['updated'] = values.get('updated')
    payload['pollable'] = row['kind'] != 'video'
    return payload


@app.get('/api/projects')
def projects():
    metrics = project_metrics()
    return [project_payload(row, metrics) for row in
            db.rows('SELECT * FROM sources ORDER BY priority,created DESC')]


@app.put('/api/projects/order')
def reorder_projects(payload: ProjectOrder):
    """Set which project the processing queue works through first."""
    db.set_project_order([ident.strip() for ident in payload.ids])
    pipeline.wake.set()
    return projects()


@app.get('/api/projects/{ident}')
def project(ident: str):
    return project_payload(project_or_404(ident), project_metrics())


@app.patch('/api/projects/{ident}')
def edit_project(ident: str, payload: ProjectUpdate):
    project_or_404(ident)
    fields = {}
    if payload.name is not None:
        fields['name'] = ' '.join(payload.name.split()) or None
    if payload.description is not None:
        fields['description'] = ' '.join(payload.description.split()) or None
    if payload.accent is not None:
        if payload.accent not in db.ACCENTS:
            raise HTTPException(400, 'Onbekende projectkleur.')
        fields['accent'] = payload.accent
    if payload.enabled is not None:
        fields['enabled'] = int(payload.enabled)
    if fields:
        db.update('sources', ident, **fields)
        pipeline.source_wake.set()
    return project_payload(project_or_404(ident), project_metrics())


@app.delete('/api/projects/{ident}')
def remove_project(ident: str):
    """Delete one project environment with everything stored inside it."""
    project_or_404(ident)
    removed = db.delete_project(ident)
    for episode_id in removed:
        folder = db.DATA / 'audio' / episode_id
        for item in sorted(folder.glob('*'), reverse=True) if folder.is_dir() else []:
            item.unlink(missing_ok=True)
        if folder.is_dir():
            folder.rmdir()
        for cached in (db.DATA / 'thumbnails').glob(episode_id + '.*'):
            cached.unlink(missing_ok=True)
    return {'ok': True, 'episodes_removed': len(removed)}


@app.get('/api/health')
def health():
    return {'status': 'ok', 'service': 'audioscraper'}


@app.get('/api/processing')
def processing_status():
    active = db.one("""SELECT COUNT(*) n FROM episodes
        WHERE status IN ('downloading','transcribing','analyzing')""")['n']
    syncing = db.one('SELECT COUNT(*) n FROM sources WHERE syncing=1')['n']
    lookups = db.one("SELECT COUNT(*) n FROM phone_lookups WHERE status='searching'")['n']
    context_analyses = db.one("SELECT COUNT(*) n FROM phone_context_analyses WHERE status='searching'")['n']
    paused = db.processing_paused()
    return {'paused': paused, 'finishing': paused and active + syncing + lookups + context_analyses > 0,
            'active': active, 'syncing': syncing, 'lookups': lookups,
            'phone_context': context_analyses}


@app.put('/api/processing')
def set_processing(payload: ProcessingControl):
    db.set_processing_paused(payload.paused)
    pipeline.wake.set()
    pipeline.source_wake.set()
    pipeline.phone_wake.set()
    pipeline.phone_context_wake.set()
    return processing_status()


@app.get('/api/worker')
def worker_status():
    try:
        r = httpx.get(pipeline.WORKER+'/health', timeout=4)
        r.raise_for_status()
        data = r.json()
        return {'online': True, 'model': data.get('model'), 'entities_model': data.get('ollama_model'), 'gpu': data.get('device'), 'ollama_available': data.get('ollama_available')}
    except Exception:
        return {'online': False}


def require_audioscraper_speaker(profile_id):
    try:
        valid = uuid.UUID(profile_id).hex == profile_id
    except ValueError:
        valid = False
    if not valid or not db.one(
        "SELECT id FROM episodes WHERE segments LIKE ? LIMIT 1",
        ('%"speaker_id": "' + profile_id + '"%',),
    ):
        raise HTTPException(404, 'Stemprofiel niet gevonden.')


@app.get('/api/speakers')
def speakers(project: str = ''):
    return speaker_catalog(worker_json('GET', '/v1/speakers'), project)


@app.patch('/api/speakers/{profile_id}')
def rename_speaker(profile_id: str, payload: SpeakerName):
    require_audioscraper_speaker(profile_id)
    profile = worker_json('PATCH', '/v1/speakers/' + profile_id,
                          json={'name': payload.name.strip()})
    display_name = profile['display_name']
    with db.connect() as connection:
        rows = connection.execute(
            "SELECT id,segments FROM episodes WHERE segments LIKE ?",
            ('%' + profile_id + '%',),
        ).fetchall()
        for row in rows:
            segments = json.loads(row['segments'] or '[]')
            changed = False
            for segment in segments:
                if segment.get('speaker_id') == profile_id:
                    segment['speaker'] = display_name
                    changed = True
            if changed:
                connection.execute(
                    'UPDATE episodes SET segments=?,updated=? WHERE id=?',
                    (json.dumps(segments, ensure_ascii=False), time.time(), row['id']),
                )
    return {'profile_id': profile_id, 'name': display_name}


@app.get('/api/speakers/{profile_id}/sample')
def speaker_sample(profile_id: str):
    require_audioscraper_speaker(profile_id)
    try:
        response = httpx.get(
            pipeline.WORKER + '/v1/speakers/' + profile_id + '/sample',
            headers={'Authorization': 'Bearer ' + pipeline.TOKEN}, timeout=30,
        )
    except httpx.HTTPError as error:
        raise HTTPException(503, 'De stemworker is tijdelijk niet bereikbaar.') from error
    if response.status_code == 404:
        raise HTTPException(404, 'Geen luisterfragment beschikbaar.')
    response.raise_for_status()
    return Response(response.content, media_type='audio/wav', headers={
        'Cache-Control': 'private, no-store',
        'Content-Disposition': 'inline; filename="stemfragment.wav"',
    })


@app.post('/api/speakers/backfill')
def backfill_speakers():
    now = time.time()
    with db.connect() as connection:
        cursor = connection.execute("""UPDATE episodes
            SET speaker_requested=1,status='queued',stage='Stemherkenning in wachtrij',
                error=NULL,attempts=0,retry_at=0,updated=?
            WHERE transcript IS NOT NULL
              AND status IN ('completed','transcribed','failed')
              AND segments NOT LIKE '%speaker_id%'""", (now,))
        queued = cursor.rowcount
    pipeline.wake.set()
    return {'queued': queued}


@app.get('/api/sources')
def sources():
    return projects()


@app.post('/api/projects', status_code=201)
def create_project(payload: AddSource):
    """Add a source and, with it, the project environment that holds its work."""
    url = payload.url.strip()
    name = ' '.join(payload.name.split())
    description = ' '.join(payload.description.split())
    try:
        public_url(url)
        if payload.kind not in {'auto', 'youtube', 'youtube-videos', 'youtube-streams', 'youtube-shorts', 'youtube-playlist', 'rss', 'meetings'}:
            raise ValueError('Onbekend brontype.')
        host = urlsplit(url).hostname
        youtube_kinds = {'youtube', 'youtube-videos', 'youtube-streams', 'youtube-shorts', 'youtube-playlist'}
        if payload.kind in youtube_kinds or (payload.kind == 'auto' and host in {'youtube.com','www.youtube.com','m.youtube.com','youtu.be','www.youtu.be'}):
            kind, url, video_id = canonical_youtube(url, payload.kind)
            if kind == 'video':
                known = db.one('SELECT id,source_id FROM episodes WHERE external_id=?', ('yt:'+video_id,))
                if known:
                    return {'kind': 'video', 'episode_id': known['id'], 'project_id': known['source_id']}
                ident = db.create_project(url, 'video', name or 'YouTube · '+video_id, name,
                                          description, initial_limit=0, initialized=1, checked=time.time())
                episode = add_video(url, ident)
                pipeline.wake.set()
                return {'kind': 'video', 'episode_id': episode['id'], 'project_id': ident}
        else:
            # An unsupported platform raises from canonical() when explicitly chosen.
            meeting = meetings.canonical(url, payload.kind) if payload.kind in {'auto', 'meetings'} else None
            kind, url = meeting if meeting else ('rss', url)
    except ValueError as error:
        raise HTTPException(400, str(error))
    existing = db.one('SELECT * FROM sources WHERE url=?', (url,))
    if existing:
        return project_payload(existing, project_metrics())
    ident = db.create_project(url, kind, urlsplit(url).hostname, name, description, payload.initial_limit)
    pipeline.source_wake.set()
    return project_payload(project_or_404(ident), project_metrics())


@app.post('/api/sources', status_code=201)
def create_source(payload: AddSource):
    return create_project(payload)


@app.patch('/api/sources/{ident}')
def toggle_source(ident: str, payload: ToggleSource):
    project_or_404(ident)
    db.update('sources', ident, enabled=int(payload.enabled))
    pipeline.source_wake.set()
    return {'ok': True}


@app.post('/api/projects/{ident}/sync')
def refresh_project(ident: str):
    project = project_or_404(ident)
    if project['kind'] == 'video':
        raise HTTPException(409, 'Een losse video heeft geen bron om te controleren.')
    if not project['enabled']:
        raise HTTPException(409, 'Hervat dit project eerst.')
    if not project['syncing']:
        db.update('sources', ident, checked=None)
        pipeline.source_wake.set()
    return {'ok': True}


@app.post('/api/sources/{ident}/sync')
def refresh_source(ident: str):
    return refresh_project(ident)


@app.get('/api/episodes')
def episodes(q: str = '', status: str = '', entity: str = '', project: str = ''):
    scope, scope_args = project_filter(project, 'e.source_id')
    items = db.rows('''SELECT e.id,e.title,e.author,e.kind,e.url,e.duration,e.thumbnail_url,e.status,e.stage,e.created,e.updated,e.error,e.language,e.published,e.entities,
    e.source_id project_id, s.title source_title, COALESCE(s.name,s.title) project_name, s.accent project_accent
    FROM episodes e LEFT JOIN sources s ON e.source_id=s.id
    WHERE (?='' OR e.title LIKE ? OR e.transcript LIKE ? OR e.author LIKE ?) AND (?='' OR e.status=?)'''
    + scope + ' ORDER BY COALESCE(e.published_at,e.created) DESC', (q,'%'+q+'%','%'+q+'%','%'+q+'%',status,status,*scope_args))
    result = []
    for item in items:
        entities = json.loads(item.pop('entities') or '[]')
        if entity and not any(e['name'].casefold() == entity.casefold() for e in entities):
            continue
        item['entity_count'] = len(entities)
        item['top_entities'] = [{k:e[k] for k in ['name','type','count']} for e in entities[:4]]
        result.append(item)
    return result


def episode_or_404(ident):
    item = db.one('SELECT * FROM episodes WHERE id=?', (ident,))
    if not item:
        raise HTTPException(404, 'Aflevering niet gevonden.')
    return item


@app.get('/api/episodes/{ident}')
def episode(ident: str):
    item = episode_or_404(ident)
    for field in ['entities','segments']:
        item[field] = json.loads(item[field] or '[]')
    item['audio_available'] = bool(item['audio_path'] and Path(item['audio_path']).exists())
    speaker_counts = {}
    for segment in item['segments']:
        if segment.get('speaker'):
            key = (segment['speaker'], segment.get('speaker_id'))
            speaker_counts[key] = speaker_counts.get(key, 0) + 1
    item['speakers'] = [
        {'name': name, 'id': profile_id, 'segments': count}
        for (name, profile_id), count in sorted(
            speaker_counts.items(), key=lambda entry: entry[0][0].casefold()
        )
    ]
    owner = db.one('SELECT id,name,title,accent,kind FROM sources WHERE id=?', (item['source_id'],)) if item['source_id'] else None
    item['project'] = {
        'id': owner['id'], 'display_name': owner['name'] or owner['title'],
        'accent': owner['accent'], 'kind': owner['kind'],
    } if owner else None
    for field in ['audio_path','media_url','entity_chunks']:
        item.pop(field, None)
    return item


@app.post('/api/episodes/{ident}/retry')
def retry(ident: str):
    item = episode_or_404(ident)
    if item['status'] not in {'failed', 'completed', 'queued', 'transcribed'}:
        raise HTTPException(409, 'Deze aflevering wordt al verwerkt.')
    extra = {'entities':None, 'entity_chunks':None} if item['status']=='completed' else {}
    db.update('episodes', ident, status='queued', stage='In wachtrij', error=None, attempts=0, retry_at=0, **extra)
    pipeline.wake.set()
    return {'ok': True}


@app.post('/api/episodes/{ident}/speakers')
def recognize_speakers(ident: str):
    item = episode_or_404(ident)
    if not item['transcript']:
        raise HTTPException(409, 'Wacht eerst tot het transcript klaar is.')
    if item['status'] not in {'completed', 'transcribed', 'failed'}:
        raise HTTPException(409, 'Deze aflevering wordt al verwerkt.')
    db.update('episodes', ident, speaker_requested=1, status='queued',
              stage='Stemherkenning in wachtrij', error=None, attempts=0, retry_at=0)
    pipeline.wake.set()
    return {'ok': True}


@app.get('/api/episodes/{ident}/audio')
def audio(ident: str):
    item = episode_or_404(ident)
    if not item['audio_path'] or not Path(item['audio_path']).is_file():
        raise HTTPException(404, 'Audio nog niet beschikbaar.')
    return FileResponse(item['audio_path'], media_type='audio/mpeg')


def image_media_type(content: bytes):
    if content.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg', '.jpg'
    if content.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png', '.png'
    if content.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif', '.gif'
    if content.startswith(b'RIFF') and content[8:12] == b'WEBP':
        return 'image/webp', '.webp'
    return None, None


@app.get('/api/episodes/{ident}/thumbnail')
def thumbnail(ident: str):
    item = episode_or_404(ident)
    url = item.get('thumbnail_url') or (youtube_thumbnail(item['url']) if item['kind'] == 'youtube' else '')
    if not url:
        raise HTTPException(404, 'Geen afbeelding beschikbaar.')
    folder = db.DATA / 'thumbnails'
    cached = next(iter(folder.glob(ident+'.*')), None) if folder.exists() else None
    if cached and cached.is_file():
        media_type, _ = image_media_type(cached.read_bytes()[:16])
        return FileResponse(cached, media_type=media_type or 'application/octet-stream')
    content = fetch_public(url, max_bytes=3*1024**2)
    media_type, suffix = image_media_type(content)
    if not media_type:
        raise HTTPException(415, 'De bronafbeelding heeft geen ondersteund formaat.')
    folder.mkdir(parents=True, exist_ok=True)
    (folder / (ident+suffix)).write_bytes(content)
    return Response(content, media_type=media_type)


@app.get('/api/episodes/{ident}/transcript.txt')
def export(ident: str):
    item = episode_or_404(ident)
    if not item['transcript']:
        raise HTTPException(404, 'Transcript nog niet beschikbaar.')
    entities = json.loads(item['entities'] or '[]')
    text = item['title']+'\n'+item['url']+'\n\nGENOEMDE ENTITEITEN\n'
    text += '\n'.join(f"{e['name']} ({e['type']}, {e['count']}×)" for e in entities)
    segments = json.loads(item['segments'] or '[]')
    if any(segment.get('speaker') for segment in segments):
        lines = []
        for segment in segments:
            seconds = max(0, int(segment.get('start') or 0))
            timestamp = (f'{seconds//3600}:' if seconds >= 3600 else '') + f'{seconds//60%60:02d}:{seconds%60:02d}'
            speaker = f" {segment['speaker']}:" if segment.get('speaker') else ''
            lines.append(f'[{timestamp}]{speaker} {segment.get("text", "")}')
        transcript = '\n'.join(lines)
    else:
        transcript = item['transcript']
    text += '\n\nVOLLEDIG TRANSCRIPT\n\n'+transcript
    return PlainTextResponse(text, headers={'Content-Disposition': f'attachment; filename="transcript-{ident[:8]}.txt"'})


@app.get('/api/entities')
def all_entities(q: str = '', kind: str = '', region: str = '', project: str = '',
                 limit: int = 0, offset: int = 0):
    """Namen in een bereik, aflopend naar hoe breed ze worden genoemd.

    Een groot project levert tienduizenden namen, dus er gaat alleen een
    gevraagd stuk over de lijn. De afleveringen waarin een naam valt worden
    geteld en niet opgesomd: het dashboard laat er toch alleen het aantal van
    zien, en de opsomming was het leeuwendeel van het antwoord.
    """
    combined = {}
    scope, scope_args = project_filter(project, 'source_id')
    for row in db.rows('SELECT id,entities FROM episodes WHERE entities IS NOT NULL' + scope, scope_args):
        for e in json.loads(row['entities']):
            if q.casefold() not in e['name'].casefold() or (kind and e['type'] != kind):
                continue
            key = (e['name'].casefold(), e['type'])
            target = combined.setdefault(key, {'name': e['name'], 'type':e['type'], 'count':0, 'episodes':0})
            target['count'] += e['count']
            target['episodes'] += 1
    result = []
    for entity in combined.values():
        geography = classify_location(entity['name']) if entity['type'] == 'location' else None
        if region and (entity['type'] != 'location' or not matches_region(geography, region)):
            continue
        if geography:
            entity['geography'] = geography
        result.append(entity)
    result.sort(key=lambda e: (-e['episodes'], -e['count'], e['name']))
    offset = max(0, offset)
    page = result[offset:offset + limit] if limit > 0 else result[offset:]
    return {'total': len(result), 'entities': page}


@app.get('/api/entity-mentions')
def entity_mentions(name: str, kind: str = '', project: str = ''):
    name = name.strip()
    if not name:
        raise HTTPException(400, 'Geef een entiteitsnaam op.')
    grouped, canonical_name, canonical_kind = {}, name, kind
    scope, scope_args = project_filter(project, 'source_id')
    for row in db.rows('SELECT id,title,url,kind,published,segments,entities FROM episodes '
                       'WHERE entities IS NOT NULL' + scope, scope_args):
        segments = json.loads(row['segments'] or '[]')
        for entity in json.loads(row['entities']):
            if entity['name'].casefold() != name.casefold() or (kind and entity['type'] != kind):
                continue
            canonical_name, canonical_kind = entity['name'], entity['type']
            for occurrence in entity.get('occurrences', []):
                spans = occurrence.get('spans') or [occurrence]
                indices = sorted({span.get('segment') for span in spans if isinstance(span.get('segment'), int)})
                if not indices or any(index >= len(segments) for index in indices):
                    continue
                paragraph = ' '.join(segments[index].get('text', '') for index in indices).strip()
                key = (row['id'], tuple(indices))
                mention = grouped.setdefault(key, {
                    'episode_id': row['id'], 'title': row['title'], 'url': row['url'], 'kind': row['kind'], 'published': row['published'],
                    'segment': indices[0], 'start': occurrence.get('start', segments[indices[0]].get('start', 0)),
                    'text': paragraph, 'count': 0,
                })
                mention['count'] += 1
                mention['start'] = min(mention['start'], occurrence.get('start', mention['start']))
    mentions = sorted(grouped.values(), key=lambda item: (item['title'].casefold(), item['start'], item['segment']))
    return {'name': canonical_name, 'type': canonical_kind, 'mentions': mentions}


@app.get('/api/phone-numbers')
def phone_numbers(project: str = ''):
    scope, scope_args = project_filter(project, 'e.source_id')
    rows = db.rows('''SELECT p.phone,p.episode_id,p.segment,p.start,p.raw,p.display,p.category,p.context,
    e.title,e.url,e.kind,e.published,v.company,v.kvk_number,v.location,v.source_url,v.source_label,v.verified,
    l.status lookup_status,l.checked lookup_checked,l.retry_at lookup_retry_at,l.error lookup_error
    FROM phone_mentions p JOIN episodes e ON e.id=p.episode_id
    LEFT JOIN phone_verifications v ON v.phone=p.phone
    LEFT JOIN phone_lookups l ON l.phone=p.phone
    WHERE 1=1''' + scope + ' ORDER BY p.phone,p.episode_id,p.segment', scope_args)
    # Evidence shown next to a number must come from this project's own transcripts.
    analysis_rows = db.rows('''SELECT a.* FROM phone_context_analyses a
        JOIN episodes e ON e.id=a.episode_id
        WHERE 1=1''' + scope + ' ORDER BY a.phone,a.confidence DESC,a.analyzed DESC', scope_args)
    analyses = {}
    for analysis in analysis_rows:
        analyses.setdefault(analysis['phone'], []).append(analysis)
    attributions = {row['phone']: row for row in db.rows('SELECT * FROM phone_attributions')}
    combined = {}
    for row in rows:
        phone = row['phone']
        item = combined.setdefault(phone, {
            'number': phone, 'display': row['display'], 'category': row['category'],
            'company': row['company'], 'kvk_number': row['kvk_number'], 'location': row['location'],
            'source_url': row['source_url'], 'source_label': row['source_label'],
            'verified': row['verified'], 'area': None, 'mentions': [], 'episodes': [],
            'lookup_status': 'matched' if row['company'] else row['lookup_status'] or 'pending',
            'lookup_checked': row['lookup_checked'], 'lookup_retry_at': row['lookup_retry_at'],
            'lookup_error': row['lookup_error'],
            'context_candidates': [], 'context_status': 'none',
            'attribution': attributions.get(phone),
        })
        if not item['company']:
            item['area'] = net_area(phone)
        occurrence = {key: row[key] for key in ['episode_id','segment','start','raw','context','title','url','kind','published']}
        item['mentions'].append(occurrence)
        episode = next((episode for episode in item['episodes'] if episode['id'] == row['episode_id']), None)
        if not episode:
            episode = {'id': row['episode_id'], 'title': row['title'], 'url': row['url'], 'kind': row['kind'], 'published': row['published'], 'mentions': []}
            item['episodes'].append(episode)
        episode['mentions'].append(occurrence)
    for phone, item in combined.items():
        phone_analyses = analyses.get(phone, [])
        statuses = {analysis['status'] for analysis in phone_analyses}
        if 'searching' in statuses:
            item['context_status'] = 'searching'
        elif 'pending' in statuses:
            item['context_status'] = 'pending'
        elif 'error' in statuses:
            item['context_status'] = 'error'
        elif phone_analyses and statuses == {'completed'}:
            item['context_status'] = 'completed'
        item['context_candidates'] = [{key: analysis[key] for key in [
            'episode_id', 'segment', 'subject_type', 'relation', 'label',
            'description', 'confidence', 'evidence', 'model', 'analyzed',
        ]} for analysis in phone_analyses if analysis['status'] == 'completed']
        item['best_candidate'] = item['context_candidates'][0] if item['context_candidates'] else None
    return sorted(combined.values(), key=lambda item: (
        not bool(item['company']),
        not bool(item.get('attribution') and item['attribution']['state'] == 'confirmed'),
        not bool(item.get('best_candidate') and item['best_candidate']['confidence'] >= 60),
        -len(item['mentions']), item['number']))


def require_phone(phone):
    if not re.fullmatch(r'0\d{7,10}', phone) or not db.one(
        'SELECT phone FROM phone_mentions WHERE phone=? LIMIT 1', (phone,)
    ):
        raise HTTPException(404, 'Telefoonnummer niet gevonden.')


@app.patch('/api/phone-numbers/{phone}/attribution')
def update_phone_attribution(phone: str, payload: PhoneAttributionUpdate):
    require_phone(phone)
    state = payload.state.strip().lower()
    if state == 'clear':
        with db.connect() as connection:
            connection.execute('DELETE FROM phone_attributions WHERE phone=?', (phone,))
        return {'ok': True, 'state': 'clear'}
    if state not in {'confirmed', 'rejected'}:
        raise HTTPException(400, 'Onbekende beoordelingsstatus.')
    if state == 'rejected':
        values = ('rejected', None, None, None, None, None, None, None, None, 'manual', time.time())
    else:
        subject_type = payload.subject_type.strip()
        relation = payload.relation.strip()
        label = ' '.join(payload.label.split()).strip(' .,;:-')
        description = ' '.join(payload.description.split())
        if subject_type not in phone_context.SUBJECT_TYPES - {'unknown'}:
            raise HTTPException(400, 'Kies een geldig type voor deze koppeling.')
        if relation not in phone_context.RELATIONS:
            raise HTTPException(400, 'Kies een geldige relatie tot het nummer.')
        if subject_type in {'person', 'organization'} and not label:
            raise HTTPException(400, 'Een persoon of organisatie heeft een naam nodig.')
        if not label and not description:
            raise HTTPException(400, 'Vul een naam of korte omschrijving in.')
        evidence, episode_id, segment = '', payload.episode_id, payload.segment
        if episode_id is not None and segment is not None:
            candidate = db.one('''SELECT evidence FROM phone_context_analyses
                WHERE phone=? AND episode_id=? AND segment=? AND status='completed' ''',
                (phone, episode_id, segment))
            if not candidate:
                raise HTTPException(400, 'De gekozen bewijsvermelding bestaat niet meer.')
            evidence = candidate['evidence'] or ''
        else:
            episode_id, segment = None, None
        values = ('confirmed', subject_type, relation, label, description, 100,
                  evidence, episode_id, segment, 'manual', time.time())
    with db.connect() as connection:
        connection.execute('''INSERT INTO phone_attributions
            (phone,state,subject_type,relation,label,description,confidence,evidence,
             episode_id,segment,source,reviewed)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(phone) DO UPDATE SET state=excluded.state,
            subject_type=excluded.subject_type,relation=excluded.relation,label=excluded.label,
            description=excluded.description,confidence=excluded.confidence,evidence=excluded.evidence,
            episode_id=excluded.episode_id,segment=excluded.segment,source=excluded.source,
            reviewed=excluded.reviewed''', (phone, *values))
        if state == 'confirmed' and values[3]:
            connection.execute("UPDATE phone_lookups SET retry_at=0 WHERE phone=? AND status='not_found'", (phone,))
    pipeline.phone_wake.set()
    return {'ok': True, 'state': state}


@app.get('/api/stats')
def stats(project: str = ''):
    scope, scope_args = project_filter(project, 'source_id')
    counts = {r['status']:r['n'] for r in db.rows(
        'SELECT status,COUNT(*) n FROM episodes WHERE 1=1' + scope + ' GROUP BY status', scope_args)}
    entities = all_entities(project=project)
    phone_scope, phone_args = project_filter(project, 'e.source_id')
    phone_count = db.one('SELECT COUNT(DISTINCT p.phone) n FROM phone_mentions p '
                         'JOIN episodes e ON e.id=p.episode_id WHERE 1=1' + phone_scope, phone_args)['n']
    project_count = 1 if project else db.one('SELECT COUNT(*) n FROM sources')['n']
    return {'total':sum(counts.values()), 'completed':counts.get('completed',0), 'active':sum(counts.get(k,0) for k in ['queued','downloading','transcribing','transcribed','analyzing']), 'failed':counts.get('failed',0), 'entities':len(entities), 'phone_numbers':phone_count, 'sources':project_count, 'projects':project_count}


def worker_inference_progress(episode_id: str):
    """Return worker progress only when it belongs to this dashboard job."""
    try:
        response = httpx.get(pipeline.WORKER+'/health', timeout=2)
        response.raise_for_status()
        data = response.json()
    except Exception:
        return None
    if data.get('inference_recording_id') != f'audioscraper:{episode_id}':
        return None
    progress = {
        'phase': data.get('inference_phase'),
        'duration': data.get('inference_duration'),
        'processed_seconds': data.get('inference_processed_seconds'),
        'percent': data.get('inference_percent'),
    }
    return progress if isinstance(progress['percent'], int) else None


@app.get('/api/transcript-progress')
def transcript_progress(project: str = ''):
    """Return only durable transcript progress, never an estimated audio percentage."""
    scope, args = project_filter(project, 'source_id')
    total = db.one("SELECT COUNT(*) n FROM episodes WHERE status!='failed'" + scope, args)['n']
    done = db.one("SELECT COUNT(*) n FROM episodes WHERE status!='failed' AND transcript IS NOT NULL" + scope, args)['n']
    waiting = db.one("SELECT COUNT(*) n FROM episodes WHERE transcript IS NULL AND status='queued'" + scope, args)['n']
    running = db.rows('''SELECT id,title,author,status,stage,duration,updated FROM episodes
        WHERE transcript IS NULL AND status IN ('downloading','transcribing')'''
        + scope + ' ORDER BY updated ASC', args)
    current = running[0] if running else None
    if current:
        current['worker_progress'] = worker_inference_progress(current['id'])
    remaining = max(0, total - done)
    return {
        'total': total,
        'done': done,
        'remaining': remaining,
        'waiting': waiting,
        'running': len(running),
        'percent': round(done * 100 / total) if total else 0,
        'current': current,
    }


@app.get('/api/entity-progress')
def entity_progress(project: str = ''):
    """Report persisted entity chunks, so a resumed job keeps its real progress."""
    scope, args = project_filter(project, 'source_id')
    summary = db.one('''SELECT COUNT(*) total,
        SUM(entities IS NOT NULL) done,
        SUM(transcript IS NULL) awaiting_transcript
        FROM episodes WHERE status!='failed' ''' + scope, args)
    total = summary['total'] or 0
    done = summary['done'] or 0
    waiting = db.one('''SELECT COUNT(*) n FROM episodes
        WHERE transcript IS NOT NULL AND entities IS NULL
        AND status IN ('queued','transcribed')''' + scope, args)['n']
    running = db.rows('''SELECT id,title,author,status,stage,transcript,entity_chunks
        FROM episodes WHERE status='analyzing' AND entities IS NULL'''
        + scope + ' ORDER BY updated ASC', args)
    current = None
    if running:
        row = running[0]
        parts_total = sum(1 for _ in pipeline.text_chunks(row['transcript'] or ''))
        parts_done = min(parts_total, len(json.loads(row['entity_chunks'] or '[]')))
        current = {
            'id': row['id'], 'title': row['title'], 'author': row['author'],
            'status': row['status'], 'stage': row['stage'],
            'parts_done': parts_done, 'parts_total': parts_total,
            'percent': round(parts_done * 100 / parts_total) if parts_total else 0,
        }
    return {
        'total': total, 'done': done, 'remaining': max(0, total - done),
        'waiting': waiting, 'awaiting_transcript': summary['awaiting_transcript'] or 0,
        'running': len(running), 'percent': round(done * 100 / total) if total else 0,
        'current': current,
    }


@app.get('/')
def index():
    return FileResponse(STATIC/'index.html')

app.mount('/static', StaticFiles(directory=STATIC), name='static')
