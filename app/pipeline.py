import json
import logging
import os
import re
import threading
import time
from pathlib import Path

import httpx
from . import db, phone_context, phone_lookup
from .netnumbers import net_area
from .sources import download, sync_source

log = logging.getLogger('audioscraper')
WORKER = os.getenv('WORKER_URL', 'http://192.168.1.28:8765').rstrip('/')
TOKEN = os.getenv('WORKER_TOKEN', '')
stop = threading.Event()
wake = threading.Event()
source_wake = threading.Event()
phone_wake = threading.Event()
phone_context_wake = threading.Event()
threads = []


class EntityChunkError(RuntimeError):
    """An entity fragment still failed after safe subdivision."""

# Only entries with a public, reviewable source belong here. KVK's public API
# cannot reverse-search telephone numbers, so it must never be used to guess a
# company from a number.
PHONE_VERIFICATIONS = {
    '0880237500': {
        'company': 'Stichting Farent',
        'kvk_number': '73600776',
        'location': "Rogier van der Weydenstraat 2, 5213 CA 's-Hertogenbosch",
        'source_url': 'https://www.farent.nl/over-farent/',
        'source_label': 'Farent · contactpagina',
        'verified': 1789646400,  # 17 September 2026
    },
}
PHONE_SCAN_VERSION = 5
PHONE_WORDS = {
    'nul': '0', 'een': '1', 'één': '1', 'twee': '2', 'drie': '3',
    'vier': '4', 'vijf': '5', 'zes': '6', 'zeven': '7', 'acht': '8',
    'negen': '9',
}
PHONE_WORD_RE = re.compile(r'\b(?:nul|een|één|twee|drie|vier|vijf|zes|zeven|acht|negen)\b', re.I)
PHONE_DIGIT_RE = re.compile(r'(?<!\w)(?:\+31[\s().-]*|0031[\s().-]*|0)(?:[\s().-]*\d){7,11}(?!\w)')
IBAN_RE = re.compile(r'\bNL[\s.,:-]*\d{2}[\s.,:-]*[A-Z]{4}(?:[\s.,:-]*\d){10}\b', re.I)


def normalize_phone(raw):
    digits = re.sub(r'\D', '', raw)
    compact = re.sub(r'\s+', '', raw)
    if compact.startswith('+31'):
        digits = '0' + digits[2:]
    elif compact.startswith('0031'):
        digits = '0' + digits[4:]
    if not digits.startswith('0'):
        return ''
    if digits.startswith('06') or digits.startswith('088'):
        return digits if len(digits) == 10 else ''
    if digits.startswith('0800') or digits.startswith(('0900', '0906', '0909')):
        return digits if 8 <= len(digits) <= 11 else ''
    if len(digits) != 10 or not net_area(digits):
        return ''
    return digits


def phone_category(phone):
    if phone.startswith('06'):
        return 'Mobiel nummer'
    if phone.startswith('0800'):
        return 'Gratis servicenummer'
    if phone.startswith(('0900', '0906', '0909')):
        return 'Betaald servicenummer'
    if phone.startswith('088'):
        return 'Bedrijfsnummer'
    return 'Telefoonnummer'


def phone_display(phone):
    if phone.startswith('06') and len(phone) == 10:
        return f'{phone[:2]} {phone[2:6]} {phone[6:]}'
    if phone.startswith('088') and len(phone) == 10:
        return f'{phone[:3]} {phone[3:7]} {phone[7:]}'
    if len(phone) == 10:
        return f'{phone[:3]} {phone[3:6]} {phone[6:]}'
    return phone


