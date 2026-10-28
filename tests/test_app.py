import hashlib
import json
import socket
import time
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
import httpx
from fastapi.testclient import TestClient
from app import db, meetings, pipeline, sources
from app.locations import classify_location, matches_region
from app.main import app
from app import main


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATA', tmp_path)
    db.init()
    pipeline.stop.clear()
    return tmp_path


@pytest.fixture
def client(store, monkeypatch):
    monkeypatch.setattr(pipeline, 'start', lambda: None)
    monkeypatch.setattr(pipeline, 'shutdown', lambda: None)
    monkeypatch.setattr(sources.socket, 'getaddrinfo', lambda *a, **k: [(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443))])
    with TestClient(app) as c:
        yield c


def test_video_canonicalization_and_deduplication(client):
    first=client.post('/api/sources', json={'url':'https://youtu.be/92wmvrQU7a0?t=4'}).json()
    again=client.post('/api/sources', json={'url':'https://www.youtube.com/watch?v=92wmvrQU7a0&list=ignored'}).json()
    assert first['episode_id']==again['episode_id']
    assert len(client.get('/api/episodes').json())==1
    assert client.get('/api/episodes').json()[0]['url']=='https://www.youtube.com/watch?v=92wmvrQU7a0'
    assert client.get('/api/episodes').json()[0]['thumbnail_url'].endswith('/92wmvrQU7a0/hqdefault.jpg')
    assert client.post('/api/sources',json={'url':'https://youtube.com/watch?v=invalid'}).status_code==400


def test_private_sources_and_cross_origin_requests_rejected(client, monkeypatch):
    monkeypatch.setattr(sources.socket,'getaddrinfo', lambda *a, **k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('192.168.1.28',80))])
    assert client.post('/api/sources',json={'url':'http://192.168.1.28/feed'}).status_code==400
    assert client.post('/api/sources',json={'url':'file:///etc/passwd'}).status_code==400
    assert client.post('/api/sources',json={'url':'https://example.com/feed'},headers={'Origin':'https://evil.example'}).status_code==403


def test_feed_import_only_latest_initially_and_new_items_later(client, monkeypatch):
    source=client.post('/api/sources',json={'url':'https://example.com/feed','initial_limit':1}).json()
    entries=[{'external_id':'rss:'+str(i),'url':'https://example.com/'+str(i),'kind':'rss','title':str(i)} for i in [3,2,1]]
    monkeypatch.setattr(sources,'discover',lambda _:('Test podcast',entries))
    sources.sync_source(source)
    assert [e['title'] for e in db.rows('SELECT * FROM episodes')]==['3']
    source=db.one('SELECT * FROM sources')
    sources.sync_source(source)
    assert len(db.rows('SELECT * FROM episodes'))==1
    entries.insert(0,{'external_id':'rss:4','url':'https://example.com/4','kind':'rss','title':'4'})
    sources.sync_source(source)
    assert {e['title'] for e in db.rows('SELECT * FROM episodes')}=={'3','4'}


def test_pause_and_resume(client):
    source=client.post('/api/sources',json={'url':'https://example.com/feed'}).json()
    ident=source['id']
    assert client.patch('/api/sources/'+ident,json={'enabled':False}).status_code==200
    assert client.post('/api/sources/'+ident+'/sync').status_code==409
    client.patch('/api/sources/'+ident,json={'enabled':True})
    assert client.post('/api/sources/'+ident+'/sync').status_code==200


def test_global_pause_is_persistent_and_stops_starting_queue_work(client):
    ident = client.post('/api/sources', json={'url':'https://youtu.be/92wmvrQU7a0'}).json()['episode_id']
    assert client.get('/api/processing').json()['paused'] is False
    paused = client.put('/api/processing', json={'paused':True}).json()
    assert paused['paused'] is True and paused['finishing'] is False
    assert pipeline.next_episode() is None
    assert db.one('SELECT status FROM episodes WHERE id=?', (ident,))['status'] == 'queued'
    db.init()
    assert client.get('/api/processing').json()['paused'] is True
    assert client.put('/api/processing', json={'paused':False}).json()['paused'] is False
    assert pipeline.next_episode()['id'] == ident


def test_global_pause_reports_current_work_until_checkpoint(client):
    ident = client.post('/api/sources', json={'url':'https://youtu.be/92wmvrQU7a0'}).json()['episode_id']
    db.update('episodes', ident, status='analyzing', transcript='Bestaand transcript.')
    status = client.put('/api/processing', json={'paused':True}).json()
    assert status['paused'] is True and status['finishing'] is True and status['active'] == 1


def test_pause_during_download_does_not_start_whisper(client, store, monkeypatch):
    ident = client.post('/api/sources', json={'url':'https://youtu.be/92wmvrQU7a0'}).json()['episode_id']
    audio = store/'audio'/ident/'audio.mp3'
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b'fake audio')
    def download_and_pause(_):
        db.set_processing_paused(True)
        return str(audio)
    worker = Mock(return_value={'text':'Een transcript.', 'segments':[{'start':0,'end':1,'text':'Een transcript.'}]})
    monkeypatch.setattr(pipeline, 'download', download_and_pause)
    monkeypatch.setattr(pipeline, 'worker_request', worker)
    pipeline.process(db.one('SELECT * FROM episodes WHERE id=?', (ident,)))
    saved = db.one('SELECT * FROM episodes WHERE id=?', (ident,))
    assert saved['status'] == 'queued' and saved['audio_path'] == str(audio)
    assert audio.is_file() and worker.call_count == 0
    db.set_processing_paused(False)
    pipeline.process(saved)
    assert worker.call_count == 1
    assert db.one('SELECT transcript FROM episodes WHERE id=?', (ident,))['transcript'] == 'Een transcript.'


def test_transcription_requests_diarization_and_keeps_speaker_labels(client, store, monkeypatch):
    ident = client.post('/api/sources', json={'url':'https://youtu.be/92wmvrQU7a0'}).json()['episode_id']
    audio = store/'audio'/ident/'audio.mp3'
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b'fake audio')
    db.update('episodes', ident, audio_path=str(audio))
    request = Mock(return_value={
        'text':'Hallo. Dag.', 'language':'nl', 'duration':2,
        'segments':[
            {'start':0,'end':1,'text':'Hallo.','speaker':'SPEAKER_00'},
            {'start':1,'end':2,'text':'Dag.','speaker':'Basti','speaker_id':'known-id'},
        ],
        'diarization_error':None,
    })
    monkeypatch.setattr(pipeline, 'worker_request', request)

    pipeline.process(db.one('SELECT * FROM episodes WHERE id=?', (ident,)))

    assert request.call_args.kwargs['data']['diarize'] == 'true'
    episode = client.get('/api/episodes/'+ident).json()
    assert episode['segments'][0]['speaker'] == 'Spreker 1'
    assert episode['segments'][1]['speaker'] == 'Basti'
    assert episode['segments'][1]['speaker_id'] == 'known-id'
    assert episode['speakers'] == [
        {'name':'Basti','id':'known-id','segments':1},
        {'name':'Spreker 1','id':None,'segments':1},
    ]


def test_existing_transcript_can_be_queued_for_speaker_recognition(client):
    ident = client.post('/api/sources', json={'url':'https://youtu.be/92wmvrQU7a0'}).json()['episode_id']
    db.update('episodes', ident, status='completed', transcript='Bestaand transcript.',
              segments='[]', entities='[]')

    response = client.post('/api/episodes/'+ident+'/speakers')

    assert response.status_code == 200
    saved = db.one('SELECT * FROM episodes WHERE id=?', (ident,))
    assert saved['speaker_requested'] == 1
    assert saved['transcript'] == 'Bestaand transcript.'
    assert pipeline.next_episode()['id'] == ident


def test_speaker_page_aggregates_spoken_entities_and_renames_every_file(client, monkeypatch):
    profile_id = 'a' * 32
    first = client.post('/api/sources', json={'url':'https://youtu.be/92wmvrQU7a0'}).json()['episode_id']
    second = client.post('/api/sources', json={'url':'https://youtu.be/dQw4w9WgXcQ'}).json()['episode_id']
    segments = [
        {'start':0,'end':3,'text':'Noor noemt NOS.','speaker':'Spreker 1','speaker_id':profile_id},
        {'start':3,'end':5,'text':'Tot ziens.','speaker':'Spreker 2','speaker_id':'b'*32},
    ]
    entities = [{'name':'NOS','type':'organization','count':1,'occurrences':[
        {'segment':0,'offset':11,'end_offset':14,'start':0,'spans':[{'segment':0,'offset':11,'end_offset':14}]}
    ]}]
    for ident in (first, second):
        db.update('episodes', ident, status='completed', transcript='Noor noemt NOS. Tot ziens.',
                  segments=json.dumps(segments), entities=json.dumps(entities))
    profiles = [
        {'profile_id':profile_id,'name':'','display_name':'Spreker 184','suggested_name':'Noor', 'sample_available':True},
        {'profile_id':'b'*32,'name':'','display_name':'Spreker 185','suggested_name':'', 'sample_available':False},
    ]
    calls = []
    def worker(method, path, **kwargs):
        calls.append((method,path,kwargs))
        if method == 'GET': return profiles
        return {**profiles[0], 'name':'Nordin', 'display_name':'Nordin'}
    monkeypatch.setattr(main, 'worker_json', worker)

    speakers = client.get('/api/speakers').json()
    first_speaker = next(item for item in speakers if item['profile_id'] == profile_id)
    assert first_speaker['episodes'] == 2
    assert first_speaker['analysis_pending'] == 0
    assert first_speaker['segments'] == 2
    assert first_speaker['entities'] == [{'name':'NOS','type':'organization','count':2}]

    renamed = client.patch('/api/speakers/'+profile_id, json={'name':'Nordin'})
    assert renamed.status_code == 200
    assert calls[-1][0:2] == ('PATCH','/v1/speakers/'+profile_id)
    for ident in (first, second):
        saved = json.loads(db.one('SELECT segments FROM episodes WHERE id=?', (ident,))['segments'])
        assert saved[0]['speaker'] == 'Nordin'


def test_speaker_backfill_only_queues_transcripts_without_voice_ids(client):
    old = client.post('/api/sources', json={'url':'https://youtu.be/92wmvrQU7a0'}).json()['episode_id']
    ready = client.post('/api/sources', json={'url':'https://youtu.be/dQw4w9WgXcQ'}).json()['episode_id']
    db.update('episodes', old, status='completed', transcript='Oud.', segments='[]', entities='[]')
    db.update('episodes', ready, status='completed', transcript='Nieuw.',
              segments=json.dumps([{'text':'Nieuw.','speaker_id':'c'*32}]), entities='[]')

    response = client.post('/api/speakers/backfill').json()

    assert response['queued'] == 1
    assert db.one('SELECT speaker_requested FROM episodes WHERE id=?', (old,))['speaker_requested'] == 1
    assert db.one('SELECT speaker_requested FROM episodes WHERE id=?', (ready,))['speaker_requested'] == 0


