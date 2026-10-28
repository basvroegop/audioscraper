"""Persistent, CPU-only discovery of public business telephone listings.

Search snippets are leads, never evidence. A fetched page must associate the
exact number with an organization. Unknowns and transport failures are retried.
"""
import base64
import html
import json
import logging
import re
import time
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit

from . import db
from .netnumbers import net_area
from .sources import fetch_public

log = logging.getLogger('audioscraper.phone_lookup')
BUSINESS_TYPES = {'Organization', 'Corporation', 'LocalBusiness', 'Restaurant',
    'Hotel', 'LodgingBusiness', 'Store', 'FoodEstablishment', 'MedicalBusiness',
    'MedicalOrganization', 'NGO', 'GovernmentOrganization', 'SportsActivityLocation',
    'HealthClub', 'AutoRepair', 'ProfessionalService', 'HomeAndConstructionBusiness',
    'BeautySalon', 'DaySpa', 'FinancialService', 'BankOrCreditUnion', 'Hospital',
    'Dentist', 'LegalService', 'AutomotiveBusiness', 'TravelAgency', 'ShoppingCenter'}
DIRECTORIES = ('telefoon', 'nummer', '180.', 'tellows', 'wieheeft', 'facebook.',
    'instagram.', 'youtube.', 'reddit.', 'trustpilot.', 'cylex.', 'bedrijven',
    'openingstijden', 'nationaletelefoongids', 'nederlandinbedrijf')


