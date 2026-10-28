import json

import pytest
from fastapi.testclient import TestClient

from app import db, phone_context, pipeline, sources
from app.main import app


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATA', tmp_path)
    db.init()
    phone_context.init()
    pipeline.stop.clear()
    return tmp_path


@pytest.fixture
def client(database, monkeypatch):
    monkeypatch.setattr(pipeline, 'start', lambda: None)
    monkeypatch.setattr(pipeline, 'shutdown', lambda: None)
    with TestClient(app) as test_client:
        yield test_client


def prepared_phone():
    episode = sources.add_video('https://youtu.be/92wmvrQU7a0')
    segments = [
        {'start': 0, 'end': 4, 'text': 'We gaan de volgende bellen.'},
        {'start': 4, 'end': 8, 'text': 'Bel 06 1234 5678, hij heet John.'},
        {'start': 8, 'end': 12, 'text': 'John is eigenaar van de kringloopwinkel.'},
    ]
    entities = [{'name': 'John', 'type': 'person', 'count': 2, 'occurrences': [
        {'segment': 1, 'start': 4, 'spans': [{'segment': 1, 'offset': 28, 'end_offset': 32}]},
        {'segment': 2, 'start': 8, 'spans': [{'segment': 2, 'offset': 0, 'end_offset': 4}]},
    ]}]
    db.update('episodes', episode['id'], status='completed', transcript=' '.join(s['text'] for s in segments),
              segments=json.dumps(segments), entities=json.dumps(entities))
    pipeline.scan_phone_mentions(episode['id'], segments)
    with db.connect() as connection:
        connection.execute("INSERT INTO phone_lookups(phone,status,attempts,checked,retry_at) VALUES (?,?,?,?,?)",
                           ('0612345678', 'not_found', 2, 1, 9999999999))
    return episode, segments


def test_context_window_masks_number_and_keeps_neighboring_evidence(database):
    _, segments = prepared_phone()
    text, first, last = phone_context.context_window(segments, 1, '06 1234 5678')

    assert '[DOELNUMMER]' in text
    assert '06 1234 5678' not in text
    assert 'John is eigenaar' in text
    assert (first, last) == (0, 3)


def test_context_init_seeds_visible_jobs_before_gpu_work(database):
    prepared_phone()

    phone_context.init()

    saved = db.one('SELECT status FROM phone_context_analyses')
    assert saved == {'status': 'pending'}


def test_context_queue_persists_candidate_and_wakes_targeted_lookup(database):
    episode, _ = prepared_phone()
    calls = []

    def analyzer(text, entities):
        calls.append((text, entities))
        return {
            'subject_type': 'person', 'relation': 'called_target', 'label': 'John',
            'description': 'eigenaar van de kringloopwinkel', 'confidence': 88,
            'evidence': 'John is eigenaar van de kringloopwinkel.', 'model': 'test-model',
        }

    assert phone_context.analyze_once(analyzer) is True
    saved = db.one('SELECT * FROM phone_context_analyses')
    assert saved['status'] == 'completed'
    assert saved['episode_id'] == episode['id']
    assert saved['label'] == 'John' and saved['confidence'] == 88
    assert calls[0][1] == [{'name': 'John', 'type': 'person'}]
    assert db.one("SELECT retry_at FROM phone_lookups WHERE phone='0612345678'")['retry_at'] == 0


def test_context_queue_rejects_non_literal_worker_label(database):
    prepared_phone()

    def analyzer(_text, _entities):
        return {
            'subject_type': 'person', 'relation': 'called_target', 'label': 'Verzonnen Naam',
            'description': 'onbekend', 'confidence': 99,
            'evidence': 'John is eigenaar van de kringloopwinkel.', 'model': 'test-model',
        }

    assert phone_context.analyze_once(analyzer) is True
    saved = db.one('SELECT * FROM phone_context_analyses')
    assert saved['status'] == 'error'
    assert saved['label'] is None


def test_phone_api_exposes_candidate_and_manual_review(client):
    episode, _ = prepared_phone()
    phone_context.analyze_once(lambda *_: {
        'subject_type': 'person', 'relation': 'called_target', 'label': 'John',
        'description': 'eigenaar van de kringloopwinkel', 'confidence': 88,
        'evidence': 'John is eigenaar van de kringloopwinkel.', 'model': 'test-model',
    })

    phone = client.get('/api/phone-numbers').json()[0]
    assert phone['best_candidate']['label'] == 'John'
    assert phone['attribution'] is None

    response = client.patch('/api/phone-numbers/0612345678/attribution', json={
        'state': 'confirmed', 'subject_type': 'person', 'relation': 'called_target',
        'label': 'John', 'description': 'eigenaar van de kringloopwinkel',
        'episode_id': episode['id'], 'segment': 1,
    })
    assert response.status_code == 200
    reviewed = client.get('/api/phone-numbers').json()[0]['attribution']
    assert reviewed['state'] == 'confirmed' and reviewed['label'] == 'John'
    assert reviewed['source'] == 'manual'

    rejected = client.patch('/api/phone-numbers/0612345678/attribution', json={'state': 'rejected'})
    assert rejected.status_code == 200
    assert client.get('/api/phone-numbers').json()[0]['attribution']['state'] == 'rejected'
