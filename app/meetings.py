"""Raadsvergaderingen als bron.

Iedere gemeente publiceert haar vergaderingen via een ander platform. Elk
platform krijgt daarom een eigen adapter met dezelfde drie taken: een geplakte
URL herkennen, het archief uitlezen en bij één vergadering de audio opzoeken.
Ondersteund zijn Company Webcast — dat draait onder meer de portalen op
channel.royalcast.com —, Notubiz, het platform achter de
raadsinformatieportalen op `<gemeente>.raadsinformatie.nl`, en
GemeenteOplossingen, dat op eigen gemeentedomeinen draait en daarom aan zijn
open-data-API wordt herkend.
"""

import calendar
import hashlib
import json
import re
import time
from urllib.parse import urlencode, urlsplit

import httpx

from .sources import fetch_public

COMPANY_WEBCAST_HOSTS = {'channel.royalcast.com', 'channel.companywebcast.com'}
NOTUBIZ_PORTAL = re.compile(r'\.raadsinformatie\.nl$')
# Brontypes die een vergaderarchief zijn, met hun label in het dashboard.
KINDS = {'companywebcast': 'Raadsvergaderingen', 'notubiz': 'Raadsvergaderingen',
         'gemeenteoplossingen': 'Raadsvergaderingen'}
PAGE_SIZE = 100
MAX_PAGES = 40
MAX_JSON = 4 * 1024**2
MAX_PAGE = 4 * 1024**2
ARCHIVE_HELP = ('Gebruik het vergaderarchief van één gemeente, bijvoorbeeld '
                'https://hoogeveen.raadsinformatie.nl/, '
                'https://gemeenteraad.groningen.nl/ of '
                'https://channel.royalcast.com/gemeenteopsterland/#!/archived')


def api_json(url):
    return json.loads(fetch_public(url, max_bytes=MAX_JSON))


def page_text(url):
    return fetch_public(url, max_bytes=MAX_PAGE).decode('utf-8', 'replace')


def canonical(url, scope='auto'):
    """Return (brontype, archief-URL) when this URL is a meeting archive."""
    parts = urlsplit(url)
    host = (parts.hostname or '').lower()
    if host in COMPANY_WEBCAST_HOSTS:
        segments = [segment for segment in parts.path.split('/') if segment]
        if not segments or not CUSTOMER.fullmatch(segments[0]):
            raise ValueError(ARCHIVE_HELP)
        return 'companywebcast', f'https://{host}/{segments[0]}/'
    if NOTUBIZ_PORTAL.search(host):
        # Een Notubiz-portaal is één gemeente; elke pagina erbinnen hoort bij
        # hetzelfde archief, dus de portaalwortel is de bron.
        return 'notubiz', f'https://{host}/'
    # GemeenteOplossingen draait op eigen gemeentedomeinen zonder gemeenschappelijk
    # achtervoegsel. Alleen het portaal zelf kan zijn platform verraden, dus dit
    # is de enige herkenning die het net op gaat.
    portal = gemeenteoplossingen_portal(host)
    if portal:
        return 'gemeenteoplossingen', portal
    if scope == 'meetings':
        raise ValueError('Dit vergaderplatform wordt nog niet ondersteund. ' + ARCHIVE_HELP)
    return None


def discover(source):
    if source['kind'] == 'notubiz':
        return notubiz_discover(source)
    if source['kind'] == 'gemeenteoplossingen':
        return go_discover(source)
    return webcast_discover(source)


def audio(episode):
    """Look up the download link plus metadata for one meeting.

    Every platform builds that link fresh — Company Webcast signs it with a
    short expiry, Notubiz and GemeenteOplossingen write it into the page source
    of the meeting — so it is resolved at download time instead of being stored
    with the episode. The third value says whether only the sound should be
    taken off the network instead of the whole file.
    """
    if str(episode.get('external_id') or '').startswith('ntb:'):
        return notubiz_audio(episode)
    if str(episode.get('external_id') or '').startswith('go:'):
        return go_audio(episode)
    return webcast_audio(episode)


