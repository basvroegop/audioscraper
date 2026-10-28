"""Persistent, low-priority context analysis for unmatched mobile numbers."""
import json
import logging
import re
import time

from . import db


log = logging.getLogger('audioscraper.phone_context')
ANALYSIS_VERSION = 1
SUBJECT_TYPES = {
    'person', 'organization', 'service', 'private_contact',
    'own_number', 'invalid', 'unknown',
}
RELATIONS = {
    'called_target', 'submitted_target', 'caller_own_number',
    'third_party_reference', 'invalid_number', 'unclear',
}


def init():
    with db.connect() as connection:
        connection.execute("UPDATE phone_context_analyses SET status='pending' WHERE status='searching'")
        _seed_candidates(connection)


def _seed_candidates(connection):
    connection.execute('''UPDATE phone_context_analyses SET
        version=?,status='pending',attempts=0,retry_at=0,subject_type=NULL,
        relation=NULL,label=NULL,description=NULL,confidence=NULL,evidence=NULL,
        model=NULL,analyzed=NULL,error=NULL
        WHERE version<?''', (ANALYSIS_VERSION, ANALYSIS_VERSION))
    connection.execute('''INSERT OR IGNORE INTO phone_context_analyses
        (phone,episode_id,segment,version,status)
        SELECT p.phone,p.episode_id,p.segment,?,'pending'
        FROM phone_mentions p
        JOIN episodes e ON e.id=p.episode_id
        JOIN phone_lookups l ON l.phone=p.phone
        LEFT JOIN phone_verifications v ON v.phone=p.phone
        WHERE p.category='Mobiel nummer' AND v.phone IS NULL
          AND l.status='not_found' AND e.entities IS NOT NULL''', (ANALYSIS_VERSION,))


def _timecode(seconds):
    value = max(0, int(float(seconds or 0)))
    hours, remainder = divmod(value, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}' if hours else f'{minutes:02d}:{seconds:02d}'