def test_global_pause_finishes_entity_chunk_then_resumes_checkpoint(client, monkeypatch):
    ident = client.post('/api/sources', json={'url':'https://youtu.be/92wmvrQU7a0'}).json()['episode_id']
    transcript = ' '.join('Noor' for _ in range(4000))
    chunks = list(pipeline.text_chunks(transcript))
    assert len(chunks) > 1
    db.update('episodes', ident, transcript=transcript,
              segments=json.dumps([{'text':transcript,'start':0,'end':100}]), status='transcribed')
    calls = []
    def worker(path, **kwargs):
        calls.append(kwargs['json']['text'])
        if len(calls) == 1:
            db.set_processing_paused(True)
        return {'entities':[{'name':'Noor','type':'person'}]}
    monkeypatch.setattr(pipeline, 'worker_request', worker)
    monkeypatch.setattr(pipeline, 'worker_gpu_busy', lambda: False)
    pipeline.process(db.one('SELECT * FROM episodes WHERE id=?', (ident,)))
    saved = db.one('SELECT * FROM episodes WHERE id=?', (ident,))
    assert saved['status'] == 'transcribed' and saved['entities'] is None
    assert len(json.loads(saved['entity_chunks'])) == 1
    assert pipeline.next_episode() is None
    db.set_processing_paused(False)
    pipeline.process(saved)
    result = db.one('SELECT * FROM episodes WHERE id=?', (ident,))
    assert result['status'] == 'completed' and len(calls) == len(chunks)
    assert calls[1] == chunks[1]


def test_source_import_waits_for_global_resume(client, monkeypatch):
    source = client.post('/api/sources', json={'url':'https://example.com/feed'}).json()
    entry = {'external_id':'rss:pause-test','url':'https://example.com/episode',
             'kind':'rss','title':'Podcast'}
    def paused_discovery(_):
        db.set_processing_paused(True)
        return 'Podcast', [entry]
    monkeypatch.setattr(sources, 'discover', paused_discovery)
    sources.sync_source(source)
    saved = db.one('SELECT * FROM sources WHERE id=?', (source['id'],))
    assert saved['syncing'] == 0 and saved['checked'] is None
    assert db.one('SELECT COUNT(*) n FROM episodes')['n'] == 0
    db.set_processing_paused(False)
    monkeypatch.setattr(sources, 'discover', lambda _: ('Podcast', [entry]))
    sources.sync_source(saved)
    assert db.one('SELECT COUNT(*) n FROM episodes')['n'] == 1


