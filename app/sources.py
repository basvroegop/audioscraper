import hashlib
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from urllib.parse import parse_qs, urljoin, urlsplit

import feedparser
import httpx
from . import db

MAX_BYTES = 2 * 1024**3
YT_HOSTS = {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be', 'www.youtu.be'}
YOUTUBE_SCOPES = {
    'youtube': ('Kanaal', None),
    'youtube-videos': ('Video’s', 'videos'),
    'youtube-streams': ('Live-uitzendingen', 'streams'),
    'youtube-shorts': ('Shorts', 'shorts'),
    'youtube-playlist': ('Afspeellijst', None),
}

def public_url(url):
    try:
        p = urlsplit(url)
        if p.scheme not in {'http', 'https'} or not p.hostname or p.username or p.password:
            raise ValueError('Gebruik een openbare http- of https-URL zonder inloggegevens.')
        addresses = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == 'https' else 80), type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError('Alleen openbare internetadressen zijn toegestaan als bron.')
    except (OSError, ValueError) as e:
        raise ValueError(str(e) if isinstance(e, ValueError) else 'Dit internetadres is niet bereikbaar.') from e
    return url

def canonical_youtube(url, scope='auto'):
    p = urlsplit(url)
    if p.hostname not in YT_HOSTS:
        raise ValueError('Gebruik een youtube.com- of youtu.be-link.')
    query = parse_qs(p.query)
    video = p.path.strip('/') if p.hostname.endswith('youtu.be') else query.get('v', [''])[0]
    if p.path.startswith(('/shorts/', '/live/', '/embed/')):
        video = p.path.split('/')[2]
    if video:
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video):
            raise ValueError('Ongeldig YouTube-video-ID.')
        # A watch URL with a playlist is a playlist source only when the user
        # explicitly chose that scope. Otherwise a specific video wins.
        if scope == 'youtube-playlist' and query.get('list'):
            playlist = query['list'][0]
            if not re.fullmatch(r'[A-Za-z0-9_-]{10,200}', playlist):
                raise ValueError('Ongeldig YouTube-afspeellijst-ID.')
            return 'youtube-playlist', f'https://www.youtube.com/playlist?list={playlist}', None
        return 'video', f'https://www.youtube.com/watch?v={video}', video
    if p.path == '/playlist' and query.get('list'):
        playlist = query['list'][0]
        if not re.fullmatch(r'[A-Za-z0-9_-]{10,200}', playlist):
            raise ValueError('Ongeldig YouTube-afspeellijst-ID.')
        return 'youtube-playlist', f'https://www.youtube.com/playlist?list={playlist}', None
    if p.path.startswith(('/@', '/channel/', '/c/', '/user/')):
        parts = p.path.rstrip('/').split('/')
        tab = parts[-1] if parts[-1] in {'videos', 'streams', 'shorts', 'featured', 'playlists'} else None
        if tab:
            parts.pop()
        if scope == 'auto':
            kind = {'videos': 'youtube-videos', 'streams': 'youtube-streams', 'shorts': 'youtube-shorts'}.get(tab, 'youtube')
        else:
            kind = scope
        if kind not in YOUTUBE_SCOPES or kind == 'youtube-playlist':
            raise ValueError('Gebruik voor een afspeellijst een YouTube-afspeellijstlink met `list=`.')
        suffix = YOUTUBE_SCOPES[kind][1]
        canonical = 'https://www.youtube.com' + '/'.join(parts)
        return kind, canonical + ('/' + suffix if suffix else ''), None
    raise ValueError('Gebruik een YouTube-kanaal, kanaaltab, afspeellijst of losse videolink.')