# --- Company Webcast (channel.royalcast.com) ---------------------------------

CUSTOMER = re.compile(r'[a-z0-9][a-z0-9_-]{1,60}', re.I)
WEBCAST_CODE = re.compile(r'[A-Za-z0-9_-]{1,40}/[A-Za-z0-9_-]{1,40}')
DATE = re.compile(r'/Date\((-?\d+)')


def moment(value):
    """Company Webcast dates look like /Date(1790098200000)/."""
    found = DATE.search(str(value or ''))
    return int(found.group(1)) // 1000 if found else 0


def customer_name(item):
    name = str(item.get('customerName') or '').strip()
    return re.sub(r'\s+webcasts?$', '', name, flags=re.I) or name


def webcast_entry(host, item):
    code = str(item.get('code') or '').strip()
    if not WEBCAST_CODE.fullmatch(code):
        return None
    start = moment(item.get('scheduledStart'))
    kind = next((tag.split(':', 1)[1].strip() for tag in item.get('tags') or []
                 if str(tag).lower().startswith('type:')), '')
    return {
        'external_id': 'cwc:' + code,
        'url': f'https://{host}/{code.split("/")[0]}/#!/{code}',
        'kind': 'meeting',
        'title': str(item.get('title') or '').strip() or 'Vergadering',
        'author': kind or customer_name(item),
        'published': time.strftime('%Y%m%d', time.gmtime(start)) if start else None,
        '_sort': start,
    }


def webcast_target(episode):
    """Split a stored meeting episode back into host, customer and webcast code."""
    host = (urlsplit(episode['url']).hostname or '').lower()
    code = str(episode.get('external_id') or '')
    code = code[4:] if code.startswith('cwc:') else ''
    if host not in COMPANY_WEBCAST_HOSTS or not WEBCAST_CODE.fullmatch(code):
        raise ValueError('Deze vergadering heeft geen herkenbare bronverwijzing meer.')
    return host, code.split('/')[0], code


def webcast_discover(source):
    host = (urlsplit(source['url']).hostname or '').lower()
    segments = [segment for segment in urlsplit(source['url']).path.split('/') if segment]
    if host not in COMPANY_WEBCAST_HOSTS or not segments:
        raise ValueError(ARCHIVE_HELP)
    customer = segments[0]
    # Houd de opgehaalde catalogus even groot als bij YouTube: een vaste, ruime
    # bovengrens. Een wisselende omvang zou oudere vergaderingen bij een
    # volgende controle alsnog als nieuw in de wachtrij zetten.
    limit = int(source.get('initial_limit') or 0)
    cap = 0 if not limit else max(PAGE_SIZE, limit)
    entries, title, page = [], '', 0
    while page < MAX_PAGES:
        payload = api_json(f'https://{host}/portal/api/1.0/{customer}/portalwebcasts/?' + urlencode({
            'pageNumber': page, 'pageSize': PAGE_SIZE,
            'status': 'indexed', 'ordering': 'startDesc',
        }))
        items = payload.get('List') or []
        for item in items:
            found = webcast_entry(host, item)
            if found:
                entries.append(found)
            title = title or customer_name(item)
        if not items or not payload.get('HasNextPage') or (cap and len(entries) >= cap):
            break
        page += 1
    if not entries:
        raise ValueError('Geen afgeronde vergaderingen gevonden in dit archief.')
    entries.sort(key=lambda found: found['_sort'], reverse=True)
    return title or customer, entries


def webcast_audio(episode):
    host, customer, code = webcast_target(episode)
    detail = api_json(f'https://{host}/portal/api/1.0/{customer}/webcasts/{code}?method=GET&key=')
    attachments = [item for item in detail.get('attachments') or [] if item.get('location')]
    chosen = next((item for item in attachments
                   if str(item.get('contentType') or '').startswith('audio/')), None)
    chosen = chosen or next((item for item in attachments
                             if str(item.get('contentType') or '') == 'video/mp4'), None)
    if not chosen:
        raise ValueError('Deze vergadering biedt geen downloadbare opname aan.')
    start, end = moment(detail.get('actualStart')), moment(detail.get('actualEnd'))
    fields = {}
    if str(detail.get('title') or '').strip():
        fields['title'] = detail['title'].strip()
    if end > start > 0:
        fields['duration'] = float(end - start)
    return chosen['location'], fields, False