def test_redirect_to_local_address_is_rejected(store, monkeypatch):
    monkeypatch.setattr(sources.socket,'getaddrinfo',lambda host,*a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1' if host=='localhost' else '8.8.8.8',80))])
    response=Mock(is_redirect=True,headers={'location':'http://localhost/secret'})
    stream=Mock(); stream.__enter__=Mock(return_value=response);stream.__exit__=Mock(return_value=False)
    client=Mock(); client.stream.return_value=stream
    monkeypatch.setattr(sources.httpx,'Client',lambda **k:client)
    with pytest.raises(ValueError,match='openbare'):
        sources.fetch_public('https://example.com/feed')
    assert client.stream.call_count==1


def test_restart_recovers_processing_without_losing_transcript(store):
    e=sources.add_video('https://youtu.be/92wmvrQU7a0')
    db.update('episodes',e['id'],status='analyzing',transcript='Noor werkt bij NOS.',segments='[]',entity_chunks='[[{"name":"Noor","type":"person"}]]')
    db.init()
    saved=db.one('SELECT * FROM episodes')
    assert saved['status']=='queued'
    assert saved['transcript']=='Noor werkt bij NOS.'
    assert len(json.loads(saved['entity_chunks']))==1


def test_entity_analysis_resumes_without_retranscribing(store, monkeypatch):
    e=sources.add_video('https://youtu.be/92wmvrQU7a0')
    text='Noor bij NOS. '+('Tekst. '*1000)
    chunks=list(pipeline.text_chunks(text))
    db.update('episodes',e['id'],transcript=text,segments=json.dumps([{'start':2,'end':10,'text':text}]),entity_chunks=json.dumps([[{'name':'Noor','type':'person'}]]))
    calls=[]
    def worker(path,**kwargs):
        calls.append((path,kwargs))
        return {'entities':[{'name':'NOS','type':'organization'}]}
    monkeypatch.setattr(pipeline,'worker_request',worker)
    monkeypatch.setattr(pipeline,'worker_gpu_busy',lambda:False)
    monkeypatch.setattr(pipeline,'download',lambda _:pytest.fail('Must not re-download'))
    pipeline.process(db.one('SELECT * FROM episodes'))
    saved=db.one('SELECT * FROM episodes')
    assert saved['status']=='completed'
    assert len(calls)==len(chunks)-1
    assert all(c[0]=='/v1/entities' for c in calls)
    assert {e['name'] for e in json.loads(saved['entities'])}=={'Noor','NOS'}


def test_entity_counts_and_timecodes_are_literal():
    segments=[{'start':3,'text':'Noor bij NOS. Noor belt.'},{'start':20,'text':'Noorderlicht en NOS.'}]
    candidates=[{'name':'Noor','type':'person'},{'name':'noor','type':'person'},{'name':'NOS','type':'organization'},{'name':'Verzonnen Naam','type':'person'}]
    result=pipeline.locate_entities(candidates,segments,'')
    assert len(result)==2
    assert next(e for e in result if e['name']=='Noor')['count']==2
    assert next(e for e in result if e['name']=='NOS')['occurrences'][1]['start']==20


def test_location_geography_and_entity_region_filters(client):
    locations = {
        'Groningen': ('NL', 'Groningen', 'EU'),
        'Leeuwarden': ('NL', 'Friesland', 'EU'),
        'Assen': ('NL', 'Drenthe', 'EU'),
        'Eindhoven': ('NL', 'Noord-Brabant', 'EU'),
        'Parijs': ('FR', '', 'EU'),
        'New York': ('US', '', 'NA'),
    }
    for name, (country, province, continent) in locations.items():
        geography = classify_location(name)
        assert (geography['country_code'], geography['province'], geography['continent_code']) == (country, province, continent)
    assert matches_region(classify_location('Assen'), 'north')
    assert not matches_region(classify_location('Eindhoven'), 'north')

    ident = client.post('/api/sources', json={'url':'https://youtu.be/92wmvrQU7a0'}).json()['episode_id']
    entities = [
        {'name':name, 'type':'location', 'count':1, 'occurrences':[]}
        for name in [*locations, 'Verzonnen Plaatsnaam']
    ] + [{'name':'Noor', 'type':'person', 'count':1, 'occurrences':[]}]
    db.update('episodes', ident, status='completed', transcript='Test', segments='[]', entities=json.dumps(entities))
    north = client.get('/api/entities', params={'region':'north'}).json()['entities']
    assert {entity['name'] for entity in north} == {'Groningen', 'Leeuwarden', 'Assen'}
    assert all(entity['type'] == 'location' and entity['geography']['north_netherlands'] for entity in north)
    assert {entity['name'] for entity in client.get('/api/entities', params={'region':'europe'}).json()['entities']} == {'Groningen', 'Leeuwarden', 'Assen', 'Eindhoven', 'Parijs'}
    assert [entity['name'] for entity in client.get('/api/entities', params={'region':'unknown'}).json()['entities']] == ['Verzonnen Plaatsnaam']


def test_entity_analysis_waits_without_calling_worker_when_shared_gpu_is_busy(store, monkeypatch):
    episode = sources.add_video('https://youtu.be/92wmvrQU7a0')
    db.update('episodes', episode['id'], status='transcribed', transcript='Noor is in Groningen.',
              segments=json.dumps([{'start':0, 'end':2, 'text':'Noor is in Groningen.'}]))
    worker = Mock()
    monkeypatch.setattr(pipeline, 'worker_gpu_busy', lambda: True)
    monkeypatch.setattr(pipeline, 'worker_request', worker)
    pipeline.process(db.one('SELECT * FROM episodes'))
    saved = db.one('SELECT * FROM episodes')
    assert saved['status'] == 'transcribed' and saved['stage'] == 'Entiteiten wachten op gedeelde GPU'
    assert saved['retry_at'] > time.time() and worker.call_count == 0


def test_entity_http_500_is_retried_as_smaller_fragments(monkeypatch):
    calls = []
    def worker(path, **kwargs):
        text = kwargs['json']['text']
        calls.append(text)
        if len(text) > 1600:
            request = httpx.Request('POST', 'http://worker/v1/entities')
            response = httpx.Response(500, request=request)
            raise httpx.HTTPStatusError('worker failed', request=request, response=response)
        return {'entities':[{'name':'Noor','type':'person'}, {'name':'Noor','type':'person'}]}
    monkeypatch.setattr(pipeline, 'worker_request', worker)
    entities = pipeline.request_entities('Noor ' * 1300)
    assert entities == [{'name':'Noor','type':'person'}]
    assert len(calls) > 2 and max(map(len, calls[1:])) <= 1600


def test_transient_worker_failures_never_become_permanent(store):
    episode = sources.add_video('https://youtu.be/92wmvrQU7a0')
    db.update('episodes', episode['id'], transcript='Bewaard transcript.', segments='[]', status='analyzing')
    request = httpx.Request('POST', 'http://worker/v1/entities')
    error = httpx.HTTPStatusError('server error', request=request, response=httpx.Response(500, request=request))
    for _ in range(5):
        pipeline.schedule_failure(episode, error)
    saved = db.one('SELECT * FROM episodes')
    assert saved['status'] == 'transcribed' and saved['attempts'] == 5
    assert saved['stage'].startswith('Tijdelijke storing') and saved['retry_at'] > time.time()


def test_old_retryable_failures_are_automatically_recovered(store):
    transcript = sources.add_video('https://youtu.be/92wmvrQU7a0')
    audio = sources.add_video('https://youtu.be/abcdefghijk')
    permanent = sources.add_video('https://youtu.be/abcdefghijl')
    db.update('episodes', transcript['id'], status='failed', transcript='Tekst.', segments='[]', error="Server error '500 Internal Server Error'", attempts=3)
    db.update('episodes', audio['id'], status='failed', error='[Errno 113] No route to host', attempts=3)
    db.update('episodes', permanent['id'], status='failed', error='Whisper heeft geen gesproken tekst gevonden.', attempts=3)
    pipeline.recover_retryable_failures()
    assert db.one('SELECT status FROM episodes WHERE id=?', (transcript['id'],))['status'] == 'transcribed'
    assert db.one('SELECT status FROM episodes WHERE id=?', (audio['id'],))['status'] == 'queued'
    assert db.one('SELECT status FROM episodes WHERE id=?', (permanent['id'],))['status'] == 'failed'


def test_entity_mentions_api_returns_each_timestamped_paragraph(client):
    ident = client.post('/api/sources', json={'url':'https://youtu.be/92wmvrQU7a0'}).json()['episode_id']
    segments = [
        {'start': 5, 'end': 9, 'text': 'Noor werkt bij NOS.'},
        {'start': 18, 'end': 24, 'text': 'Later belt Noor opnieuw.'},
    ]
    entities = [{'name':'Noor','type':'person','count':3,'occurrences':[
        {'segment':0,'start':5,'spans':[{'segment':0,'offset':0,'end_offset':4}]},
        {'segment':0,'start':7,'spans':[{'segment':0,'offset':0,'end_offset':4}]},
        {'segment':1,'start':18,'spans':[{'segment':1,'offset':12,'end_offset':16}]},
    ]}]
    db.update('episodes', ident, transcript=' '.join(s['text'] for s in segments), segments=json.dumps(segments), entities=json.dumps(entities))
    result = client.get('/api/entity-mentions', params={'name':'Noor','kind':'person'}).json()
    assert result['name'] == 'Noor' and result['type'] == 'person'
    assert [(mention['segment'], mention['start'], mention['count'], mention['text']) for mention in result['mentions']] == [
        (0, 5, 2, 'Noor werkt bij NOS.'), (1, 18, 1, 'Later belt Noor opnieuw.'),
    ]


def test_phone_numbers_are_normalized_with_spoken_digit_support():
    segments = [
        {'start': 12, 'text': 'Bel 0613 32 32 11. Het algemene nummer is 0880 237 500.'},
        {'start': 40, 'text': 'Voor informatie bel nul acht acht nul twee drie zeven vijf nul nul.'},
    ]
    mentions = pipeline.extract_phone_mentions(segments)
    assert {(m['phone'], m['display'], m['category']) for m in mentions} == {
        ('0613323211', '06 1332 3211', 'Mobiel nummer'),
        ('0880237500', '088 0237 500', 'Bedrijfsnummer'),
    }


def test_phone_numbers_reject_iban_fragments_and_invalid_prefixes():
    segments = [{'start': 3, 'text': 'Mijn IBAN is NL12 ABCD 0123456789. Bel 020 2288 800.'}]
    mentions = pipeline.extract_phone_mentions(segments)
    assert [(item['phone'], item['display']) for item in mentions] == [('0202288800', '020 228 8800')]
    assert pipeline.normalize_phone('000 60.000') == ''


def test_geographic_phone_numbers_include_their_netnummer_area(client):
    ident = client.post('/api/sources',json={'url':'https://youtu.be/92wmvrQU7a0'}).json()['episode_id']
    segments = [{'start': 3, 'end': 8, 'text': 'Bel 020 2288 800.'}]
    db.update('episodes', ident, transcript=segments[0]['text'], segments=json.dumps(segments))
    pipeline.scan_phone_mentions(ident, segments)
    phone = next(item for item in client.get('/api/phone-numbers').json() if item['number'] == '0202288800')
    assert phone['company'] is None
    assert phone['area'] == ['020', 'Amsterdam']


def test_episode_thumbnail_is_proxied_and_cached(client, monkeypatch, store):
    ident = client.post('/api/sources', json={'url':'https://youtu.be/92wmvrQU7a0'}).json()['episode_id']
    from app import main
    image = b'\xff\xd8\xff\xe0' + b'jpeg-artwork'
    monkeypatch.setattr(main, 'fetch_public', lambda url, max_bytes: image)
    response = client.get('/api/episodes/'+ident+'/thumbnail')
    assert response.status_code == 200
    assert response.headers['content-type'] == 'image/jpeg'
    assert response.content == image
    assert (store/'thumbnails'/(ident+'.jpg')).is_file()


def test_phone_numbers_api_groups_mentions_and_keeps_only_verified_company_links(client):
    ident = client.post('/api/sources',json={'url':'https://youtu.be/92wmvrQU7a0'}).json()['episode_id']
    segments = [{'start': 25, 'end': 30, 'text': 'Het algemene nummer van Farent is 0880 237 500.'}]
    db.update('episodes', ident, transcript=segments[0]['text'], segments=json.dumps(segments))
    pipeline.scan_phone_mentions(ident, segments)
    pipeline.seed_phone_verifications()
    result = client.get('/api/phone-numbers').json()
    assert result[0]['number'] == '0880237500'
    assert result[0]['company'] == 'Stichting Farent'
    assert result[0]['kvk_number'] == '73600776'
    assert result[0]['location'] == "Rogier van der Weydenstraat 2, 5213 CA 's-Hertogenbosch"
    assert result[0]['mentions'][0]['start'] == 25


def test_phone_number_overview_groups_mentions_from_multiple_episodes(client):
    first = client.post('/api/sources',json={'url':'https://youtu.be/92wmvrQU7a0'}).json()['episode_id']
    second = client.post('/api/sources',json={'url':'https://youtu.be/abcdefghijk'}).json()['episode_id']
    for ident, start in [(first, 12), (second, 44)]:
        segments = [{'start': start, 'end': start+4, 'text': 'Bel 0880 237 500 voor informatie.'}]
        db.update('episodes', ident, transcript=segments[0]['text'], segments=json.dumps(segments))
        pipeline.scan_phone_mentions(ident, segments)
    result = client.get('/api/phone-numbers').json()
    phone = next(item for item in result if item['number'] == '0880237500')
    assert len(phone['mentions']) == 2
    assert len(phone['episodes']) == 2
    assert {episode['id'] for episode in phone['episodes']} == {first, second}
    assert {episode['mentions'][0]['start'] for episode in phone['episodes']} == {12, 44}


def test_transcript_progress_reports_durable_queue_progress(client, monkeypatch):
    completed = client.post('/api/sources', json={'url':'https://youtu.be/92wmvrQU7a0'}).json()['episode_id']
    current = client.post('/api/sources', json={'url':'https://youtu.be/abcdefghijk'}).json()['episode_id']
    waiting = client.post('/api/sources', json={'url':'https://youtu.be/zyxwvutsrqp'}).json()['episode_id']
    db.update('episodes', completed, status='transcribed', transcript='Transcript is duurzaam opgeslagen.')
    db.update('episodes', current, status='transcribing', stage='Whisper transcribeert', duration=120)
    from app import main
    monkeypatch.setattr(main, 'worker_inference_progress', lambda episode_id: {
        'phase': 'transcribing', 'duration': 120.0, 'processed_seconds': 48.0, 'percent': 40,
    })
    result = client.get('/api/transcript-progress').json()
    assert result['total'] == 3
    assert result['done'] == 1 and result['remaining'] == 2 and result['waiting'] == 1
    assert result['running'] == 1 and result['percent'] == 33
    assert result['current']['id'] == current
    assert result['current']['duration'] == 120
    assert result['current']['worker_progress']['percent'] == 40
    assert waiting != current


def test_entity_progress_tracks_saved_chunks_and_waiting_episodes(client):
    assert client.get('/api/entity-progress').json()['total'] == 0
    ids = [client.post('/api/sources', json={'url':f'https://youtu.be/{video}'}).json()['episode_id']
           for video in ('92wmvrQU7a0', 'abcdefghijk', 'zyxwvutsrqp', '0123456789a')]
    transcript = ' '.join('woord'+str(i) for i in range(1800))
    parts = list(pipeline.text_chunks(transcript))
    assert len(parts) > 1
    db.update('episodes', ids[0], status='completed', transcript='Klaar.', entities='[]')
    db.update('episodes', ids[1], status='analyzing', transcript=transcript,
              entity_chunks=json.dumps([[{'name':'NOS','type':'organization'}]]),
              stage=f'Entiteiten herkennen · deel 2 van {len(parts)}')
    db.update('episodes', ids[2], status='transcribed', transcript='Wacht op analyse.')
    result = client.get('/api/entity-progress').json()
    assert (result['total'],result['done'],result['waiting'],result['awaiting_transcript'],result['running']) == (4,1,1,1,1)
    assert result['current']['id'] == ids[1]
    assert result['current']['parts_done'] == 1
    assert result['current']['parts_total'] == len(parts)
    assert result['current']['percent'] == round(100/len(parts))
    db.update('episodes', ids[1], entity_chunks=json.dumps([[], []]))
    resumed = client.get('/api/entity-progress').json()
    assert resumed['current']['parts_done'] == 2
    assert resumed['percent'] == 25  # Queue percentage changes only on completion.


def test_chunking_covers_entire_transcript_with_overlap():
    text=' '.join('woord'+str(i) for i in range(10000))
    chunks=list(pipeline.text_chunks(text))
    assert all(len(c)<=6500 for c in chunks)
    assert chunks[0].startswith(text[:30]) and chunks[-1].endswith(text[-30:])
    for a,b in zip(chunks,chunks[1:]):
        assert a[-350:]==b[:350]
    assert 'woord9999' in chunks[-1]


def test_transcript_survives_entity_failure(store, monkeypatch):
    e=sources.add_video('https://youtu.be/92wmvrQU7a0')
    audio=store/'audio'/e['id']/'audio.mp3'; audio.parent.mkdir(parents=True); audio.write_bytes(b'audio')
    db.update('episodes',e['id'],audio_path=str(audio))
    def worker(path,**kwargs):
        if path.endswith('transcriptions'):
            return {'text':'Noor spreekt.','segments':[{'start':0,'end':2,'text':'Noor spreekt.'}],'language':'nl','duration':2}
        raise RuntimeError('Worker offline')
    monkeypatch.setattr(pipeline,'worker_request',worker)
    monkeypatch.setattr(pipeline,'worker_gpu_busy',lambda:False)
    pipeline.process(db.one('SELECT * FROM episodes'))
    saved=db.one('SELECT * FROM episodes')
    assert saved['transcript']=='Noor spreekt.'
    assert saved['status']=='transcribed'
    assert saved['audio_path'] is None
    assert not audio.exists()
    with pytest.raises(RuntimeError):pipeline.process(saved)


def test_queue_finishes_all_transcripts_before_entity_analysis(store):
    waiting = sources.add_video('https://youtu.be/92wmvrQU7a0')
    ready = sources.add_video('https://youtu.be/abcdefghijk')
    db.update('episodes', waiting['id'], published='20260922', published_at=db.published_seconds('20260922'))
    db.update('episodes', ready['id'], published='20260901', published_at=db.published_seconds('20260901'),
              status='transcribed', transcript='Noor noemt NOS.', segments='[]')
    assert pipeline.next_episode()['id'] == waiting['id']
    db.update('episodes', waiting['id'], status='transcribed', transcript='Klaar.', segments='[]')
    assert pipeline.next_episode()['id'] == waiting['id']


def test_entity_queue_prioritizes_transcripts_with_speaker_profiles(store):
    ordinary = sources.add_video('https://youtu.be/92wmvrQU7a0')
    diarized = sources.add_video('https://youtu.be/abcdefghijk')
    db.update('episodes', ordinary['id'], status='transcribed', transcript='Ouder.', segments='[]')
    db.update('episodes', diarized['id'], status='transcribed', transcript='Stem.',
              segments=json.dumps([{'text':'Stem.','speaker_id':'d'*32}]))

    assert pipeline.next_episode()['id'] == diarized['id']


def test_delayed_transcript_retry_blocks_entity_analysis(store):
    waiting = sources.add_video('https://youtu.be/92wmvrQU7a0')
    ready = sources.add_video('https://youtu.be/abcdefghijk')
    db.update('episodes', waiting['id'], status='queued', retry_at=time.time()+600)
    db.update('episodes', ready['id'], status='transcribed', transcript='Noor noemt NOS.', segments='[]')
    assert pipeline.next_episode() is None


def test_startup_removes_legacy_audio_only_after_a_transcript_exists(store):
    completed=sources.add_video('https://youtu.be/92wmvrQU7a0')
    pending=sources.add_video('https://youtu.be/abcdefghijk')
    completed_audio=store/'audio'/completed['id']/'audio.mp3'; completed_audio.parent.mkdir(parents=True); completed_audio.write_bytes(b'done')
    pending_audio=store/'audio'/pending['id']/'audio.mp3'; pending_audio.parent.mkdir(parents=True); pending_audio.write_bytes(b'pending')
    db.update('episodes', completed['id'], transcript='Opgeslagen transcript.', audio_path=str(completed_audio))
    db.update('episodes', pending['id'], audio_path=str(pending_audio))
    pipeline.purge_transcribed_audio()
    assert not completed_audio.exists()
    assert db.one('SELECT * FROM episodes WHERE id=?',(completed['id'],))['audio_path'] is None
    assert pending_audio.exists()
    assert db.one('SELECT * FROM episodes WHERE id=?',(pending['id'],))['audio_path'] == str(pending_audio)


def test_entities_arrive_a_page_at_a_time_and_count_their_episodes(client):
    """Een groot project levert tienduizenden namen; die gaan niet in één keer mee."""
    for number in range(3):
        ident = client.post('/api/sources', json={'url': 'https://youtu.be/vid%08d' % number}).json()['episode_id']
        # Elke aflevering noemt Noor, de rest is uniek voor deze aflevering.
        named = [{'name': 'Noor', 'type': 'person', 'count': 2, 'occurrences': []}]
        named += [{'name': 'Naam %d-%d' % (number, index), 'type': 'person', 'count': 1, 'occurrences': []}
                  for index in range(4)]
        db.update('episodes', ident, status='completed', transcript='t', segments='[]',
                  entities=json.dumps(named))
    everything = client.get('/api/entities').json()
    assert everything['total'] == 13 and len(everything['entities']) == 13
    # Een naam telt de afleveringen waarin hij valt, en dat bepaalt de volgorde.
    assert everything['entities'][0] == {'name': 'Noor', 'type': 'person', 'count': 6, 'episodes': 3}
    first = client.get('/api/entities?limit=5').json()
    rest = client.get('/api/entities?limit=5&offset=5').json()
    assert first['total'] == rest['total'] == 13
    assert len(first['entities']) == len(rest['entities']) == 5
    names = [entity['name'] for entity in first['entities'] + rest['entities']]
    assert len(set(names)) == 10  # de tweede pagina herhaalt de eerste niet
    assert names == [entity['name'] for entity in everything['entities'][:10]]
    # Het totaal volgt het filter, zodat “x van y” bij een zoekopdracht klopt.
    filtered = client.get('/api/entities?q=Naam 1-&limit=2').json()
    assert filtered['total'] == 4 and len(filtered['entities']) == 2


def test_api_search_export_retry_and_audio_range(client,store):
    ident=client.post('/api/sources',json={'url':'https://youtu.be/92wmvrQU7a0'}).json()['episode_id']
    audio=store/'audio.mp3';audio.write_bytes(b'a'*1000)
    db.update('episodes',ident,status='completed',transcript='Noor bij NOS.',segments='[]',entities=json.dumps([{'name':'Noor','type':'person','count':1,'occurrences':[]}]),audio_path=str(audio))
    assert len(client.get('/api/episodes?q=Noor').json())==1
    assert client.get('/api/episodes?q=missing').json()==[]
    assert client.get('/api/entities?kind=person').json()['entities'][0]['name']=='Noor'
    assert 'Noor bij NOS.' in client.get('/api/episodes/'+ident+'/transcript.txt').text
    detail=client.get('/api/episodes/'+ident).json()
    assert 'audio_path' not in detail and 'entity_chunks' not in detail
    response=client.get('/api/episodes/'+ident+'/audio',headers={'Range':'bytes=0-9'})
    assert response.status_code==206 and len(response.content)==10
    client.post('/api/episodes/'+ident+'/retry')
    saved=db.one('SELECT * FROM episodes')
    assert saved['status']=='queued' and saved['transcript']=='Noor bij NOS.' and saved['entity_chunks'] is None
    db.update('episodes',ident,status='transcribing')
    assert client.post('/api/episodes/'+ident+'/retry').status_code==409


def test_rss_enclosures_and_newest_first(store,monkeypatch):
    feed=b'''<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel><title>Test podcast</title><itunes:image href="https://example.com/cover.jpg"/><item><guid>old</guid><title>Oud</title><pubDate>Mon, 01 Jun 2026 10:00:00 GMT</pubDate><enclosure url="https://example.com/old.mp3" type="audio/mpeg"/></item><item><guid>new</guid><title>Nieuw</title><pubDate>Tue, 02 Jun 2026 10:00:00 GMT</pubDate><enclosure url="https://example.com/new.mp3" type="audio/mpeg"/></item></channel></rss>'''
    monkeypatch.setattr(sources,'fetch_public',lambda _:feed)
    title,entries=sources.discover({'url':'https://example.com/rss','kind':'rss','title':'fallback'})
    assert title=='Test podcast'
    assert [e['title'] for e in entries]==['Nieuw','Oud']
    assert entries[0]['media_url']=='https://example.com/new.mp3'
    assert entries[0]['thumbnail_url']=='https://example.com/cover.jpg'


def test_channel_import_flattens_videos_shorts_and_streams(store, monkeypatch):
    data={'title':'Kanaal','entries':[
        {'_type':'playlist','entries':[{'id':'abcdefghijk','title':'Video','timestamp':10}]},
        {'_type':'playlist','entries':[{'id':'92wmvrQU7a0','title':'Stream','timestamp':30},{'id':'abcdefghijk','title':'Duplicate'}]},
        {'_type':'playlist','entries':[{'id':'shorts12345','title':'Short','timestamp':20},{'id':'future12345','title':'Live','live_status':'is_live'}]},
    ]}
    monkeypatch.setattr(sources,'ytdlp',lambda *a,**kw:json.dumps(data))
    title,entries=sources.discover({'url':'https://youtube.com/@test','kind':'youtube','title':'test'})
    assert title=='Kanaal'
    assert [e['title'] for e in entries]==['Stream','Short','Video']
    assert [e['published'] for e in entries]==['19700101','19700101','19700101']


def test_youtube_scope_preserves_the_requested_tab_or_playlist(client):
    stream = client.post('/api/sources', json={
        'url': 'https://www.youtube.com/@Nordin2409/streams', 'kind': 'auto', 'initial_limit': 0,
    }).json()
    assert stream['kind'] == 'youtube-streams'
    assert stream['url'] == 'https://www.youtube.com/@Nordin2409/streams'
    assert stream['initial_limit'] == 0

    videos = client.post('/api/sources', json={
        'url': 'https://www.youtube.com/@Nordin2409', 'kind': 'youtube-videos', 'initial_limit': 3,
    }).json()
    assert videos['kind'] == 'youtube-videos'
    assert videos['url'].endswith('/videos')

    playlist = client.post('/api/sources', json={
        'url': 'https://www.youtube.com/playlist?list=PL123456789', 'kind': 'youtube-playlist', 'initial_limit': 0,
    }).json()
    assert playlist['kind'] == 'youtube-playlist'
    assert playlist['url'] == 'https://www.youtube.com/playlist?list=PL123456789'
    assert client.post('/api/sources', json={
        'url': 'https://www.youtube.com/@Nordin2409', 'kind': 'youtube-playlist', 'initial_limit': 3,
    }).status_code == 400
    assert client.post('/api/sources', json={
        'url': 'https://www.youtube.com/playlist?list=bad', 'kind': 'youtube-playlist', 'initial_limit': 3,
    }).status_code == 400


def test_stream_source_downloads_only_the_stream_tab_and_all_initial_items(store, monkeypatch):
    requested = []
    data = {'channel': 'Nordin2409', 'entries': [
        {'id': 'stream00001', 'title': 'Afgeronde stream', 'timestamp': 3},
        {'id': 'stream00002', 'title': 'Eerdere stream', 'timestamp': 2},
        {'id': 'stream00003', 'title': 'Lopende stream', 'timestamp': 4, 'live_status': 'is_live'},
        {'id': 'stream00004', 'title': 'Geplande stream', 'timestamp': 5, 'live_status': 'is_upcoming'},
    ]}
    def fake_ytdlp(args, **kwargs):
        requested.extend(args)
        return json.dumps(data)
    monkeypatch.setattr(sources, 'ytdlp', fake_ytdlp)
    source = {
        'id': 'streams', 'url': 'https://www.youtube.com/@Nordin2409/streams',
        'kind': 'youtube-streams', 'title': 'Nordin2409', 'initial_limit': 0,
        'initialized': 0,
    }
    title, entries = sources.discover(source)
    assert title == 'Nordin2409'
    assert [entry['title'] for entry in entries] == ['Afgeronde stream', 'Eerdere stream']
    assert '--playlist-end' not in requested

    with db.connect() as con:
        con.execute('INSERT INTO sources(id,url,kind,title,initial_limit,created) VALUES (?,?,?,?,?,?)',
                    ('streams', source['url'], source['kind'], source['title'], 0, time.time()))
    sources.sync_source(db.one('SELECT * FROM sources WHERE id=?', ('streams',)))
    assert {row['title'] for row in db.rows('SELECT * FROM episodes')} == {'Afgeronde stream', 'Eerdere stream'}


def test_entity_spanning_transcript_segments_is_counted_once():
    segments=[{'start':1,'text':'De gemeente Amsterdam en Mark'},{'start':3,'text':'Rutte werden genoemd.'}]
    result=pipeline.locate_entities([{'name':'Mark Rutte','type':'person'}],segments,'')
    assert len(result)==1 and result[0]['count']==1
    mention=result[0]['occurrences'][0]
    assert mention['start']==1
    assert [s['segment'] for s in mention['spans']]==[0,1]


def test_dashboard_and_exports_cannot_be_cached_by_public_proxy(client):
    assert client.get('/').headers['cache-control']=='private, no-store'
    assert client.get('/api/episodes').headers['cache-control']=='private, no-store'
    assert client.get('/static/app.js').headers['cache-control']=='private, no-store'


def project_with_episode(client, url, title, *, name='', text='Geen bijzonderheden.', entities=None):
    """Create one project and give its single episode a finished transcript."""
    created = client.post('/api/projects', json={'url': url, 'name': name}).json()
    ident = created['episode_id']
    segments = [{'start': 0, 'end': 5, 'text': text}]
    db.update('episodes', ident, title=title, transcript=text, segments=json.dumps(segments),
              entities=json.dumps(entities or []), status='completed', stage='Voltooid')
    return created['project_id'], ident


def test_every_source_becomes_its_own_project_including_loose_videos(client):
    channel = client.post('/api/sources', json={'url':'https://www.youtube.com/@kanaal/streams','name':'Streams'}).json()
    video = client.post('/api/projects', json={'url':'https://youtu.be/92wmvrQU7a0'}).json()
    assert video['kind'] == 'video' and video['project_id']
    projects = client.get('/api/projects').json()
    assert {p['id'] for p in projects} == {channel['id'], video['project_id']}
    assert {p['kind'] for p in projects} == {'youtube-streams', 'video'}
    assert next(p for p in projects if p['id'] == channel['id'])['display_name'] == 'Streams'
    # Every episode lives inside exactly one project environment.
    assert all(row['source_id'] for row in db.rows('SELECT source_id FROM episodes'))
    # A video project has nothing to poll, so it is never queued for a source check.
    assert not db.rows("SELECT id FROM sources WHERE kind='video' AND checked IS NULL")


def test_adding_the_same_video_twice_reuses_its_project(client):
    first = client.post('/api/projects', json={'url':'https://youtu.be/92wmvrQU7a0'}).json()
    again = client.post('/api/projects', json={'url':'https://www.youtube.com/watch?v=92wmvrQU7a0'}).json()
    assert first['project_id'] == again['project_id']
    assert first['episode_id'] == again['episode_id']
    assert len(client.get('/api/projects').json()) == 1


def test_project_environments_keep_episodes_entities_and_numbers_apart(client):
    first, first_episode = project_with_episode(
        client, 'https://youtu.be/92wmvrQU7a0', 'Uitzending A',
        text='Wieke belt met 0880 237 500 over Farent.',
        entities=[{'name':'Farent','type':'organization','count':1,'occurrences':[{'segment':0,'start':0,'offset':32,'end_offset':38}]}])
    second, second_episode = project_with_episode(
        client, 'https://youtu.be/abcdefghijk', 'Uitzending B',
        text='Jasper noemt 0505 123 456 en bedankt Noorderzon.',
        entities=[{'name':'Noorderzon','type':'event','count':1,'occurrences':[{'segment':0,'start':0,'offset':37,'end_offset':47}]}])
    for ident in (first_episode, second_episode):
        episode = db.one('SELECT segments FROM episodes WHERE id=?', (ident,))
        pipeline.scan_phone_mentions(ident, json.loads(episode['segments']))

    assert [e['id'] for e in client.get('/api/episodes?project='+first).json()] == [first_episode]
    assert [e['title'] for e in client.get('/api/episodes?project='+second).json()] == ['Uitzending B']
    assert len(client.get('/api/episodes').json()) == 2

    assert [e['name'] for e in client.get('/api/entities?project='+first).json()['entities']] == ['Farent']
    assert [e['name'] for e in client.get('/api/entities?project='+second).json()['entities']] == ['Noorderzon']
    assert {e['name'] for e in client.get('/api/entities').json()['entities']} == {'Farent', 'Noorderzon'}

    assert [p['number'] for p in client.get('/api/phone-numbers?project='+first).json()] == ['0880237500']
    assert [p['number'] for p in client.get('/api/phone-numbers?project='+second).json()] == ['0505123456']

    assert client.get('/api/stats?project='+first).json()['total'] == 1
    assert client.get('/api/stats').json()['total'] == 2
    assert client.get('/api/transcript-progress?project='+second).json()['total'] == 1
    assert client.get('/api/entity-progress?project='+first).json()['done'] == 1
    mentions = client.get('/api/entity-mentions?name=Noorderzon&project='+first).json()
    assert mentions['mentions'] == []
    assert len(client.get('/api/entity-mentions?name=Noorderzon&project='+second).json()['mentions']) == 1
    assert client.get('/api/episodes?project=onbekend').status_code == 404


def test_project_card_counts_summarize_its_own_environment(client):
    project, ident = project_with_episode(
        client, 'https://youtu.be/92wmvrQU7a0', 'Uitzending A',
        text='Farent is bereikbaar op 0880 237 500.',
        entities=[{'name':'Farent','type':'organization','count':2,'occurrences':[]}])
    pipeline.scan_phone_mentions(ident, json.loads(db.one('SELECT segments FROM episodes WHERE id=?', (ident,))['segments']))
    card = client.get('/api/projects/'+project).json()
    assert (card['episode_count'], card['transcripts'], card['completed']) == (1, 1, 1)
    assert (card['entities'], card['phone_numbers']) == (1, 1)
    assert card['accent'] in db.ACCENTS and card['pollable'] is False


def test_project_can_be_renamed_recoloured_and_paused(client):
    project = client.post('/api/sources', json={'url':'https://example.com/feed'}).json()
    updated = client.patch('/api/projects/'+project['id'], json={
        'name': '  Podcast   Noord  ', 'description': 'Alle afleveringen', 'accent': 'plum', 'enabled': False}).json()
    assert updated['display_name'] == 'Podcast Noord'
    assert (updated['description'], updated['accent'], updated['enabled']) == ('Alle afleveringen', 'plum', 0)
    assert client.patch('/api/projects/'+project['id'], json={'accent':'neon'}).status_code == 400
    assert client.post('/api/projects/'+project['id']+'/sync').status_code == 409
    assert client.patch('/api/projects/onbekend', json={'name':'x'}).status_code == 404


def test_deleting_a_project_removes_only_its_own_environment(client, store):
    keep, keep_episode = project_with_episode(client, 'https://youtu.be/abcdefghijk', 'Blijft')
    drop, drop_episode = project_with_episode(
        client, 'https://youtu.be/92wmvrQU7a0', 'Verdwijnt', text='Bel 0880 237 500 maar.')
    pipeline.scan_phone_mentions(drop_episode, json.loads(db.one('SELECT segments FROM episodes WHERE id=?', (drop_episode,))['segments']))
    (store/'audio'/drop_episode).mkdir(parents=True)
    (store/'audio'/drop_episode/'audio.mp3').write_bytes(b'x')
    (store/'thumbnails').mkdir(exist_ok=True)
    (store/'thumbnails'/(drop_episode+'.jpg')).write_bytes(b'x')

    assert client.delete('/api/projects/'+drop).json() == {'ok': True, 'episodes_removed': 1}
    assert [p['id'] for p in client.get('/api/projects').json()] == [keep]
    assert [e['id'] for e in client.get('/api/episodes').json()] == [keep_episode]
    assert client.get('/api/phone-numbers').json() == []
    assert not (store/'audio'/drop_episode).exists()
    assert not (store/'thumbnails'/(drop_episode+'.jpg')).exists()
    assert client.get('/api/episodes/'+drop_episode).status_code == 404
    assert client.get('/api/episodes/'+keep_episode).json()['project']['id'] == keep


def test_episodes_and_detail_expose_their_project(client):
    project, ident = project_with_episode(
        client, 'https://youtu.be/92wmvrQU7a0', 'Uitzending A', name='Noordelijk archief')
    listed = client.get('/api/episodes').json()[0]
    assert (listed['project_id'], listed['project_name']) == (project, 'Noordelijk archief')
    assert listed['project_accent'] in db.ACCENTS
    detail = client.get('/api/episodes/'+ident).json()['project']
    assert detail['id'] == project and detail['display_name'] == 'Noordelijk archief'


def test_loose_episode_joins_the_source_that_already_discovered_it(store):
    """A video added on its own before its channel was followed belongs to that channel."""
    with db.connect() as connection:
        connection.execute("INSERT INTO sources(id,url,kind,title,initial_limit,initialized,created)"
                           " VALUES ('kanaal','https://www.youtube.com/@kanaal/streams','youtube-streams','Kanaal',3,1,1)")
        connection.execute("INSERT INTO episodes(id,source_id,external_id,url,kind,title,created,updated)"
                           " VALUES ('los',NULL,'yt:92wmvrQU7a0','https://www.youtube.com/watch?v=92wmvrQU7a0','youtube','Aflevering',1,1)")
        connection.execute("INSERT INTO seen(source_id,external_id) VALUES ('kanaal','yt:92wmvrQU7a0')")
    db.init()
    assert db.one("SELECT source_id FROM episodes WHERE id='los'")['source_id'] == 'kanaal'
    assert db.one('SELECT COUNT(*) n FROM sources')['n'] == 1  # geen los project ernaast


def test_existing_loose_episodes_are_adopted_by_a_project_on_upgrade(store):
    with db.connect() as connection:
        connection.execute('''INSERT INTO episodes(id,source_id,external_id,url,kind,title,created,updated)
            VALUES ('legacy',NULL,'yt:legacy','https://www.youtube.com/watch?v=legacy0000','youtube','Oude video',1,1)''')
    db.init()
    episode = db.one("SELECT source_id FROM episodes WHERE id='legacy'")
    project = db.one('SELECT * FROM sources WHERE id=?', (episode['source_id'],))
    assert project['kind'] == 'video' and project['title'] == 'Oude video'
    assert project['accent'] in db.ACCENTS
    db.init()  # a second start must not create a second project for the same video
    assert db.one('SELECT COUNT(*) n FROM sources')['n'] == 1


def archive_page(codes, has_next=False):
    return {'HasNextPage': has_next, 'List': [{
        'code': 'gemeenteopsterland/' + code,
        'customerName': 'Gemeente Opsterland webcasts',
        'title': 'Raadsvergadering',
        'scheduledStart': '/Date(%d000)/' % (1790098200 - index * 86400),  # het archief staat nieuwste eerst
        'tags': ['embed:1', 'Type: Raadsvergadering'],
    } for index, code in enumerate(codes)]}


def test_meeting_archive_url_is_recognised_and_other_platforms_are_not_invented(monkeypatch):
    monkeypatch.setattr(meetings, 'gemeenteoplossingen_portal', lambda host: None)
    assert meetings.canonical('https://channel.royalcast.com/gemeenteopsterland/#!/archived') == (
        'companywebcast', 'https://channel.royalcast.com/gemeenteopsterland/')
    assert meetings.canonical('https://channel.companywebcast.com/gemeenteopsterland/')[0] == 'companywebcast'
    # Any page inside a Notubiz portal belongs to the same municipal archive.
    assert meetings.canonical('https://hoogeveen.raadsinformatie.nl/vergadering/1453754/Dialoog') == (
        'notubiz', 'https://hoogeveen.raadsinformatie.nl/')
    # A feed stays a feed when nothing claims it.
    assert meetings.canonical('https://example.com/feed') is None
    assert meetings.canonical('https://api.notubiz.nl/events') is None
    # Explicitly choosing meetings for an unsupported platform must say so.
    with pytest.raises(ValueError, match='nog niet ondersteund'):
        meetings.canonical('https://example.com/vergaderingen', 'meetings')
    with pytest.raises(ValueError, match='vergaderarchief'):
        meetings.canonical('https://channel.royalcast.com/', 'meetings')


def test_meeting_archive_is_imported_with_its_date_and_meeting_type(monkeypatch):
    monkeypatch.setattr(meetings, 'api_json', lambda url: archive_page(['20260922_1', '20260908_1']))
    title, entries = meetings.discover({'url': 'https://channel.royalcast.com/gemeenteopsterland/',
                                        'kind': 'companywebcast', 'initial_limit': 3})
    assert title == 'Gemeente Opsterland'
    assert [entry['external_id'] for entry in entries] == [
        'cwc:gemeenteopsterland/20260922_1', 'cwc:gemeenteopsterland/20260908_1']
    assert entries[0]['kind'] == 'meeting'
    assert entries[0]['published'] == '20260922'
    assert entries[0]['author'] == 'Raadsvergadering'
    assert entries[0]['url'] == 'https://channel.royalcast.com/gemeenteopsterland/#!/gemeenteopsterland/20260922_1'


def test_meeting_catalogue_keeps_one_size_so_old_meetings_are_not_requeued(monkeypatch):
    """A shrinking window would hand already-seen meetings back as new."""
    monkeypatch.setattr(meetings, 'PAGE_SIZE', 2)
    pages = [archive_page(['a1', 'a2'], True), archive_page(['b1', 'b2'], True), archive_page(['c1'], False)]
    requested = []

    def fake(url):
        requested.append(url)
        return pages[min(len(requested) - 1, len(pages) - 1)]

    monkeypatch.setattr(meetings, 'api_json', fake)
    _, few = meetings.discover({'url': 'https://channel.royalcast.com/gemeenteopsterland/',
                                'kind': 'companywebcast', 'initial_limit': 1})
    requested.clear()
    _, many = meetings.discover({'url': 'https://channel.royalcast.com/gemeenteopsterland/',
                                 'kind': 'companywebcast', 'initial_limit': 0})
    assert len(few) == 2  # bounded window, not just the single wanted item
    assert len(many) == 5  # "alle beschikbare" walks every page
    assert 'pageNumber=0' in requested[0] and 'pageSize=2' in requested[0]
    assert 'status=indexed' in requested[0]  # live and upcoming meetings stay out


def test_meeting_download_link_is_resolved_at_download_time(monkeypatch):
    detail = {'attachments': [
        {'contentType': 'video/x-ms-wmv', 'location': 'http://player.example/opname.wmv'},
        {'contentType': 'audio/mpeg', 'location': 'https://download.example/bb_nl.mp3?Signature=abc'},
        {'contentType': 'video/mp4', 'location': 'https://download.example/bb_nl.mp4?Signature=abc'},
    ], 'title': 'Raadsvergadering', 'actualStart': '/Date(1790098262000)/', 'actualEnd': '/Date(1790110392000)/'}
    monkeypatch.setattr(meetings, 'api_json', lambda url: detail)
    episode = {'url': 'https://channel.royalcast.com/gemeenteopsterland/#!/gemeenteopsterland/20260922_1',
               'external_id': 'cwc:gemeenteopsterland/20260922_1'}
    url, fields, stream = meetings.audio(episode)
    assert url.endswith('bb_nl.mp3?Signature=abc')  # audio wins over the video copies
    assert fields == {'title': 'Raadsvergadering', 'duration': 12130.0}
    assert stream is False  # a plain audio attachment is downloaded as a file
    monkeypatch.setattr(meetings, 'api_json', lambda url: {'attachments': []})
    with pytest.raises(ValueError, match='geen downloadbare opname'):
        meetings.audio(episode)
    with pytest.raises(ValueError, match='bronverwijzing'):
        meetings.audio({'url': 'https://example.com/x', 'external_id': 'cwc:a/b'})


def test_meeting_archive_becomes_its_own_project(client, monkeypatch):
    monkeypatch.setattr(meetings, 'api_json', lambda url: archive_page(['20260922_1', '20260908_1']))
    project = client.post('/api/projects', json={
        'url': 'https://channel.royalcast.com/gemeenteopsterland/#!/archived'}).json()
    assert project['kind'] == 'companywebcast'
    assert project['url'] == 'https://channel.royalcast.com/gemeenteopsterland/'
    sources.sync_source(db.one('SELECT * FROM sources WHERE id=?', (project['id'],)))
    refreshed = client.get('/api/projects/' + project['id']).json()
    assert refreshed['display_name'] == 'Gemeente Opsterland'
    episodes = client.get('/api/episodes?project=' + project['id']).json()
    assert [episode['kind'] for episode in episodes] == ['meeting', 'meeting']
    assert episodes[0]['project_name'] == 'Gemeente Opsterland'
    # Picking a platform we do not support must not silently become an RSS feed.
    rejected = client.post('/api/projects', json={'url': 'https://example.com/raad', 'kind': 'meetings'})
    assert rejected.status_code == 400 and 'nog niet ondersteund' in rejected.json()['detail']


NOTUBIZ_PORTAL_PAGE = '<meta class="site" data-organisation-id="572" />'
# De vergaderpagina zet de live-encoder en de opname als losse blokken neer;
# alleen de tweede hoort in `media_files` en heeft een terugkijkbare stream.
NOTUBIZ_MEETING_PAGE = """
    var encoder_plannings = {}, media_files = {}, media_file;
        media_file = {
            pubpoint: "rtmp://wowza1.notubiz.nl/nbrepeater",
            streamname: "mp4:S_01-HOOGEVEEN-RAA",
            file: '',
        };
        encoder_plannings[213152] = media_file;
        media_file = {
            pubpoint: "rtmp://wowza1.notubiz.nl/nbvod/_definst_",
            streamname: "Hoogeveen/bestanden/S_24.09.26-HOOGEVEEN-RAA.mp4",
            file: "https://hoogeveen.raadsinformatie.nl/media/download/S_24.09.26-HOOGEVEEN-RAA.mp4",
        };
        media_files[402420] = media_file;
        $('#mediaspace').createPlayer({
            pubpoint: media_file.pubpoint,
            title: "Vergadering Raadsavond raadzaal 24-09-2026",
        });
"""


def notubiz_moment(hours_ago):
    """Notubiz noemt lokale tijden; de tests rekenen vanaf nu terug."""
    return time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() - hours_ago * 3600))