def ytdlp(args, timeout=240):
    cmd = [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-warnings', '--js-runtimes', 'node', '--socket-timeout', '30', '--retries', '3']
    cookies = db.DATA / 'youtube-cookies.txt'
    if cookies.exists():
        cmd += ['--cookies', str(cookies)]
    result = subprocess.run(cmd + args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Do not expose signed media URLs or cookie paths in the dashboard.
        import re
        error = re.sub(r'https?://\S+', '[URL]', result.stderr[-1500:])
        raise RuntimeError(error.strip() or 'YouTube-download mislukt.')
    return result.stdout

def fetch_public(url, output=None, max_bytes=10*1024**2, user_agent='Audioscraper/1.0', read_timeout=90, client=None):
    # A caller may pass its own client to keep cookies across calls. It must
    # leave redirects to this loop, so every hop stays a checked public address.
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(30, read=read_timeout), follow_redirects=False)
    try:
        for _ in range(8):
            public_url(url)
            with client.stream('GET', url, headers={'User-Agent': user_agent}) as r:
                if r.is_redirect:
                    url = urljoin(url, r.headers['location'])
                    continue
                r.raise_for_status()
                size, chunks = 0, []
                for chunk in r.iter_bytes(128*1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError('Bestand is groter dan de ingestelde limiet.')
                    if output:
                        output.write(chunk)
                    else:
                        chunks.append(chunk)
                return b''.join(chunks)
    finally:
        if own:
            client.close()
    raise ValueError('Te veel doorverwijzingen.')


def image_url(value):
    """Pick an artwork URL from one of feedparser's image representations."""
    if isinstance(value, dict):
        for key in ('href', 'url'):
            if value.get(key):
                return str(value[key])
    if isinstance(value, str) and value:
        return value
    return ''


def youtube_thumbnail(url):
    try:
        _, _, video = canonical_youtube(url)
    except ValueError:
        return ''
    return f'https://i.ytimg.com/vi/{video}/hqdefault.jpg' if video else ''

def insert_episode(item, source_id=None):
    now = time.time()
    ident = uuid.uuid4().hex
    with db.connect() as con:
        con.execute('''INSERT OR IGNORE INTO episodes
        (id,source_id,external_id,url,media_url,kind,title,author,published,published_at,duration,thumbnail_url,created,updated)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (ident, source_id, item['external_id'], item['url'], item.get('media_url'), item['kind'], item['title'], item.get('author'), item.get('published'), db.published_seconds(item.get('published')), item.get('duration'), item.get('thumbnail_url'), now, now))
        return dict(con.execute('SELECT * FROM episodes WHERE external_id=?', (item['external_id'],)).fetchone())

def add_video(url, source_id=None):
    kind, url, vid = canonical_youtube(url)
    if kind != 'video':
        raise ValueError('Dit is geen losse video.')
    return insert_episode({'external_id': 'yt:'+vid, 'url': url, 'kind': 'youtube', 'title': 'YouTube · '+vid, 'thumbnail_url': youtube_thumbnail(url)}, source_id)

def discover(source):
    # Deferred: meetings.py builds on this module's public-URL fetching.
    from . import meetings
    if source['kind'] in meetings.KINDS:
        return meetings.discover(source)
    if source['kind'].startswith('youtube'):
        # No playlist end means all currently public items. This is used only
        # for the explicit “all available” first import; normal sources retain
        # a bounded catalogue to avoid an accidental huge import.
        limit = int(source.get('initial_limit', 3))
        args = ['--flat-playlist', '--extractor-args', 'youtubetab:approximate_date']
        if limit:
            args += ['--playlist-end', str(max(50, limit))]
        args += ['--dump-single-json', source['url']]
        info = json.loads(ytdlp(args, timeout=900 if not limit else 240))
        entries = []
        def videos(node):
            if not node:
                return
            if 'entries' in node:
                for child in node.get('entries') or []:
                    yield from videos(child)
            else:
                yield node
        seen = set()
        for e in videos(info):
            if e.get('live_status') in {'is_live', 'is_upcoming'}:
                continue
            vid = e.get('id', '')
            if len(vid) != 11 or vid in seen:
                continue
            seen.add(vid)
            timestamp = e.get('timestamp') or e.get('release_timestamp') or 0
            published = e.get('upload_date') or e.get('release_date')
            if not published and timestamp:
                try:
                    published = time.strftime('%Y%m%d', time.gmtime(float(timestamp)))
                except (OverflowError, OSError, TypeError, ValueError):
                    pass
            url = f'https://www.youtube.com/watch?v={vid}'
            entries.append({'external_id': 'yt:'+vid, 'url': url, 'kind': 'youtube', 'title': e.get('title') or vid, 'author': e.get('channel') or info.get('channel'), 'published': published, 'duration': e.get('duration'), 'thumbnail_url': e.get('thumbnail') or youtube_thumbnail(url), '_sort': timestamp})
        entries.sort(key=lambda e: e['_sort'], reverse=True)
        if not entries:
            raise ValueError('Geen openbare, afgeronde video’s gevonden voor deze YouTube-bron.')
        channel = info.get('channel') or info.get('uploader') or info.get('title') or source['title']
        return channel, entries
    feed = feedparser.parse(fetch_public(source['url']))
    if not feed.entries and not feed.feed.get('title'):
        raise ValueError('Geen geldige podcast-RSS-feed gevonden.')
    feed_image = image_url(feed.feed.get('image')) or image_url(feed.feed.get('itunes_image'))
    entries = []
    for e in feed.entries:
        enclosure = next((x for x in e.get('enclosures', []) if x.get('href') and (x.get('type','').startswith('audio/') or not x.get('type') or x.get('type') == 'application/octet-stream')), None)
        if not enclosure:
            continue
        media_url = urljoin(source['url'], enclosure['href'])
        ext = e.get('id') or media_url
        key = hashlib.sha256((source['url']+'|'+ext).encode()).hexdigest()
        published = e.get('published') or e.get('updated')
        sort_time = e.get('published_parsed') or e.get('updated_parsed') or ()
        entry_image = image_url(e.get('image')) or image_url(e.get('itunes_image'))
        if not entry_image:
            thumbnails = e.get('media_thumbnail') or []
            entry_image = image_url(thumbnails[0]) if thumbnails else feed_image
        entries.append({'external_id': 'rss:'+key, 'url': e.get('link') or source['url'], 'media_url': media_url, 'kind': 'rss', 'title': e.get('title') or 'Podcastaflevering', 'author': feed.feed.get('title'), 'published': published, 'thumbnail_url': entry_image, '_sort': tuple(sort_time)})
    entries.sort(key=lambda e: e['_sort'], reverse=True)
    return feed.feed.get('title') or source['title'], entries

def sync_source(source):
    if db.processing_paused():
        return
    db.update('sources', source['id'], syncing=1, error=None)
    try:
        title, entries = discover(source)
        if db.processing_paused():
            db.update('sources', source['id'], syncing=0)
            return
        # Mark the older back catalogue as seen on first import; future polls
        # then only enqueue episodes actually published since subscribing.
        known = {r['external_id'] for r in db.rows('SELECT external_id FROM seen WHERE source_id=?', (source['id'],))}
        fresh = [e for e in entries if e['external_id'] not in known]
        selected = fresh if source['initialized'] or not source['initial_limit'] else fresh[:source['initial_limit']]
        for e in selected:
            if db.processing_paused():
                db.update('sources', source['id'], syncing=0)
                return
            insert_episode(e, source['id'])
        # Playlist metadata often has a date before audio is downloaded. Fill
        # it for already-seen queue items without changing their queue state.
        for e in entries:
            if db.processing_paused():
                db.update('sources', source['id'], syncing=0)
                return
            existing = db.one('SELECT id,published,thumbnail_url FROM episodes WHERE external_id=?', (e['external_id'],))
            if existing:
                fields = {}
                if e.get('published') and not existing['published']:
                    fields['published'] = e['published']
                    fields['published_at'] = db.published_seconds(e['published'])
                if e.get('thumbnail_url') and not existing['thumbnail_url']:
                    fields['thumbnail_url'] = e['thumbnail_url']
                if fields:
                    db.update('episodes', existing['id'], **fields)
        if db.processing_paused():
            db.update('sources', source['id'], syncing=0)
            return
        with db.connect() as con:
            con.executemany('INSERT OR IGNORE INTO seen(source_id,external_id) VALUES (?,?)', [(source['id'],e['external_id']) for e in entries])
        db.update('sources', source['id'], title=title, initialized=1, checked=time.time(), error=None, syncing=0)
    except Exception as e:
        db.update('sources', source['id'], checked=time.time(), error=str(e)[:1500], syncing=0)

def transcode(source, target, network=False):
    """Write one compact mono MP3, reading either a local file or a stream.

    Reading straight from the network keeps the video of a meeting recording
    off the disk, so the input is pinned to plain HLS over HTTPS without
    redirects: it may not lead FFmpeg anywhere the source host did not.
    """
    reach = ['-protocol_whitelist', 'https,tls,tcp,crypto,hls', '-max_redirects', '0',
             '-user_agent', 'Audioscraper/1.0', '-reconnect', '1',
             '-reconnect_on_network_error', '1', '-reconnect_delay_max', '60'] if network else []
    proc = subprocess.run(['ffmpeg','-nostdin','-y','-v','error',*reach,'-i',str(source),'-vn','-ac','1','-ar','24000','-b:a','64k',str(target)], capture_output=True, timeout=7200)
    if proc.returncode:
        raise RuntimeError('Audioconversie mislukt: '+proc.stderr.decode(errors='replace')[-500:])

def download(episode):
    folder = db.DATA / 'audio' / episode['id']
    folder.mkdir(parents=True, exist_ok=True)
    if episode['kind'] == 'youtube':
        output = ytdlp(['--no-playlist', '-f', 'bestaudio/best', '--max-filesize', str(MAX_BYTES), '--write-info-json', '-o', str(folder/'audio.%(ext)s'), '--print', 'after_move:filepath', episode['url']], timeout=7200)
        lines = [line for line in output.splitlines() if line.strip()]
        if not lines:
            raise RuntimeError('YouTube heeft geen audiobestand opgeleverd.')
        path = __import__('pathlib').Path(lines[-1])
        meta = folder/'audio.info.json'
        if meta.exists():
            info = json.loads(meta.read_text())
            db.update('episodes', episode['id'], title=info.get('title') or episode['title'], author=info.get('channel'),
                      duration=info.get('duration'), published=info.get('upload_date'),
                      published_at=db.published_seconds(info.get('upload_date')))
            # Metadata can contain temporary signed URLs; keep only selected fields.
            meta.unlink()
    else:
        from . import meetings
        media_url, stream = episode['media_url'], False
        if episode['kind'] == 'meeting':
            # The platforms rebuild their links per visit, so resolve one per attempt.
            media_url, fields, stream = meetings.audio(episode)
            if fields:
                db.update('episodes', episode['id'], **fields)
        if stream:
            # A council recording is a video of gigabytes that is only served
            # as a stream. Take its sound straight off the network so nothing
            # but the finished mp3 ever reaches the disk.
            path = folder/'audio.mp3'
            transcode(public_url(media_url), path, network=True)
        else:
            path = folder/'audio.media'
            partial = folder/'audio.part'
            with partial.open('wb') as out:
                fetch_public(media_url, output=out, max_bytes=MAX_BYTES)
            partial.replace(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError('Het gedownloade audiobestand is leeg.')
    # A single compact, seekable format for uploads and dashboard playback.
    target = folder/'audio.mp3'
    if path != target:
        transcode(path, target)
        path.unlink()
    return str(target)