def context_window(segments, target_segment, raw='', before=8, after=12, max_chars=5000):
    """Return a bounded window while visibly marking and masking the target number."""
    if not 0 <= target_segment < len(segments):
        raise ValueError('Transcriptsegment voor nummeranalyse bestaat niet meer.')
    first = max(0, target_segment - before)
    last = min(len(segments), target_segment + after + 1)
    lines = []
    for index in range(first, last):
        segment = segments[index]
        text = re.sub(r'\s+', ' ', str(segment.get('text', ''))).strip()
        if index == target_segment:
            if raw:
                text = re.sub(re.escape(raw), '[DOELNUMMER]', text, count=1, flags=re.I)
            if '[DOELNUMMER]' not in text:
                text = '[DOELNUMMER] ' + text
            marker = 'DOELSEGMENT'
        else:
            marker = 'CONTEXT'
        speaker = f" · {segment['speaker']}" if segment.get('speaker') else ''
        lines.append(f'[{marker} {index} · {_timecode(segment.get("start", 0))}{speaker}] {text}')
    joined = '\n'.join(lines)
    if len(joined) <= max_chars:
        return joined, first, last
    marker = joined.find('[DOELSEGMENT')
    start = max(0, marker - max_chars // 2)
    end = min(len(joined), start + max_chars)
    start = max(0, end - max_chars)
    return joined[start:end], first, last


def nearby_entities(entities, first_segment, last_segment):
    result, seen = [], set()
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        occurs = False
        for occurrence in entity.get('occurrences', []):
            spans = occurrence.get('spans') or [occurrence]
            if any(first_segment <= span.get('segment', -1) < last_segment for span in spans):
                occurs = True
                break
        name, kind = str(entity.get('name', '')).strip(), entity.get('type')
        key = (name.casefold(), kind)
        if occurs and name and key not in seen:
            result.append({'name': name, 'type': kind})
            seen.add(key)
    return result[:80]


def _validated(result, text):
    if not isinstance(result, dict):
        raise ValueError('Worker gaf geen geldige nummeranalyse terug.')
    subject_type = str(result.get('subject_type', ''))
    relation = str(result.get('relation', ''))
    label = ' '.join(str(result.get('label', '')).split()).strip(' .,;:-')[:160]
    description = ' '.join(str(result.get('description', '')).split())[:300]
    evidence = ' '.join(str(result.get('evidence', '')).split())[:600]
    try:
        confidence = max(0, min(100, int(result.get('confidence', 0))))
    except (TypeError, ValueError) as error:
        raise ValueError('Worker gaf geen geldige betrouwbaarheid terug.') from error
    normalized = ' '.join(text.split()).casefold()
    if subject_type not in SUBJECT_TYPES or relation not in RELATIONS:
        raise ValueError('Worker gaf een onbekende nummerclassificatie terug.')
    if evidence and ' '.join(evidence.split()).casefold() not in normalized:
        raise ValueError('Bewijsfragment staat niet letterlijk in het transcript.')
    if label and not re.search(r'(?<!\w)' + re.escape(label) + r'(?!\w)', text, re.I):
        raise ValueError('Voorgestelde naam staat niet letterlijk in het transcript.')
    if subject_type in {'person', 'organization'} and not label:
        raise ValueError('Een persoons- of organisatiekandidaat mist een letterlijke naam.')
    return {
        'subject_type': subject_type,
        'relation': relation,
        'label': label,
        'description': description,
        'confidence': confidence,
        'evidence': evidence,
        'model': str(result.get('model', ''))[:160] or None,
    }


def analyze_once(analyzer):
    """Analyze one eligible mention and persist success or a retryable error."""
    if db.processing_paused():
        return False
    now = time.time()
    with db.connect() as connection:
        _seed_candidates(connection)
        row = connection.execute('''SELECT a.*,p.raw,e.segments,e.entities
            FROM phone_context_analyses a
            JOIN phone_mentions p ON p.phone=a.phone AND p.episode_id=a.episode_id AND p.segment=a.segment
            JOIN episodes e ON e.id=a.episode_id
            JOIN phone_lookups l ON l.phone=a.phone
            LEFT JOIN phone_verifications v ON v.phone=a.phone
            WHERE a.status IN ('pending','error') AND a.retry_at<=?
              AND l.status='not_found' AND v.phone IS NULL
            ORDER BY a.attempts,a.phone,a.episode_id,a.segment LIMIT 1''', (now,)).fetchone()
        if not row:
            return False
        row = dict(row)
        attempts = row['attempts'] + 1
        connection.execute('''UPDATE phone_context_analyses
            SET status='searching',attempts=?,error=NULL
            WHERE phone=? AND episode_id=? AND segment=?''',
            (attempts, row['phone'], row['episode_id'], row['segment']))

    try:
        segments = json.loads(row['segments'] or '[]')
        entities = json.loads(row['entities'] or '[]')
        text, first, last = context_window(segments, row['segment'], row['raw'])
        result = _validated(analyzer(text, nearby_entities(entities, first, last)), text)
    except Exception:
        log.exception('Phone context analysis failed')
        delay = min(86400, 300 * 2 ** min(attempts - 1, 8))
        with db.connect() as connection:
            connection.execute('''UPDATE phone_context_analyses
                SET status='error',retry_at=?,error=?
                WHERE phone=? AND episode_id=? AND segment=?''',
                (time.time() + delay, 'Contextanalyse tijdelijk niet beschikbaar; herhaalpoging volgt',
                 row['phone'], row['episode_id'], row['segment']))
        return True

    with db.connect() as connection:
        connection.execute('''UPDATE phone_context_analyses SET
            status='completed',retry_at=0,subject_type=?,relation=?,label=?,description=?,
            confidence=?,evidence=?,model=?,analyzed=?,error=NULL
            WHERE phone=? AND episode_id=? AND segment=?''',
            (result['subject_type'], result['relation'], result['label'], result['description'],
             result['confidence'], result['evidence'], result['model'], time.time(),
             row['phone'], row['episode_id'], row['segment']))
        if result['label'] and result['confidence'] >= 60:
            connection.execute("UPDATE phone_lookups SET retry_at=0 WHERE phone=? AND status='not_found'",
                               (row['phone'],))
    return True


def run(stop, wake, analyzer, available):
    while not stop.is_set():
        if db.processing_paused():
            wake.wait(3)
            wake.clear()
            continue
        try:
            with db.connect() as connection:
                _seed_candidates(connection)
        except Exception:
            log.exception('Could not seed phone context queue')
        if not available():
            wake.wait(3)
            wake.clear()
            continue
        try:
            worked = analyze_once(analyzer)
        except Exception:
            log.exception('Phone context queue failed')
            worked = False
        wake.wait(5 if worked else 15)
        wake.clear()