def notubiz_day(days_ago):
    return time.strftime('%Y-%m-%d 19:30:00', time.gmtime(time.time() - days_ago * 86400))


def notubiz_event(ident, title, start, kind='meeting', live=True, group='public'):
    return {'id': ident, 'type': kind, 'live': live, 'permission_group': group,
            'canceled': False, 'inactive': False, 'gremium': {'id': 10241},
            'attributes': [{'id': 1, 'value': title}],
            'plannings': [{'start_date': start}]}


def notubiz_archive(monkeypatch, events, rooms=()):
    """Serve the Notubiz endpoints this adapter touches; return the asked URLs."""
    asked = []

    def api_json(url):
        asked.append(url)
        if '/gremia' in url:
            return {'gremia': [{'id': 10241, 'title': 'Raadsavond'}]}
        if '/events/assemblies/' in url:
            return {'assembly': {'meetings': [{'id': room, 'plannings': [{'start_date': start}]}
                                              for room, start in rooms]}}
        if url.startswith('https://api.notubiz.nl/events?'):
            window = parse_qs(urlsplit(url).query)
            inside = [event for event in events
                      if window['date_from'][0] <= event['plannings'][0]['start_date']
                      <= window['date_to'][0]]
            return {'pagination': {'has_more_pages': False}, 'events': inside}
        return {'organisation': {'name': 'Gemeente Hoogeveen'}}

    monkeypatch.setattr(meetings, 'api_json', api_json)
    monkeypatch.setattr(meetings, 'page_text', lambda url: NOTUBIZ_PORTAL_PAGE)
    return asked