def init():
    with db.connect() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS phone_lookups (
            phone TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0, checked REAL, retry_at REAL NOT NULL DEFAULT 0,
            error TEXT, evidence TEXT)''')
        conn.execute("UPDATE phone_lookups SET status='pending' WHERE status='searching'")


def canonical(raw):
    value = re.sub(r'\D', '', str(raw).split(';')[0])
    if value.startswith('0031'):
        value = '0' + value[4:].removeprefix('0')
    elif value.startswith('31'):
        value = '0' + value[2:].removeprefix('0')
    return value


class Page(HTMLParser):
    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.links, self.meta, self.scripts, self.text, self.title = [], {}, [], [], []
        self.script = None
        self.hidden = 0
        self.in_title = False
        self.in_heading = False
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a' and attrs.get('href'):
            self.links.append((attrs['href'], attrs.get('class', '') + (' search-heading' if self.in_heading else '')))
        if tag == 'h2':
            self.in_heading = True
        if tag == 'meta':
            self.meta[attrs.get('property', attrs.get('name', ''))] = attrs.get('content', '')
        if tag == 'script':
            self.script = [] if attrs.get('type', '').lower() == 'application/ld+json' else None
        if tag in {'script', 'style', 'noscript'}:
            self.hidden += 1
        if tag == 'title':
            self.in_title = True

    def handle_endtag(self, tag):
        if tag == 'h2':
            self.in_heading = False
        if tag == 'script' and self.script is not None:
            try:
                self.scripts.append(json.loads(''.join(self.script)))
            except (ValueError, RecursionError):
                pass
            self.script = None
        if tag in {'script', 'style', 'noscript'}:
            self.hidden = max(0, self.hidden - 1)
        if tag == 'title':
            self.in_title = False

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)
        if self.in_title:
            self.title.append(data)
        if not self.hidden:
            self.text.append(data)


def objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)


def address_text(value):
    if isinstance(value, str):
        return value[:250]
    if isinstance(value, dict):
        return ', '.join(str(value[k]) for k in ('streetAddress', 'postalCode', 'addressLocality') if value.get(k))[:250]
    return ''


def evidence_from_page(phone, markup, url):
    page = Page(markup)
    matches = []
    for obj in objects(page.scripts):
        types = obj.get('@type', [])
        types = {types} if isinstance(types, str) else set(types) if isinstance(types, list) else set()
        if not types.intersection(BUSINESS_TYPES):
            continue
        phones = obj.get('telephone', [])
        phones = phones if isinstance(phones, list) else [phones]
        for contact in objects(obj.get('contactPoint', [])):
            phones += [contact.get('telephone', '')]
        if not any(canonical(p) == phone for p in phones):
            continue
        name = obj.get('name') or obj.get('legalName')
        if isinstance(name, str) and 2 <= len(name) <= 160:
            matches.append((name, address_text(obj.get('address')), 'Gestructureerde bedrijfsgegevens'))
    # Conservative fallback for official contact pages without JSON-LD phone data.
    # Never infer a business from a reverse-lookup directory's site name.
    host = urlsplit(url).hostname or ''
    site = page.meta.get('og:site_name', '').strip()
    title = ''.join(page.title).strip()
    text = re.sub(r'\s+', ' ', ' '.join(page.text))
    tel_match = any(href.lower().startswith('tel:') and canonical(unquote(href[4:])) == phone for href, _ in page.links)
    contact_page = bool(re.search(r'contact|over-ons|about|privacy', urlsplit(url).path, re.I))
    postal = re.search(r'\b([A-ZÀ-ÿ][A-Za-zÀ-ÿ .\'-]{2,45}\s\d{1,4}[A-Za-z]?)\s*[,·|]?\s*(\d{4}\s?[A-Z]{2})\s+([A-ZÀ-ÿ][A-Za-zÀ-ÿ\'-]+(?:\s(?:aan|den|de|der|het|op)\s[A-ZÀ-ÿ][A-Za-zÀ-ÿ\'-]+)?)\b', text)
    if not matches and tel_match and site and site.casefold() in title.casefold() and contact_page and postal and not any(d in host for d in DIRECTORIES):
        addresses = {address_text(obj['address']) for obj in objects(page.scripts) if isinstance(obj.get('address'), dict)}
        street = re.sub(r'^(?:'+re.escape(site)+r'\s+|Contact\s+)', '', postal[1], flags=re.I)
        location = next(iter(addresses)) if len(addresses) == 1 else ', '.join((street, postal[2], postal[3]))
        matches.append((site, location, 'Contactpagina · exact telefoonnummer'))
    unique = {(name.casefold(), location) for name, location, _ in matches}
    if len(unique) != 1:
        return None  # Conflicting branches/organizations require review.
    name, location, method = matches[0]
    return {'company': name, 'location': location or None, 'kvk_number': None,
            'source_url': url, 'source_label': f'{host} · automatisch gecontroleerd',
            'verified': time.time(), 'evidence': {'method': method, 'phone': phone, 'company': name, 'location': location}}


def search_links(query, provider):
    base = 'https://lite.duckduckgo.com/lite/' if provider == 'duckduckgo' else 'https://www.bing.com/search'
    markup = fetch_public(base + '?' + urlencode({'q': query}), max_bytes=2*1024**2,
                          user_agent='Mozilla/5.0', read_timeout=20).decode('utf-8', 'replace')
    if any(token in markup.lower() for token in ('anomaly.js', 'challenge-form', 'b_captcha', 'verify you are human')):
        raise RuntimeError('Zoekmachine vraagt tijdelijk om verificatie')
    page = Page(markup)
    results = []
    for href, css in page.links:
        if provider == 'duckduckgo' and 'result-link' not in css:
            continue
        if provider == 'bing' and 'search-heading' not in css:
            continue
        href = urljoin(base, html.unescape(href))
        parsed = urlsplit(href)
        params = parse_qs(parsed.query)
        if provider == 'duckduckgo' and params.get('uddg'):
            href = params['uddg'][0]
        elif provider == 'bing' and parsed.path == '/ck/a' and params.get('u'):
            target = params['u'][0]
            if target.startswith('a1'):
                try:
                    href = base64.urlsafe_b64decode(target[2:] + '=' * (-len(target[2:]) % 4)).decode()
                except (ValueError, UnicodeError):
                    continue
        parsed = urlsplit(href)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
            continue
        if any(parsed.hostname == domain or parsed.hostname.endswith('.'+domain) for domain in ('bing.com', 'microsoft.com', 'duckduckgo.com')):
            continue
        if href not in results:
            results.append(href)
    # An empty recognized result page is a valid no-match. Consent/challenge
    # screens must be retryable errors, not permanently cached misses.
    if not results and not any(token in markup.lower() for token in ('no results', 'geen resultaten', 'b_results', 'no-results')):
        raise RuntimeError('Zoekresultaten tijdelijk niet leesbaar')
    return results[:6]


def queries_for(phone):
    area = net_area(phone)
    prefix = area[0] if area else phone[:2] if phone.startswith('06') else phone[:4] if phone.startswith(('0800', '0900')) else phone[:3]
    spaced = prefix + ' ' + phone[len(prefix):]
    queries = [f'"{spaced}"', f'"{phone}" telefoon']
    # Already recognized organization names provide useful search leads; the
    # final page still has to confirm the exact number independently.
    rows = db.rows('''SELECT DISTINCT e.entities FROM phone_mentions p JOIN episodes e ON e.id=p.episode_id
                      WHERE p.phone=? AND e.entities IS NOT NULL LIMIT 3''', (phone,))
    contexts = ' '.join(r['context'] for r in db.rows('SELECT context FROM phone_mentions WHERE phone=? LIMIT 3', (phone,)))
    reviewed = db.one("SELECT label,description FROM phone_attributions WHERE phone=? AND state='confirmed'", (phone,))
    candidate = reviewed or db.one('''SELECT label,description FROM phone_context_analyses
        WHERE phone=? AND status='completed' AND confidence>=60 AND label<>''
        ORDER BY confidence DESC,analyzed DESC LIMIT 1''', (phone,))
    if candidate and candidate.get('label'):
        label = candidate['label'].replace('"', ' ').strip()
        queries.insert(0, f'"{spaced}" "{label}"')
    for row in rows:
        for entity in json.loads(row['entities'] or '[]'):
            if entity.get('type') == 'organization' and entity.get('name', '').casefold() in contexts.casefold():
                queries.append(f'"{entity["name"]}" {area[1] if area else ""}'.strip())
    # Transcripts are available before entity analysis. Use explicit multiword
    # names or business categories nearby as additional leads in that interval.
    names = re.findall(r'\b(?:[A-Z][a-zA-ZÀ-ÿ]+(?:\s+(?:van|der|de))?\s+){1,4}[A-Z][a-zA-ZÀ-ÿ]+\b', contexts)
    if names:
        name = re.sub(r'^(?:(?:Bel|Welkom|Bij|Met|Hallo|Oké|Ja|Nee|Het|De|Een)\s+)+', '', names[0])
        queries.append(f'"{name}" {area[1] if area else ""}'.strip())
    elif area:
        category = re.search(r'\b(sportschool|zonnestudio|massagesalon|bergingsbedrijf|thuiszorg|nachtwinkel|stucadoor|pizzeria)\b', contexts, re.I)
        if category:
            queries.append(f'{category.group()} {area[1]} contact')
    contextual = queries[2:] + queries[:2]
    if candidate and candidate.get('label'):
        contextual = queries[:1] + queries[3:] + queries[1:3]
    return list(dict.fromkeys(contextual))[:3]


def discover(phone):
    errors, successes, pages, fetched = [], 0, 0, set()
    for query in queries_for(phone):
        for provider in ('duckduckgo', 'bing'):
            try:
                links = search_links(query, provider)
                successes += 1
            except Exception as error:
                errors.append(str(error))
                continue
            for url in links:
                if url in fetched or pages >= 10:
                    continue
                fetched.add(url)
                if urlsplit(url).path.lower().endswith(('.pdf', '.jpg', '.png')):
                    continue
                try:
                    markup = fetch_public(url, max_bytes=2*1024**2, read_timeout=20).decode('utf-8', 'replace')
                    pages += 1
                    result = evidence_from_page(phone, markup, url)
                    if result:
                        return result
                    # Search engines often return the homepage; also inspect
                    # its own contact page, without crawling unrelated sites.
                    for href, _ in Page(markup).links:
                        contact = urljoin(url, href)
                        if (contact not in fetched and pages < 10
                                and urlsplit(contact).hostname == urlsplit(url).hostname
                                and re.search(r'/contact(?:/|$|\.|-)', urlsplit(contact).path, re.I)):
                            fetched.add(contact)
                            pages += 1
                            body = fetch_public(contact, max_bytes=2*1024**2, read_timeout=20).decode('utf-8', 'replace')
                            result = evidence_from_page(phone, body, contact)
                            if result:
                                return result
                            break
                except Exception as error:
                    errors.append(str(error))
    if not successes or (fetched and not pages):
        raise RuntimeError('Openbare zoekbronnen tijdelijk niet bereikbaar; wordt opnieuw geprobeerd')
    return None


def lookup_once():
    if db.processing_paused():
        return False
    now = time.time()
    with db.connect() as conn:
        conn.execute('''INSERT OR IGNORE INTO phone_lookups(phone)
            SELECT DISTINCT p.phone FROM phone_mentions p
            LEFT JOIN phone_verifications v ON v.phone=p.phone WHERE v.phone IS NULL''')
        row = conn.execute('''SELECT l.* FROM phone_lookups l
            WHERE l.retry_at<=? AND NOT EXISTS (SELECT 1 FROM phone_verifications v WHERE v.phone=l.phone)
            AND EXISTS (SELECT 1 FROM phone_mentions p WHERE p.phone=l.phone)
            ORDER BY l.attempts, (l.phone LIKE '06%'), l.phone LIMIT 1''', (now,)).fetchone()
        if not row:
            return False
        phone, attempts = row['phone'], row['attempts'] + 1
        conn.execute("UPDATE phone_lookups SET status='searching',attempts=?,error=NULL WHERE phone=?", (attempts, phone))
    result, error = None, None
    try:
        result = discover(phone)
        if result:
            status, delay = 'matched', 0
        else:
            status = 'not_found'
            delay = 90*86400 if phone.startswith('06') and attempts >= 2 else 7*86400
    except Exception:
        log.exception('Company lookup failed for a phone number')
        status, delay = 'error', min(86400, 900 * 2**min(attempts-1, 6))
        error = 'Zoeken tijdelijk niet beschikbaar; automatische herhaalpoging volgt'
    # Commit verification and lookup state together; never overwrite a
    # separately reviewed mapping that appeared while the network was slow.
    with db.connect() as conn:
        if result:
            conn.execute('''INSERT OR IGNORE INTO phone_verifications
                (phone,company,kvk_number,location,source_url,source_label,verified) VALUES (?,?,?,?,?,?,?)''',
                (phone, *(result[k] for k in ('company','kvk_number','location','source_url','source_label','verified'))))
        conn.execute('''UPDATE phone_lookups SET status=?,checked=?,retry_at=?,error=?,evidence=? WHERE phone=?''',
            (status, time.time(), time.time()+delay, error, json.dumps(result['evidence'], ensure_ascii=False) if result else None, phone))
    return True


def run(stop, wake=None):
    while not stop.is_set():
        if db.processing_paused():
            if wake:
                wake.wait(1)
                wake.clear()
            else:
                stop.wait(1)
            continue
        try:
            lookup_once()
        except Exception:
            log.exception('Phone lookup queue failed')
        if wake:
            wake.wait(60)
            wake.clear()
        else:
            stop.wait(60)