# --- Notubiz (<gemeente>.raadsinformatie.nl) ---------------------------------

NOTUBIZ_API = 'https://api.notubiz.nl'
NOTUBIZ_VERSION = '1.10.8'
NOTUBIZ_MEDIA = re.compile(r'\.notubiz\.nl$')
NOTUBIZ_CODE = re.compile(r'\d{1,9}/\d{1,12}')
ORGANISATION = re.compile(r'organisation[-_]id["\']?\s*[:=]\s*["\']?(\d{1,9})')
# De speler zet zijn bronnen als losse blokken in de paginabron: de live-encoder
# gaat naar encoder_plannings, de opname naar media_files.
MEDIA_BLOCK = re.compile(r'media_file\s*=\s*\{([^{}]*)\};\s*media_files\[')
MEDIA_FIELD = re.compile(r'\b(pubpoint|streamname)\s*:\s*"([^"]*)"')
PLAYER_TITLE = re.compile(r'\btitle\s*:\s*"([^"]+)"')
NOTUBIZ_PER_PAGE = 200
NOTUBIZ_YEARS = 20   # zo ver loopt een eerste volledige import terug
NOTUBIZ_RECENT = 400  # dagen die een vervolgcontrole nog nakijkt
NOTUBIZ_GRACE = 12   # uur na de starttijd voordat de opname er mag zijn
NOTUBIZ_TITLE = 'Vergadering'


def notubiz_api(path, **query):
    return api_json(f'{NOTUBIZ_API}/{path}?' + urlencode(
        {'format': 'json', 'version': NOTUBIZ_VERSION, **query}))


def notubiz_organisation(portal):
    """Elk portaal noemt zijn Notubiz-organisatienummer in de paginabron."""
    found = ORGANISATION.search(page_text(portal))
    if not found:
        raise ValueError('Dit raadsinformatieportaal noemt geen Notubiz-organisatie.')
    return found.group(1)


def notubiz_attribute(item, key=1):
    """Notubiz nummert zijn tekstvelden; 1 is de titel van een vergadering."""
    return next((str(field.get('value') or '').strip()
                 for field in item.get('attributes') or [] if field.get('id') == key), '')


def notubiz_moment(item):
    """De geplande starttijd, in lokale tijd, dus goed genoeg om op te sorteren."""
    planning = (item.get('plannings') or [{}])[0] or {}
    try:
        return calendar.timegm(time.strptime(str(planning.get('start_date') or ''),
                                             '%Y-%m-%d %H:%M:%S'))
    except ValueError:
        return 0


def notubiz_events(organisation, start, end):
    """Alle agendapunten in één tijdvak; de API geeft ze oudste eerst."""
    events, page = [], 1
    while page <= MAX_PAGES:
        payload = notubiz_api('events', organisation_id=organisation, per_page=NOTUBIZ_PER_PAGE,
                              page=page,
                              date_from=time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(start)),
                              date_to=time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(end)))
        events += payload.get('events') or []
        if not (payload.get('pagination') or {}).get('has_more_pages'):
            break
        page += 1
    return events


def notubiz_entry(portal, organisation, meeting, title, author, start):
    return {
        'external_id': f'ntb:{organisation}/{meeting}',
        'url': f'{portal}vergadering/{meeting}',
        'kind': 'meeting',
        'title': title or NOTUBIZ_TITLE,
        'author': author,
        'published': time.strftime('%Y%m%d', time.gmtime(start)) if start else None,
        '_sort': start,
    }