def test_notubiz_archive_keeps_only_the_public_meetings_that_were_broadcast(monkeypatch):
    evening = notubiz_day(4)
    events = [
        notubiz_event(1, 'Presidium', notubiz_day(20), group='members'),
        notubiz_event(2, 'Inloopspreekuur', notubiz_day(10), live=False),
        notubiz_event(3, 'Dialoogavond armoedebestrijding', notubiz_day(6)),
        notubiz_event(4, 'Raadsavond parallel', evening, kind='assembly'),
        notubiz_event(5, 'Raadsavond raadzaal', notubiz_moment(1)),
    ]
    notubiz_archive(monkeypatch, events, rooms=[(11, evening), (12, evening)])
    title, entries = meetings.discover({'url': 'https://hoogeveen.raadsinformatie.nl/',
                                        'kind': 'notubiz', 'initial_limit': 3, 'initialized': 0})
    assert title == 'Gemeente Hoogeveen'
    # Besloten of nooit uitgezonden: dan valt er ook geen opname op te halen.
    # De agenda kondigt ook aan, dus de vergadering van een uur geleden kan nog
    # bezig zijn en wacht op een volgende controle.
    assert [entry['external_id'] for entry in entries] == ['ntb:572/11', 'ntb:572/12', 'ntb:572/3']
    assert entries[0]['url'] == 'https://hoogeveen.raadsinformatie.nl/vergadering/11'
    assert entries[0]['kind'] == 'meeting'
    assert entries[0]['author'] == 'Raadsavond'  # het gremium is het vergadertype
    assert entries[0]['published'] == evening[:10].replace('-', '')
    # Een raadsavond vergadert in meerdere zalen tegelijk, elk met een eigen
    # opname; hun eigen naam staat pas op de vergaderpagina.
    assert [entry['title'] for entry in entries] == [
        'Raadsavond parallel · deel 1', 'Raadsavond parallel · deel 2',
        'Dialoogavond armoedebestrijding']