def extract_phone_mentions(segments):
    """Find written or spoken Dutch telephone numbers in timestamped text."""
    mentions = []
    joined = ' '.join(segment.get('text', '') for segment in segments)
    iban_spans = [(match.start(), match.end()) for match in IBAN_RE.finditer(joined)]
    position = 0
    for index, segment in enumerate(segments):
        text = segment.get('text', '')
        offset = position
        position += len(text) + 1
        candidates = [(match.start(), match.end(), match.group()) for match in PHONE_DIGIT_RE.finditer(text)]
        words = list(PHONE_WORD_RE.finditer(text))
        run, runs = [], []
        for word in words:
            if run and word.start() - run[-1].end() > 4:
                runs.append(run)
                run = []
            run.append(word)
        if run:
            runs.append(run)
        for part in runs:
            if not 8 <= len(part) <= 11:
                continue
            raw = text[part[0].start():part[-1].end()]
            # A sequence of spoken digits needs a nearby call/number cue;
            # this avoids treating ordinary counted lists as phone numbers.
            nearby = text[max(0, part[0].start()-80):min(len(text), part[-1].end()+80)].casefold()
            if re.search(r'nummer|telefoon|bel|bereikbaar', nearby):
                candidates.append((part[0].start(), part[-1].end(), raw))
        seen = set()
        for start, end, raw in candidates:
            if any(offset+start < iban_end and offset+end > iban_start for iban_start, iban_end in iban_spans):
                continue
            phone = normalize_phone(raw if any(ch.isdigit() for ch in raw) else ''.join(PHONE_WORDS[w.group().casefold()] for w in PHONE_WORD_RE.finditer(raw)))
            if not phone or phone in seen:
                continue
            seen.add(phone)
            context = re.sub(r'\s+', ' ', text[max(0, start-105):min(len(text), end+145)]).strip()
            mentions.append({'phone': phone, 'segment': index, 'start': float(segment.get('start', 0)),
                             'raw': raw, 'display': phone_display(phone), 'category': phone_category(phone),
                             'context': context})
    return mentions


def scan_phone_mentions(episode_id, segments):
    db.replace_phone_mentions(episode_id, extract_phone_mentions(segments), PHONE_SCAN_VERSION)


def seed_phone_verifications():
    directory = json.loads(Path(__file__).with_name('phone_directory.json').read_text())
    for phone, details in {**PHONE_VERIFICATIONS, **directory}.items():
        db.put_phone_verification(phone, **details)


def backfill_phone_mentions():
    for episode in db.rows('''SELECT e.id,e.segments FROM episodes e
    WHERE e.transcript IS NOT NULL AND NOT EXISTS (SELECT 1 FROM phone_scans s WHERE s.episode_id=e.id AND s.version>=?)''', (PHONE_SCAN_VERSION,)):
        if db.processing_paused():
            return False
        scan_phone_mentions(episode['id'], json.loads(episode['segments'] or '[]'))
    return True


def discard_audio(episode_id, audio_path):
    """Remove downloaded media only after its transcript is durably saved.

    Downloaded files are a temporary transport format. The archive keeps the
    transcript, timestamped segments, entities and the original source URL.
    Never delete a path outside the application's own audio directory.
    """
    if not audio_path:
        return False
    audio = Path(audio_path)
    audio_root = (db.DATA / 'audio').resolve()
    try:
        audio.resolve().relative_to(audio_root)
    except ValueError:
        log.warning('Refusing to remove audio outside the data directory for %s', episode_id)
        return False

    try:
        audio.unlink(missing_ok=True)
        parent = audio.parent
        while parent != audio_root:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    except OSError:
        log.exception('Could not remove temporary audio for %s', episode_id)
        return False
    db.update('episodes', episode_id, audio_path=None)
    log.info('Removed temporary audio for %s after transcript storage', episode_id)
    return True


def purge_transcribed_audio():
    """One-off recovery for media retained by an older service version."""
    for episode in db.rows("SELECT id, audio_path FROM episodes WHERE transcript IS NOT NULL AND audio_path IS NOT NULL"):
        if db.processing_paused():
            return False
        discard_audio(episode['id'], episode['audio_path'])
    return True


def worker_request(path, read_timeout=14400, **kwargs):
    with httpx.Client(timeout=httpx.Timeout(30, read=read_timeout, write=300), headers={'Authorization': 'Bearer '+TOKEN}) as client:
        r = client.post(WORKER + path, **kwargs)
        if r.status_code == 404:
            raise RuntimeError('De worker mist de Audioscraper-uitbreiding. Installeer de bijgewerkte worker.')
        if r.status_code == 401:
            raise RuntimeError('De worker weigert het ingestelde toegangstoken.')
        r.raise_for_status()
        return r.json()