def notubiz_entries(portal, organisation, gremia, event):
    """Eén agendapunt als afleveringen: een raadsavond heeft er meerdere."""
    if event.get('permission_group') != 'public' or not event.get('live'):
        # Niet openbaar, of niet uitgezonden: dan is er ook geen opname.
        return []
    if event.get('canceled') or event.get('inactive'):
        return []
    title = notubiz_attribute(event)
    author = gremia.get((event.get('gremium') or {}).get('id'), '')
    start = notubiz_moment(event)
    if event.get('type') != 'assembly':
        return [notubiz_entry(portal, organisation, event['id'], title, author, start)]
    # Een raadsavond (assembly) is een omslag om de zalen waarin die avond
    # tegelijk wordt vergaderd; de opname hangt aan elke zaal apart. De eigen
    # naam van een zaal staat pas op de vergaderpagina, dus die volgt bij het
    # downloaden; tot dan zijn ze genummerd.
    rooms = (notubiz_api(f'events/assemblies/{event["id"]}').get('assembly') or {}).get('meetings') or []
    return [notubiz_entry(portal, organisation, room['id'],
                          title if len(rooms) == 1 else f'{title} · deel {number}',
                          author, notubiz_moment(room) or start)
            for number, room in enumerate(rooms, 1)]


def notubiz_discover(source):
    portal = source['url']
    if not NOTUBIZ_PORTAL.search((urlsplit(portal).hostname or '').lower()):
        raise ValueError(ARCHIVE_HELP)
    organisation = notubiz_organisation(portal)
    gremia = {group['id']: str(group.get('title') or '').strip()
              for group in notubiz_api(f'organisations/{organisation}/gremia').get('gremia') or []}
    limit = int(source.get('initial_limit') or 0)
    cap = 0 if not limit else max(PAGE_SIZE, limit)
    now = time.time()
    if source.get('initialized'):
        # Een vervolgcontrole kijkt alleen de laatste maanden na. Dat venster
        # valt binnen dat van de eerste import, dus al geziene vergaderingen
        # kunnen niet alsnog als nieuw in de wachtrij komen.
        windows = [(now - NOTUBIZ_RECENT * 86400, now)]
    else:
        # De eerste import loopt jaar voor jaar terug tot de catalogus vol is.
        # Hele jaren houden die omvang stabiel bij een volgende volledige import.
        this_year = time.gmtime(now).tm_year
        windows = [(calendar.timegm((year, 1, 1, 0, 0, 0, 0, 1, 0)),
                    calendar.timegm((year, 12, 31, 23, 59, 59, 0, 365, 0)))
                   for year in range(this_year, this_year - NOTUBIZ_YEARS, -1)]
    # De agenda kondigt een uitzending vooraf aan, dus een vergadering telt pas
    # mee als ze lang genoeg geleden begon om afgelopen en gepubliceerd te zijn.
    edge = now - NOTUBIZ_GRACE * 3600
    entries = []
    for start, end in windows:
        if start >= min(end, edge):
            continue
        for event in notubiz_events(organisation, start, min(end, edge)):
            entries += notubiz_entries(portal, organisation, gremia, event)
        if cap and len(entries) >= cap:
            break
    if not entries:
        raise ValueError('Geen uitgezonden vergaderingen gevonden in dit archief.')
    entries.sort(key=lambda found: found['_sort'], reverse=True)
    title = str((notubiz_api(f'organisations/{organisation}').get('organisation') or {}).get('name') or '')
    return title.strip() or (urlsplit(portal).hostname or ''), entries


def notubiz_stream(page):
    """Bouw de HLS-URL van de opname precies zoals de speler van het portaal dat doet."""
    for block in MEDIA_BLOCK.findall(page):
        fields = dict(MEDIA_FIELD.findall(block))
        pubpoint, streamname = fields.get('pubpoint', ''), fields.get('streamname', '')
        if not pubpoint or not streamname:
            continue
        stream = re.sub(r'^rtmps?://', 'https://', pubpoint) + '/' + streamname + '/playlist.m3u8'
        if not NOTUBIZ_MEDIA.search((urlsplit(stream).hostname or '').lower()):
            raise ValueError('Deze vergadering verwijst naar een onbekende mediaserver.')
        return stream
    raise ValueError('Deze vergadering biedt geen opname aan.')