def test_notubiz_follow_up_check_stays_inside_the_first_catalogue(monkeypatch):
    """Re-walking twenty years every hour would be rude, and pointless."""
    events = [notubiz_event(3, 'Raadsavond', notubiz_day(4)),
              notubiz_event(4, 'Raadsavond', notubiz_day(2500))]
    asked = notubiz_archive(monkeypatch, events)
    portal = {'url': 'https://hoogeveen.raadsinformatie.nl/', 'kind': 'notubiz', 'initial_limit': 0}
    _, first = meetings.discover({**portal, 'initialized': 0})
    windows = len([url for url in asked if '/events?' in url])
    asked.clear()
    _, later = meetings.discover({**portal, 'initialized': 1})
    assert [entry['external_id'] for entry in first] == ['ntb:572/3', 'ntb:572/4']
    assert windows >= meetings.NOTUBIZ_YEARS - 1  # de eerste import loopt jaar voor jaar terug
    # Daarna volstaat één recent venster, dat binnen het eerste valt: een oude
    # vergadering kan zo niet alsnog als nieuw in de wachtrij komen.
    assert len([url for url in asked if '/events?' in url]) == 1
    assert [entry['external_id'] for entry in later] == ['ntb:572/3']


def test_notubiz_recording_is_read_from_the_player_on_the_meeting_page(monkeypatch):
    monkeypatch.setattr(meetings, 'page_text', lambda url: NOTUBIZ_MEETING_PAGE)
    episode = {'url': 'https://hoogeveen.raadsinformatie.nl/vergadering/11',
               'external_id': 'ntb:572/11'}
    url, fields, stream = meetings.audio(episode)
    # De opname wint van de live-encoder, en de speler bouwt zijn URL net zo.
    assert url == ('https://wowza1.notubiz.nl/nbvod/_definst_/'
                   'Hoogeveen/bestanden/S_24.09.26-HOOGEVEEN-RAA.mp4/playlist.m3u8')
    assert fields == {'title': 'Vergadering Raadsavond raadzaal 24-09-2026'}
    assert stream is True  # alleen het geluid; de video blijft van de schijf
    monkeypatch.setattr(meetings, 'page_text', lambda url: '<p>Deze avond is niet opgenomen.</p>')
    with pytest.raises(ValueError, match='geen opname'):
        meetings.audio(episode)
    with pytest.raises(ValueError, match='bronverwijzing'):
        meetings.audio({'url': 'https://example.com/x', 'external_id': 'ntb:572/11'})
    # Een portaal mag FFmpeg niet naar een eigen keuze van server sturen.
    monkeypatch.setattr(meetings, 'page_text', lambda url: NOTUBIZ_MEETING_PAGE.replace(
        'rtmp://wowza1.notubiz.nl/nbvod/_definst_', 'rtmp://127.0.0.1/nbvod/_definst_'))
    with pytest.raises(ValueError, match='onbekende mediaserver'):
        meetings.audio(episode)


