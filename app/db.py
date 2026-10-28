import calendar
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

DATA = Path(os.getenv('DATA_DIR', './data'))

# Every source is one project. The accent only drives colour in the dashboard,
# so a project stays recognisable while switching between environments.
ACCENTS = ('moss', 'teal', 'clay', 'indigo', 'amber', 'plum', 'rust', 'slate')

@contextmanager
def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATA / 'audioscraper.sqlite3', timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def init():
    with connect() as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''
        CREATE TABLE IF NOT EXISTS sources (
          id TEXT PRIMARY KEY, url TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
          title TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
          initial_limit INTEGER NOT NULL DEFAULT 3, initialized INTEGER NOT NULL DEFAULT 0,
          created REAL NOT NULL, checked REAL, error TEXT, syncing INTEGER NOT NULL DEFAULT 0,
          name TEXT, description TEXT, accent TEXT,
          priority INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS seen (
          source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
          external_id TEXT NOT NULL, PRIMARY KEY(source_id,external_id)
        );
        CREATE TABLE IF NOT EXISTS episodes (
          id TEXT PRIMARY KEY, source_id TEXT REFERENCES sources(id) ON DELETE SET NULL,
          external_id TEXT NOT NULL UNIQUE, url TEXT NOT NULL, media_url TEXT,
          kind TEXT NOT NULL, title TEXT NOT NULL, author TEXT, published TEXT,
          duration REAL, thumbnail_url TEXT, status TEXT NOT NULL DEFAULT 'queued', stage TEXT NOT NULL DEFAULT 'In wachtrij',
          created REAL NOT NULL, updated REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          retry_at REAL NOT NULL DEFAULT 0, error TEXT, audio_path TEXT,
          transcript TEXT, segments TEXT, language TEXT, entities TEXT, entity_chunks TEXT,
          speaker_error TEXT, speaker_requested INTEGER NOT NULL DEFAULT 0,
          published_at REAL
        );
        CREATE INDEX IF NOT EXISTS episodes_status ON episodes(status,retry_at);
        CREATE TABLE IF NOT EXISTS phone_mentions (
          phone TEXT NOT NULL,
          episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
          segment INTEGER NOT NULL,
          start REAL NOT NULL DEFAULT 0,
          raw TEXT NOT NULL,
          display TEXT NOT NULL,
          category TEXT NOT NULL,
          context TEXT NOT NULL,
          PRIMARY KEY(phone, episode_id, segment)
        );
        CREATE INDEX IF NOT EXISTS phone_mentions_phone ON phone_mentions(phone);
        CREATE TABLE IF NOT EXISTS phone_scans (
          episode_id TEXT PRIMARY KEY REFERENCES episodes(id) ON DELETE CASCADE,
          scanned REAL NOT NULL,
          version INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS phone_verifications (
          phone TEXT PRIMARY KEY,
          company TEXT NOT NULL,
          kvk_number TEXT,
          location TEXT,
          source_url TEXT NOT NULL,
          source_label TEXT NOT NULL,
          verified REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS phone_lookups (
          phone TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0, checked REAL, retry_at REAL NOT NULL DEFAULT 0,
          error TEXT, evidence TEXT
        );
        CREATE TABLE IF NOT EXISTS phone_context_analyses (
          phone TEXT NOT NULL,
          episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
          segment INTEGER NOT NULL,
          version INTEGER NOT NULL DEFAULT 1,
          status TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0,
          retry_at REAL NOT NULL DEFAULT 0,
          subject_type TEXT,
          relation TEXT,
          label TEXT,
          description TEXT,
          confidence INTEGER,
          evidence TEXT,
          model TEXT,
          analyzed REAL,
          error TEXT,
          PRIMARY KEY(phone, episode_id, segment)
        );
        CREATE INDEX IF NOT EXISTS phone_context_status
          ON phone_context_analyses(status,retry_at);
        CREATE TABLE IF NOT EXISTS phone_attributions (
          phone TEXT PRIMARY KEY,
          state TEXT NOT NULL,
          subject_type TEXT,
          relation TEXT,
          label TEXT,
          description TEXT,
          confidence INTEGER,
          evidence TEXT,
          episode_id TEXT REFERENCES episodes(id) ON DELETE SET NULL,
          segment INTEGER,
          source TEXT NOT NULL DEFAULT 'manual',
          reviewed REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS app_settings (
          key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        ''')
        db.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES ('processing_paused','0')")
        phone_scan_columns = {row['name'] for row in db.execute('PRAGMA table_info(phone_scans)')}
        if 'version' not in phone_scan_columns:
            db.execute('ALTER TABLE phone_scans ADD COLUMN version INTEGER NOT NULL DEFAULT 1')
        verification_columns = {row['name'] for row in db.execute('PRAGMA table_info(phone_verifications)')}
        if 'location' not in verification_columns:
            db.execute('ALTER TABLE phone_verifications ADD COLUMN location TEXT')
        episode_columns = {row['name'] for row in db.execute('PRAGMA table_info(episodes)')}
        if 'thumbnail_url' not in episode_columns:
            db.execute('ALTER TABLE episodes ADD COLUMN thumbnail_url TEXT')
        if 'speaker_error' not in episode_columns:
            db.execute('ALTER TABLE episodes ADD COLUMN speaker_error TEXT')
        if 'speaker_requested' not in episode_columns:
            db.execute('ALTER TABLE episodes ADD COLUMN speaker_requested INTEGER NOT NULL DEFAULT 0')
        if 'published_at' not in episode_columns:
            # A sortable publication moment: the stored text comes in three
            # different formats, and the queue and library both order on it.
            db.execute('ALTER TABLE episodes ADD COLUMN published_at REAL')
            for row in db.execute('SELECT id,published FROM episodes WHERE published IS NOT NULL').fetchall():
                seconds = published_seconds(row['published'])
                if seconds:
                    db.execute('UPDATE episodes SET published_at=? WHERE id=?', (seconds, row['id']))
        source_columns = {row['name'] for row in db.execute('PRAGMA table_info(sources)')}
        for column in ('name', 'description', 'accent'):
            if column not in source_columns:
                db.execute(f'ALTER TABLE sources ADD COLUMN {column} TEXT')
        if 'priority' not in source_columns:
            db.execute('ALTER TABLE sources ADD COLUMN priority INTEGER NOT NULL DEFAULT 0')
            renumber(db)
        adopt_loose_episodes(db)
        assign_missing_accents(db)
        db.execute("UPDATE episodes SET status='queued',stage='Hervatten na herstart' WHERE status IN ('downloading','transcribing','analyzing')")
        db.execute('UPDATE sources SET syncing=0')


def adopt_loose_episodes(db):
    """Give every episode a project, including videos added before projects.

    A video that was added on its own before its channel was followed is still
    part of that channel: the source has it in `seen` but never overwrote the
    existing row. Such an episode belongs to that source, not to a project of
    its own. Only a video no source ever discovered becomes its own project.
    """
    for episode in db.execute('SELECT id,url,title,kind,external_id FROM episodes WHERE source_id IS NULL').fetchall():
        owner = db.execute('SELECT source_id FROM seen WHERE external_id=? LIMIT 1',
                           (episode['external_id'],)).fetchone()
        if owner:
            db.execute('UPDATE episodes SET source_id=? WHERE id=?', (owner['source_id'], episode['id']))
            continue
        existing = db.execute('SELECT id FROM sources WHERE url=?', (episode['url'],)).fetchone()
        ident = existing['id'] if existing else uuid.uuid4().hex
        if not existing:
            db.execute('''INSERT INTO sources(id,url,kind,title,name,initial_limit,initialized,created,checked)
            VALUES (?,?,?,?,?,0,1,?,?)''', (ident, episode['url'], 'video', episode['title'],
                                            episode['title'], time.time(), time.time()))
        db.execute('UPDATE episodes SET source_id=? WHERE id=?', (ident, episode['id']))


def assign_missing_accents(db):
    for source in db.execute("SELECT id FROM sources WHERE accent IS NULL OR accent=''").fetchall():
        db.execute('UPDATE sources SET accent=? WHERE id=?', (next_accent(db), source['id']))


def next_accent(db):
    """Pick the least used accent so neighbouring projects stay distinguishable."""
    used = [row['accent'] for row in db.execute("SELECT accent FROM sources WHERE accent IS NOT NULL")]
    return min(ACCENTS, key=lambda accent: (used.count(accent), ACCENTS.index(accent)))

def published_seconds(value):
    """Read a publication date in any of the formats the sources hand us."""
    text = str(value or '').strip()
    if not text:
        return None
    if re.fullmatch(r'[12]\d{7}', text):  # YouTube and vergaderingen: YYYYMMDD
        try:
            return float(calendar.timegm(time.strptime(text, '%Y%m%d')))
        except ValueError:
            return None
    for parse in (parsedate_to_datetime, datetime.fromisoformat):  # RSS: RFC 822 or ISO
        try:
            moment = parse(text)
        except (TypeError, ValueError):
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.timestamp()
    return None


def rows(sql, args=()):
    with connect() as db:
        return [dict(r) for r in db.execute(sql, args).fetchall()]

def one(sql, args=()):
    result = rows(sql, args)
    return result[0] if result else None


def processing_paused():
    setting = one("SELECT value FROM app_settings WHERE key='processing_paused'")
    return bool(setting and setting['value'] == '1')


def set_processing_paused(paused):
    with connect() as db:
        db.execute("INSERT INTO app_settings(key,value) VALUES ('processing_paused',?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", ('1' if paused else '0',))

def update(table, ident, **fields):
    if table == 'episodes':
        fields['updated'] = time.time()
    with connect() as db:
        db.execute(f"UPDATE {table} SET {','.join(k+'=?' for k in fields)} WHERE id=?", (*fields.values(), ident))


def replace_phone_mentions(episode_id, mentions, scan_version=1):
    """Replace the deterministic phone-number scan for one transcript."""
    with connect() as db:
        db.execute('DELETE FROM phone_context_analyses WHERE episode_id=?', (episode_id,))
        db.execute('DELETE FROM phone_mentions WHERE episode_id=?', (episode_id,))
        db.executemany('''INSERT INTO phone_mentions(phone,episode_id,segment,start,raw,display,category,context)
        VALUES (:phone,:episode_id,:segment,:start,:raw,:display,:category,:context)''', [
            {**mention, 'episode_id': episode_id} for mention in mentions
        ])
        db.execute('INSERT OR REPLACE INTO phone_scans(episode_id,scanned,version) VALUES (?,?,?)', (episode_id, time.time(), scan_version))


def put_phone_verification(phone, company, kvk_number, location, source_url, source_label, verified):
    with connect() as db:
        db.execute('''INSERT INTO phone_verifications(phone,company,kvk_number,location,source_url,source_label,verified)
        VALUES (?,?,?,?,?,?,?) ON CONFLICT(phone) DO UPDATE SET
        company=excluded.company,kvk_number=excluded.kvk_number,location=excluded.location,source_url=excluded.source_url,
        source_label=excluded.source_label,verified=excluded.verified
        WHERE excluded.verified >= phone_verifications.verified''',
        (phone, company, kvk_number, location, source_url, source_label, verified))


def renumber(db):
    """Keep the processing order stored as 0..n-1, newest project first on a tie."""
    rows = db.execute('SELECT id FROM sources ORDER BY priority,created DESC').fetchall()
    for position, row in enumerate(rows):
        db.execute('UPDATE sources SET priority=? WHERE id=?', (position, row['id']))
    return [row['id'] for row in rows]


def set_project_order(ids):
    """Store an explicit processing order; projects left out keep their place after it."""
    with connect() as db:
        known = [row['id'] for row in db.execute('SELECT id FROM sources ORDER BY priority,created DESC')]
        chosen = [ident for ident in dict.fromkeys(ids) if ident in set(known)]
        order = chosen + [ident for ident in known if ident not in set(chosen)]
        for position, ident in enumerate(order):
            db.execute('UPDATE sources SET priority=? WHERE id=?', (position, ident))
        return order


def create_project(url, kind, title, name=None, description='', initial_limit=3, initialized=0, checked=None):
    """Create the one project that belongs to a newly added source.

    A new project starts at the front of the queue: you just added it, so its
    work is what you are waiting for. Reorder it afterwards if that is wrong.
    """
    ident = uuid.uuid4().hex
    with connect() as db:
        top = db.execute('SELECT MIN(priority) first FROM sources').fetchone()['first']
        db.execute('''INSERT INTO sources(id,url,kind,title,name,description,initial_limit,initialized,created,checked,accent,priority)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''', (ident, url, kind, title, name or None, description or None,
                                              initial_limit, initialized, time.time(), checked, next_accent(db),
                                              (top - 1) if top is not None else 0))
        renumber(db)
    return ident


def delete_project(ident):
    """Remove a project with every transcript, entity and scan inside it."""
    with connect() as db:
        episodes = [row['id'] for row in db.execute('SELECT id FROM episodes WHERE source_id=?', (ident,))]
        db.execute('DELETE FROM episodes WHERE source_id=?', (ident,))
        db.execute('DELETE FROM sources WHERE id=?', (ident,))
    return episodes