def notubiz_audio(episode):
    host = (urlsplit(episode['url']).hostname or '').lower()
    code = str(episode.get('external_id') or '')[4:]
    if not NOTUBIZ_PORTAL.search(host) or not NOTUBIZ_CODE.fullmatch(code):
        raise ValueError('Deze vergadering heeft geen herkenbare bronverwijzing meer.')
    page = page_text(episode['url'])
    stream = notubiz_stream(page)
    # De speler kent de volledige naam van deze zaal of vergadering wél.
    found = PLAYER_TITLE.search(page, max(0, page.find('createPlayer')))
    fields = {'title': found.group(1).strip()} if found else {}
    # Een raadsopname is video van gigabytes: alleen het geluid halen.
    return stream, fields, True


# --- GemeenteOplossingen (eigen gemeentedomein) ------------------------------

GO_API = 'api/v2/'
GO_MEDIA = re.compile(r'https://[a-z0-9.-]+\.gemeenteoplossingen\.nl/[^"\'\s]+\.mp3')
GO_PER_PAGE = 100
GO_MAX_MEETINGS = 5000
GO_RECENT = 400  # dagen die een vervolgcontrole nog nakijkt
GO_GRACE = 12    # uur na de starttijd voordat de opname er mag zijn
GO_DELAY = 2     # Crawl-delay die het portaal in robots.txt vraagt
# Anubis is de poort die dit portaal voor elke bezoeker neerzet: reken eerst een
# hash uit met een aantal nullen voorop, en je mag erlangs. Dat is geen
# geheim en geen inlog — een browser doet het onzichtbaar — maar het houdt
# wel gewone clients tegen, dus de agenda komt uit de open-data-API en alleen
# het opzoeken van de opname gaat hierlangs.
ANUBIS = re.compile(r'<script id="anubis_challenge" type="application/json">(.*?)</script>', re.S)
ANUBIS_PASS = '/.within.website/x/cmd/anubis/api/pass-challenge'
ANUBIS_MAX_NONCE = 1 << 24
ANUBIS_COOKIES = {}


def go_api(portal, path, read_timeout=90, **query):
    """De open-data-API van het portaal; die staat los van de poort."""
    url = portal.rstrip('/') + '/' + GO_API + path
    if query:
        url += '?' + urlencode(query)
    return json.loads(fetch_public(url, max_bytes=MAX_JSON, read_timeout=read_timeout))


def gemeenteoplossingen_portal(host):
    """Herken het platform aan zijn open-data-API, of geef niets terug."""
    if not host:
        return None
    portal = f'https://{host}/'
    try:
        result = (go_api(portal, 'meetings', limit=1, read_timeout=10).get('result') or {})
    except Exception:
        # Elk ander portaal, elke feed en elk netwerkprobleem valt hier terug
        # op "dit is het niet"; de beller bepaalt wat er dan gebeurt.
        return None
    return portal if isinstance(result.get('meetings'), list) else None


def anubis_nonce(data, difficulty):
    """Zoek de nonce waarvan de SHA-256 met `difficulty` nullen begint."""
    whole, odd = difficulty // 2, difficulty % 2
    for nonce in range(ANUBIS_MAX_NONCE):
        digest = hashlib.sha256((data + str(nonce)).encode()).digest()
        if not any(digest[:whole]) and not (odd and digest[whole] >> 4):
            return digest.hex(), nonce
    raise ValueError('De toegangspoort van dit portaal gaf een te zware opgave.')