def test_meeting_video_never_reaches_the_disk(store, monkeypatch):
    """Whisper only needs the sound, and a council recording is gigabytes."""
    episode = sources.insert_episode({'external_id': 'ntb:572/11', 'kind': 'meeting',
                                      'url': 'https://hoogeveen.raadsinformatie.nl/vergadering/11',
                                      'title': 'Raadsavond parallel · deel 1'})
    stream = 'https://wowza1.notubiz.nl/nbvod/_definst_/Hoogeveen/bestanden/x.mp4/playlist.m3u8'
    monkeypatch.setattr(meetings, 'audio', lambda _: (stream, {'title': 'Raadzaal'}, True))
    monkeypatch.setattr(sources, 'public_url', lambda url: url)
    monkeypatch.setattr(sources, 'fetch_public', Mock(side_effect=AssertionError('gedownload')))
    calls = []

    def ffmpeg(args, **options):
        calls.append(args)
        open(args[-1], 'wb').write(b'ID3')
        return Mock(returncode=0)

    monkeypatch.setattr(sources.subprocess, 'run', ffmpeg)
    path = sources.download(episode)
    assert path == str(db.DATA / 'audio' / episode['id'] / 'audio.mp3')
    assert [item.name for item in (db.DATA / 'audio' / episode['id']).iterdir()] == ['audio.mp3']
    assert len(calls) == 1  # geen tweede ronde: FFmpeg schrijft meteen de mp3
    assert stream in calls[0] and '-vn' in calls[0]
    assert calls[0][calls[0].index('-max_redirects') + 1] == '0'
    assert db.one('SELECT title FROM episodes WHERE id=?', (episode['id'],))['title'] == 'Raadzaal'


def test_notubiz_portal_becomes_its_own_project(client, monkeypatch):
    notubiz_archive(monkeypatch, [notubiz_event(3, 'Raadsavond', notubiz_day(4))])
    project = client.post('/api/projects', json={
        'url': 'https://hoogeveen.raadsinformatie.nl/vergadering/1453754/Raadsavond'}).json()
    assert project['kind'] == 'notubiz'
    assert project['url'] == 'https://hoogeveen.raadsinformatie.nl/'
    sources.sync_source(db.one('SELECT * FROM sources WHERE id=?', (project['id'],)))
    refreshed = client.get('/api/projects/' + project['id']).json()
    assert refreshed['display_name'] == 'Gemeente Hoogeveen'
    episodes = client.get('/api/episodes?project=' + project['id']).json()
    assert [episode['kind'] for episode in episodes] == ['meeting']
    assert episodes[0]['title'] == 'Raadsavond'


GO_PORTAL = 'https://gemeenteraad.groningen.nl/'
ANUBIS_PAGE = ('<script id="anubis_challenge" type="application/json">'
               '{"rules":{"algorithm":"fast","difficulty":3},'
               '"challenge":{"id":"abc-123","randomData":"deadbeef"}}</script>')
GO_MEETING_PAGE = ('<title>gemeenteraad 16 september 2026</title><a class="download" href='
                   '"https://go-va-web.gemeenteoplossingen.nl/groningen_1/16-09-gemeenteraad.mp3"'
                   '>Geluidsbestand</a>')


def go_day(days_ago):
    return time.strftime('%Y-%m-%d', time.gmtime(time.time() - days_ago * 86400))


def go_meeting(ident, date, start, body, label=None):
    return {'id': ident, 'date': date, 'startTime': start, 'meetingLabel': label,
            'dmu': {'id': 1, 'name': body},
            'url': '/Vergaderingen/%s/%s/%s' % (body.replace(' ', '-'), date, start)}


def go_archive(monkeypatch, rows):
    """Serve the open data API this adapter reads; return the asked URLs."""
    asked = []

    def fetch_public(url, **options):
        asked.append(url)
        window = parse_qs(urlsplit(url).query)
        inside = [row for row in rows
                  if window.get('date_from', [''])[0] <= row['date'] <= window['date_to'][0]]
        return json.dumps({'result': {'totalCount': str(len(inside)), 'meetings': inside}}).encode()

    monkeypatch.setattr(meetings, 'fetch_public', fetch_public)
    monkeypatch.setattr(meetings, 'time', TimeWithoutSleep(meetings.time))
    return asked


class TimeWithoutSleep:
    """Keep the crawl delay out of the tests without hiding the rest of `time`."""

    def __init__(self, wrapped):
        self._wrapped = wrapped

    def __getattr__(self, name):
        return lambda *a, **k: None if name == 'sleep' else getattr(self._wrapped, name)(*a, **k)


def test_gemeenteoplossingen_archive_is_read_from_the_open_data_api(monkeypatch):
    rows = [go_meeting(3, go_day(4), '15:30', 'Politiek Vragenuur'),
            go_meeting(2, go_day(4), '13:00', 'Agendacommissie'),
            go_meeting(1, go_day(40), '16:30', 'gemeenteraad', label='Begroting 2027')]
    go_archive(monkeypatch, rows)
    title, entries = meetings.discover({'url': GO_PORTAL, 'kind': 'gemeenteoplossingen',
                                        'initial_limit': 3, 'initialized': 0})
    assert title == 'gemeenteraad.groningen.nl'
    # Nieuwste eerst, en binnen een dag telt de begintijd.
    assert [entry['external_id'] for entry in entries] == [
        'go:gemeenteraad.groningen.nl/3', 'go:gemeenteraad.groningen.nl/2',
        'go:gemeenteraad.groningen.nl/1']
    assert entries[0]['kind'] == 'meeting'
    assert entries[0]['author'] == 'Politiek Vragenuur'
    assert entries[0]['published'] == go_day(4).replace('-', '')
    assert entries[0]['url'] == GO_PORTAL.rstrip('/') + rows[0]['url']
    assert entries[2]['title'] == 'Begroting 2027'  # een eigen label wint van het orgaan