def split_entity_text(text, target=1600):
    """Split without overlap; the parent transcript chunk already overlaps."""
    parts, start = [], 0
    while start < len(text):
        end = min(len(text), start + target)
        if end < len(text):
            boundary = text.rfind(' ', start + target // 2, end)
            if boundary > start:
                end = boundary
        parts.append(text[start:end])
        start = end
    return [part for part in parts if part.strip()]


def request_entities(text, subdivided=False):
    """Retry content-dependent worker 500s with smaller text fragments.

    Ollama occasionally cannot produce schema-valid JSON for a dense 6,500
    character fragment. Smaller fragments succeed and can be merged into the
    same durable parent checkpoint, so existing checkpoints remain valid.
    """
    try:
        result = worker_request('/v1/entities', read_timeout=1300, json={'text': text})
        if not isinstance(result.get('entities'), list):
            raise ValueError('Worker gaf geen geldige lijst van entiteiten terug.')
        return result['entities']
    except httpx.HTTPStatusError as error:
        if error.response.status_code != 500 or len(text) <= 900:
            raise
        target = 800 if subdivided else 1600
        parts = split_entity_text(text, target)
        if len(parts) < 2:
            raise
        combined, seen = [], set()
        for part in parts:
            try:
                entities = request_entities(part, subdivided=True)
            except httpx.HTTPStatusError as nested:
                raise EntityChunkError('Entiteitsdetectie bleef mislukken na het veilig opsplitsen van een tekstdeel.') from nested
            for entity in entities:
                key = (str(entity.get('name', '')).casefold(), entity.get('type'))
                if key not in seen:
                    combined.append(entity)
                    seen.add(key)
        return combined


def request_phone_context(text, entities):
    result = worker_request('/v1/phone-context', read_timeout=650,
                            json={'text': text, 'nearby_entities': entities})
    if not isinstance(result, dict):
        raise ValueError('Worker gaf geen geldige nummeranalyse terug.')
    return result


def worker_gpu_busy():
    """Avoid parking an entity request behind a long shared-GPU job."""
    try:
        with httpx.Client(timeout=3, headers={'Authorization': 'Bearer '+TOKEN}) as client:
            response = client.get(WORKER + '/health')
            response.raise_for_status()
            return bool(response.json().get('inference_phase'))
    except (httpx.HTTPError, ValueError):
        # Let the real request provide the actionable connection error.
        return False


def phone_context_available():
    """Run number context only after higher-priority archive work is done."""
    if db.processing_paused() or worker_gpu_busy():
        return False
    return not db.one('''SELECT id FROM episodes
        WHERE status NOT IN ('completed','failed')
          AND (transcript IS NULL OR entities IS NULL OR speaker_requested=1)
        LIMIT 1''')


def text_chunks(text, size=6500, overlap=350):
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            boundary = text.rfind(' ', start + size//2, end)
            if boundary > start:
                end = boundary
        yield text[start:end]
        if end == len(text):
            break
        start = max(start+1, end-overlap)


def locate_entities(candidates, segments, transcript):
    # Search a single continuous timeline, so names split by Whisper segment
    # boundaries are still counted once and highlighted in every affected part.
    from bisect import bisect_right
    offsets, position = [], 0
    for segment in segments:
        offsets.append(position)
        position += len(segment['text']) + 1
    text = ' '.join(s['text'] for s in segments)
    entities = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        name, kind = str(candidate.get('name', '')).strip(), candidate.get('type', 'other')
        if not name or len(name) > 200 or kind not in {'person','organization','location','product','event','other'}:
            continue
        key = name.casefold()
        if key in entities:
            continue
        pattern = re.compile(r'(?<!\w)'+re.escape(name)+r'(?!\w)', re.I)
        occurrences = []
        for match in pattern.finditer(text):
            index = bisect_right(offsets, match.start()) - 1
            spans = []
            for j in range(index, len(segments)):
                if offsets[j] >= match.end():
                    break
                spans.append({'segment':j, 'offset':max(0,match.start()-offsets[j]), 'end_offset':min(len(segments[j]['text']),match.end()-offsets[j])})
            occurrences.append({**spans[0], 'start':segments[index].get('start',0), 'spans':spans})
        if occurrences:
            entities[key] = {'name': name, 'type': kind, 'count': len(occurrences), 'occurrences': occurrences}
    return sorted(entities.values(), key=lambda e: (-e['count'], e['name'].casefold()))


def process(episode):
    if db.processing_paused():
        return
    ident = episode['id']
    if not episode['transcript'] or episode.get('speaker_requested'):
        audio = episode.get('audio_path')
        if not audio or not Path(audio).is_file():
            db.update('episodes', ident, status='downloading', stage='Audio downloaden', error=None)
            audio = download(episode)
            db.update('episodes', ident, audio_path=audio)
        if db.processing_paused():
            db.update('episodes', ident, status='queued', stage='Gepauzeerd · audio klaar')
            return
        stage = ('Stemmen herkennen · Whisper en diarization'
                 if episode.get('speaker_requested')
                 else 'Transcriberen met Whisper · wacht zo nodig op de gedeelde GPU')
        db.update('episodes', ident, status='transcribing', stage=stage)
        with open(audio, 'rb') as f:
            result = worker_request('/v1/media/transcriptions', files={'file': ('audio.mp3', f, 'audio/mpeg')}, data={
                'response_format': 'verbose_json', 'recording_id': f'audioscraper:{ident}',
                'diarize': 'true',
            })
        segments = result.get('segments') or []
        transcript = result.get('text', '').strip()
        if not transcript:
            raise RuntimeError('Whisper heeft geen gesproken tekst gevonden.')
        if not segments:
            segments = [{'start': 0, 'end': result.get('duration', 0), 'text': transcript}]
        # Keep timestamped text and speaker identity. Word-level metadata is
        # unnecessary here and would make the database much larger.
        local_speakers = {}
        cleaned_segments = []
        for segment in segments:
            speaker = segment.get('speaker')
            speaker_id = segment.get('speaker_id')
            if speaker and re.fullmatch(r'SPEAKER_\d+', speaker):
                speaker = local_speakers.setdefault(speaker, f'Spreker {len(local_speakers)+1}')
            cleaned = {
                'start': segment['start'], 'end': segment['end'],
                'text': segment['text'].strip(),
            }
            if speaker:
                cleaned['speaker'] = speaker
            if speaker_id:
                cleaned['speaker_id'] = speaker_id
            cleaned_segments.append(cleaned)
        segments = cleaned_segments
        db.update('episodes', ident, transcript=transcript,
                  segments=json.dumps(segments, ensure_ascii=False),
                  language=result.get('language'), duration=result.get('duration'),
                  speaker_error=result.get('diarization_error'), speaker_requested=0,
                  entities=None, entity_chunks=None, attempts=0)
        scan_phone_mentions(ident, segments)
        discard_audio(ident, audio)
        db.update('episodes', ident, status='transcribed', stage='Transcript klaar · wacht op overige transcripties', error=None)
        return
    segments = json.loads(episode['segments'])
    chunks = list(text_chunks(episode['transcript']))
    checkpoint = json.loads(episode.get('entity_chunks') or '[]')
    db.update('episodes', ident, status='analyzing', error=None)
    for i in range(len(checkpoint), len(chunks)):
        if stop.is_set():
            return
        if db.processing_paused():
            db.update('episodes', ident, status='transcribed', stage='Entiteitsdetectie gepauzeerd')
            return
        if worker_gpu_busy():
            db.update('episodes', ident, status='transcribed', stage='Entiteiten wachten op gedeelde GPU', retry_at=time.time()+30)
            return
        db.update('episodes', ident, stage=f'Entiteiten herkennen · deel {i+1} van {len(chunks)}')
        # The worker bounds each of the two Ollama passes at ten minutes. Do
        # not let a broken entity request occupy the queue for four hours like
        # a long Whisper transcription is allowed to do.
        checkpoint.append(request_entities(chunks[i]))
        db.update('episodes', ident, entity_chunks=json.dumps(checkpoint, ensure_ascii=False))
    if db.processing_paused():
        db.update('episodes', ident, status='transcribed', stage='Entiteitsdetectie gepauzeerd')
        return
    entities = locate_entities([e for chunk in checkpoint for e in chunk], segments, episode['transcript'])
    db.update('episodes', ident, entities=json.dumps(entities, ensure_ascii=False), status='completed', stage='Voltooid', error=None, attempts=0)
    # Newly identified organization names can improve an earlier phone search.
    with db.connect() as conn:
        conn.execute('''UPDATE phone_lookups SET retry_at=0 WHERE status='not_found'
            AND phone IN (SELECT phone FROM phone_mentions WHERE episode_id=?)''', (ident,))
    phone_context_wake.set()


def next_episode():
    """Work the projects in the order the dashboard shows them.

    A project is finished before the next one starts. Inside a project a
    durable transcript always comes first, and entity analysis waits while that
    same project still owes a transcript — also when that transcript is only
    waiting for a retry, because Ollama would otherwise occupy the shared GPU
    exactly when Whisper becomes eligible again. A project that is blocked on a
    delayed retry is skipped so the queue keeps working instead of idling.
    Inside one project the newest episode goes first.
    """
    if db.processing_paused():
        return None
    return db.one("""SELECT e.* FROM episodes e
    LEFT JOIN sources s ON s.id=e.source_id
    WHERE e.status IN ('queued','transcribed') AND e.retry_at<=?
      AND ((e.transcript IS NULL OR e.speaker_requested=1)
           OR (e.transcript IS NOT NULL AND e.entities IS NULL AND NOT EXISTS (
                 SELECT 1 FROM episodes o WHERE o.source_id IS e.source_id
                   AND (o.transcript IS NULL OR o.speaker_requested=1)
                   AND o.status IN ('queued','transcribed','downloading','transcribing'))))
    ORDER BY COALESCE(s.priority,0), s.created DESC,
             CASE WHEN e.transcript IS NULL OR e.speaker_requested=1 THEN 0 ELSE 1 END,
             CASE WHEN e.segments LIKE '%speaker_id%' THEN 0 ELSE 1 END,
             COALESCE(e.published_at,e.created) DESC LIMIT 1""", (time.time(),))


def transient_error(error):
    if isinstance(error, EntityChunkError):
        return True
    if isinstance(error, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in {408, 409, 425, 429} or error.response.status_code >= 500
    return isinstance(error, OSError) and getattr(error, 'errno', None) in {101, 104, 110, 111, 113}


def schedule_failure(episode, error):
    current = db.one('SELECT attempts,transcript FROM episodes WHERE id=?', (episode['id'],))
    attempts = current['attempts'] + 1
    temporary = transient_error(error)
    retry = temporary or attempts < 3
    if temporary:
        delay = min(1800, 60 * (2 ** min(attempts - 1, 5)))
        minutes = max(1, round(delay / 60))
        status = 'transcribed' if current['transcript'] else 'queued'
        stage = f'Tijdelijke storing · nieuwe poging over {minutes} minuut/minuten'
    else:
        delay = 60 * attempts
        status = 'queued' if retry else 'failed'
        stage = f'Nieuwe poging over {attempts} minuut/minuten' if retry else 'Verwerking mislukt'
    message = re.sub(r'https?://\S+', '[URL]', str(error))[:1800]
    db.update('episodes', episode['id'], status=status, stage=stage, error=message,
              attempts=attempts, retry_at=time.time()+delay)


def recover_retryable_failures():
    """One-time recovery for failures stored by the old three-strikes policy."""
    patterns = ('Server error \'5', 'No route to host', 'Connection reset',
                'Connection refused', 'timed out', 'Temporary failure')
    for episode in db.rows("SELECT id,transcript,error FROM episodes WHERE status='failed'"):
        if any(pattern.casefold() in (episode['error'] or '').casefold() for pattern in patterns):
            db.update('episodes', episode['id'],
                      status='transcribed' if episode['transcript'] else 'queued',
                      stage='Automatisch opnieuw proberen', attempts=0, retry_at=0, error=None)


def run_queue():
    maintenance_done = False
    while not stop.is_set():
        if db.processing_paused():
            wake.wait(1)
            wake.clear()
            continue
        if not maintenance_done:
            maintenance_done = backfill_phone_mentions() and purge_transcribed_audio()
            if not maintenance_done:
                continue
        episode = next_episode()
        if not episode:
            wake.wait(3)
            wake.clear()
            continue
        try:
            process(episode)
        except Exception as error:
            log.exception('Processing failed for %s', episode['id'])
            schedule_failure(episode, error)


def run_sources():
    interval = int(os.getenv('POLL_SECONDS', '3600'))
    while not stop.is_set():
        if db.processing_paused():
            source_wake.wait(1)
            source_wake.clear()
            continue
        # A single video is its own project but has nothing to poll for.
        for source in db.rows("SELECT * FROM sources WHERE enabled=1 AND kind!='video' AND (checked IS NULL OR checked<?)", (time.time()-interval,)):
            if stop.is_set():
                return
            if db.processing_paused():
                break
            sync_source(source)
            wake.set()
        source_wake.wait(10)
        source_wake.clear()


def start():
    stop.clear()
    phone_lookup.init()
    phone_context.init()
    seed_phone_verifications()
    recover_retryable_failures()
    for target in [run_queue, run_sources, run_phone_lookups, run_phone_context]:
        t = threading.Thread(target=target, daemon=True, name=target.__name__)
        t.start()
        threads.append(t)


def run_phone_lookups():
    phone_lookup.run(stop, phone_wake)


def run_phone_context():
    phone_context.run(stop, phone_context_wake, request_phone_context, phone_context_available)


def shutdown():
    stop.set()
    wake.set()
    source_wake.set()
    phone_wake.set()
    phone_context_wake.set()
    for t in threads:
        t.join(timeout=2)