def go_page(url):
    """Haal één vergaderpagina op, desnoods via de poort.

    De cookie die de poort teruggeeft blijft per portaal bewaard, zodat er niet
    bij iedere vergadering opnieuw hoeft te worden aangeklopt.
    """
    host = (urlsplit(url).hostname or '').lower()
    with httpx.Client(timeout=httpx.Timeout(30, read=90), follow_redirects=False,
                      cookies=ANUBIS_COOKIES.get(host) or {}) as client:
        page = fetch_public(url, max_bytes=MAX_PAGE, client=client).decode('utf-8', 'replace')
        found = ANUBIS.search(page)
        if found:
            puzzle = json.loads(found.group(1))
            challenge, rules = puzzle['challenge'], puzzle['rules']
            started = time.time()
            response, nonce = anubis_nonce(str(challenge['randomData']), int(rules['difficulty']))
            passed = urlsplit(url)._replace(path=ANUBIS_PASS, fragment='', query=urlencode({
                'id': challenge['id'], 'response': response, 'nonce': nonce, 'redir': url,
                'elapsedTime': int((time.time() - started) * 1000)})).geturl()
            page = fetch_public(passed, max_bytes=MAX_PAGE, client=client).decode('utf-8', 'replace')
            if ANUBIS.search(page):
                raise ValueError('De toegangspoort van dit portaal liet ons niet door.')
        ANUBIS_COOKIES[host] = dict(client.cookies)
    return page


def go_moment(meeting):
    stamp = '%s %s' % (meeting.get('date') or '', meeting.get('startTime') or '00:00')
    try:
        return calendar.timegm(time.strptime(stamp, '%Y-%m-%d %H:%M'))
    except ValueError:
        return 0


def go_entry(portal, host, meeting):
    body = str((meeting.get('dmu') or {}).get('name') or '').strip()
    return {
        'external_id': f'go:{host}/{meeting["id"]}',
        'url': portal.rstrip('/') + str(meeting.get('url') or ''),
        'kind': 'meeting',
        'title': str(meeting.get('meetingLabel') or body or 'Vergadering').strip(),
        'author': body,
        'published': str(meeting.get('date') or '').replace('-', '') or None,
        '_sort': go_moment(meeting),
    }


def go_discover(source):
    portal = source['url']
    host = (urlsplit(portal).hostname or '').lower()
    limit = int(source.get('initial_limit') or 0)
    cap = 0 if not limit else max(PAGE_SIZE, limit)
    # De agenda kondigt vergaderingen vooruit aan en de opname verschijnt pas na
    # afloop, dus de laatste uren blijven nog even buiten beschouwing.
    edge = time.time() - GO_GRACE * 3600
    window = {'sort': 'date_desc', 'date_to': time.strftime('%Y-%m-%d', time.gmtime(edge))}
    if source.get('initialized'):
        # Een vervolgcontrole kijkt alleen de laatste maanden na. Dat venster
        # valt binnen dat van de eerste import, dus al geziene vergaderingen
        # kunnen niet alsnog als nieuw in de wachtrij komen.
        window['date_from'] = time.strftime('%Y-%m-%d', time.gmtime(edge - GO_RECENT * 86400))
    entries, offset = [], 0
    while len(entries) < GO_MAX_MEETINGS:
        if offset:
            time.sleep(GO_DELAY)
        result = go_api(portal, 'meetings', offset=offset, limit=GO_PER_PAGE, **window).get('result') or {}
        meetings = result.get('meetings') or []
        entries += [go_entry(portal, host, meeting) for meeting in meetings if meeting.get('id')]
        offset += len(meetings)
        if not meetings or offset >= int(result.get('totalCount') or 0) or (cap and len(entries) >= cap):
            break
    if not entries:
        raise ValueError('Geen afgeronde vergaderingen gevonden in dit archief.')
    entries.sort(key=lambda found: found['_sort'], reverse=True)
    return host, entries


def go_audio(episode):
    host = (urlsplit(episode['url']).hostname or '').lower()
    if not str(episode.get('external_id') or '').startswith(f'go:{host}/'):
        raise ValueError('Deze vergadering heeft geen herkenbare bronverwijzing meer.')
    found = GO_MEDIA.search(go_page(episode['url']))
    if not found:
        # Lang niet iedere vergadering wordt uitgezonden; de agenda verraadt
        # vooraf niet welke, dus dat blijkt hier.
        raise ValueError('Deze vergadering biedt geen downloadbare opname aan.')
    # Dit is een gewoon mp3-bestand op een open mediaserver: normaal ophalen.
    return found.group(0), {}, False