def test_gemeenteoplossingen_follow_up_check_stays_inside_the_first_catalogue(monkeypatch):
    rows = [go_meeting(3, go_day(4), '15:30', 'Politiek Vragenuur'),
            go_meeting(1, go_day(500), '16:30', 'gemeenteraad')]
    go_archive(monkeypatch, rows)
    portal = {'url': GO_PORTAL, 'kind': 'gemeenteoplossingen', 'initial_limit': 0}
    _, first = meetings.discover({**portal, 'initialized': 0})
    _, later = meetings.discover({**portal, 'initialized': 1})
    assert [entry['external_id'] for entry in first] == [
        'go:gemeenteraad.groningen.nl/3', 'go:gemeenteraad.groningen.nl/1']
    assert [entry['external_id'] for entry in later] == ['go:gemeenteraad.groningen.nl/3']


def test_gemeenteoplossingen_is_recognised_by_its_open_data_api(monkeypatch):
    monkeypatch.setattr(meetings, 'fetch_public',
                        lambda url, **options: json.dumps({'result': {'meetings': []}}).encode())
    assert meetings.canonical('https://gemeenteraad.groningen.nl/vergaderingen') == (
        'gemeenteoplossingen', GO_PORTAL)
    # Een portaal dat niet antwoordt is dit platform niet, en wordt niet verzonnen.
    monkeypatch.setattr(meetings, 'fetch_public',
                        Mock(side_effect=ValueError('geen API')))
    assert meetings.canonical('https://example.com/feed') is None
    with pytest.raises(ValueError, match='nog niet ondersteund'):
        meetings.canonical('https://example.com/vergaderingen', 'meetings')


def test_anubis_puzzle_is_solved_to_the_asked_number_of_zeroes():
    """De poort vraagt een hash met een aantal nullen voorop; even en oneven."""
    for difficulty in (2, 3, 4):
        response, nonce = meetings.anubis_nonce('deadbeef', difficulty)
        assert response.startswith('0' * difficulty)
        assert hashlib.sha256(('deadbeef' + str(nonce)).encode()).hexdigest() == response


def test_gemeenteoplossingen_recording_is_read_after_passing_the_gate(monkeypatch):
    asked = []

    def fetch_public(url, **options):
        asked.append(url)
        gate = len(asked) == 1 and meetings.ANUBIS_PASS not in url
        return (ANUBIS_PAGE if gate else GO_MEETING_PAGE).encode()

    monkeypatch.setattr(meetings, 'fetch_public', fetch_public)
    monkeypatch.setattr(meetings, 'ANUBIS_COOKIES', {})
    episode = {'url': GO_PORTAL + 'Vergaderingen/gemeenteraad/2026/16-september/16:30',
               'external_id': 'go:gemeenteraad.groningen.nl/5424'}
    url, fields, stream = meetings.audio(episode)
    assert url == 'https://go-va-web.gemeenteoplossingen.nl/groningen_1/16-09-gemeenteraad.mp3'
    assert fields == {} and stream is False  # een gewone mp3 op een open mediaserver
    # Het antwoord op de opgave gaat mee naar de poort, met de eigen naam intact.
    answer = parse_qs(urlsplit(asked[1]).query)
    assert meetings.ANUBIS_PASS in asked[1] and answer['redir'][0] == episode['url']
    assert hashlib.sha256(('deadbeef' + answer['nonce'][0]).encode()).hexdigest() \
        == answer['response'][0] == meetings.anubis_nonce('deadbeef', 3)[0]


def test_gemeenteoplossingen_meeting_without_a_recording_says_so(monkeypatch):
    monkeypatch.setattr(meetings, 'fetch_public',
                        lambda url, **options: b'<title>Agendacommissie</title>')
    episode = {'url': GO_PORTAL + 'Vergaderingen/Agendacommissie/2026/23-september/13:00',
               'external_id': 'go:gemeenteraad.groningen.nl/5696'}
    with pytest.raises(ValueError, match='geen downloadbare opname'):
        meetings.audio(episode)
    with pytest.raises(ValueError, match='bronverwijzing'):
        meetings.audio({'url': 'https://example.com/x', 'external_id': 'go:elders.nl/1'})


def transcribed_episode(client, url, project=None, **fields):
    """Add one episode with a finished transcript, optionally to an existing project."""
    if project is None:
        created = client.post('/api/projects', json={'url': url}).json()
        project, ident = created['project_id'], created['episode_id']
    else:
        ident = sources.add_video(url, project)['id']
    db.update('episodes', ident, status='transcribed', transcript='Tekst.', segments='[]', **fields)
    return project, ident


def test_projects_start_at_the_front_of_the_queue_and_keep_a_readable_order(client):
    first = client.post('/api/sources', json={'url': 'https://example.com/een'}).json()
    second = client.post('/api/sources', json={'url': 'https://example.com/twee'}).json()
    third = client.post('/api/sources', json={'url': 'https://example.com/drie'}).json()
    listed = client.get('/api/projects').json()
    assert [p['id'] for p in listed] == [third['id'], second['id'], first['id']]
    assert [p['priority'] for p in listed] == [0, 1, 2]

    reordered = client.put('/api/projects/order', json={'ids': [first['id'], third['id']]}).json()
    assert [p['id'] for p in reordered] == [first['id'], third['id'], second['id']]
    assert [p['priority'] for p in reordered] == [0, 1, 2]
    # Unknown or repeated ids must not corrupt the order.
    kept = client.put('/api/projects/order', json={'ids': [third['id'], 'bestaat-niet', third['id']]}).json()
    assert [p['id'] for p in kept] == [third['id'], first['id'], second['id']]


def test_queue_finishes_the_first_project_before_starting_the_next(client):
    later, later_episode = transcribed_episode(client, 'https://youtu.be/92wmvrQU7a0')
    sooner = client.post('/api/sources', json={'url': 'https://example.com/raad'}).json()['id']
    sooner_episode = sources.add_video('https://youtu.be/abcdefghijk', sooner)['id']
    client.put('/api/projects/order', json={'ids': [sooner, later]})

    # The first project still needs a transcript, so that comes before anything else.
    assert pipeline.next_episode()['id'] == sooner_episode
    db.update('episodes', sooner_episode, status='transcribed', transcript='Raad.', segments='[]')
    # Its entity analysis now outranks the other project's waiting work.
    assert pipeline.next_episode()['id'] == sooner_episode
    db.update('episodes', sooner_episode, status='completed', entities='[]')
    assert pipeline.next_episode()['id'] == later_episode

    # Moving the other project up changes what the queue picks next.
    db.update('episodes', sooner_episode, status='queued', transcript=None, entities=None)
    client.put('/api/projects/order', json={'ids': [later, sooner]})
    assert pipeline.next_episode()['id'] == later_episode


def test_a_blocked_project_does_not_stall_the_whole_queue(client):
    blocked = client.post('/api/sources', json={'url': 'https://example.com/raad'}).json()['id']
    stuck = sources.add_video('https://youtu.be/92wmvrQU7a0', blocked)['id']
    db.update('episodes', stuck, status='queued', retry_at=time.time() + 600)
    other, other_episode = transcribed_episode(client, 'https://youtu.be/abcdefghijk')
    client.put('/api/projects/order', json={'ids': [blocked, other]})
    assert pipeline.next_episode()['id'] == other_episode


def test_entity_analysis_waits_for_its_own_projects_delayed_transcript(client):
    project = client.post('/api/sources', json={'url': 'https://example.com/raad'}).json()['id']
    delayed = sources.add_video('https://youtu.be/92wmvrQU7a0', project)['id']
    db.update('episodes', delayed, status='queued', retry_at=time.time() + 600)
    transcribed_episode(client, 'https://youtu.be/abcdefghijk', project)
    # Ollama must not take the shared GPU while this project's Whisper retry is due.
    assert pipeline.next_episode() is None


def test_publication_dates_from_every_source_become_sortable():
    """The three sources each write their date differently."""
    assert db.published_seconds('20260922') == 1790035200.0          # YouTube en vergaderingen
    assert db.published_seconds('Tue, 02 Jun 2026 10:00:00 GMT') == 1780394400.0  # RSS
    assert db.published_seconds('2026-06-02T10:00:00+00:00') == 1780394400.0      # RSS, ISO
    assert db.published_seconds('20260922') > db.published_seconds('20260901')
    for unusable in (None, '', 'binnenkort', '99999999'):
        assert db.published_seconds(unusable) is None


def test_queue_takes_the_newest_episode_of_a_project_first(client):
    project = client.post('/api/sources', json={'url': 'https://example.com/raad'}).json()['id']
    dates = ['20260901', '20260922', '20260908']
    ids = {}
    for index, date in enumerate(dates):
        episode = sources.insert_episode({
            'external_id': 'cwc:raad/%s' % date, 'url': 'https://example.com/%d' % index,
            'kind': 'meeting', 'title': date, 'published': date}, project)
        ids[date] = episode['id']
    # Toevoegvolgorde is niet de verwerkvolgorde: de nieuwste vergadering gaat voor.
    assert pipeline.next_episode()['id'] == ids['20260922']
    db.update('episodes', ids['20260922'], status='transcribed', transcript='A.', segments='[]', entities='[]')
    assert pipeline.next_episode()['id'] == ids['20260908']
    # Ook de entiteitsronde loopt van nieuw naar oud.
    for date in dates:
        db.update('episodes', ids[date], status='transcribed', transcript='T.', segments='[]', entities=None)
    assert pipeline.next_episode()['id'] == ids['20260922']


def test_library_lists_the_newest_episode_first(client):
    project = client.post('/api/sources', json={'url': 'https://example.com/raad'}).json()['id']
    for index, date in enumerate(['20260901', '20260922', '20260908']):
        sources.insert_episode({'external_id': 'cwc:raad/%s' % date, 'url': 'https://example.com/%d' % index,
                                'kind': 'meeting', 'title': date, 'published': date}, project)
    assert [e['title'] for e in client.get('/api/episodes').json()] == ['20260922', '20260908', '20260901']


def test_existing_episodes_get_a_sortable_publication_date_on_upgrade(store):
    project = db.create_project('https://example.com/feed', 'rss', 'Podcast')
    sources.insert_episode({'external_id': 'rss:1', 'url': 'https://example.com/1', 'kind': 'rss',
                            'title': 'Oud', 'published': 'Mon, 01 Jun 2026 10:00:00 GMT'}, project)
    sources.insert_episode({'external_id': 'rss:2', 'url': 'https://example.com/2', 'kind': 'rss',
                            'title': 'Nieuw', 'published': 'Tue, 02 Jun 2026 10:00:00 GMT'}, project)
    with db.connect() as connection:  # terug naar een database van vóór deze kolom
        connection.execute('ALTER TABLE episodes DROP COLUMN published_at')
    db.init()
    stored = {row['title']: row['published_at'] for row in db.rows('SELECT title,published_at FROM episodes')}
    assert stored['Nieuw'] > stored['Oud'] > 0
