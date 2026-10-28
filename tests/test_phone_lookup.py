import json
from unittest.mock import Mock

import pytest

from app import db, phone_lookup as lookup, pipeline, sources


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATA', tmp_path)
    db.init()
    lookup.init()
    episode = sources.add_video('https://youtu.be/92wmvrQU7a0')
    pipeline.scan_phone_mentions(episode['id'], [{'text': 'Bel 0570 633637.', 'start': 0}])
    return episode


def markup(obj):
    return '<script type="application/ld+json">'+json.dumps(obj)+'</script>'


def business(**overrides):
    return {'@type': 'HealthClub', 'name': 'Studio Perfect', 'telephone': '+31 (0)570 633637',
            'address': {'streetAddress': 'Ceintuurbaan 8A', 'postalCode': '7415 AL', 'addressLocality': 'Deventer'}, **overrides}


def test_exact_phone_is_tied_to_the_same_business_object():
    result = lookup.evidence_from_page('0570633637', markup({'@graph':[business()]}), 'https://example.org/')
    assert result['company'] == 'Studio Perfect'
    assert result['location'] == 'Ceintuurbaan 8A, 7415 AL, Deventer'
    assert lookup.evidence_from_page('0570633638', markup(business()), 'https://example.org/') is None
    assert lookup.evidence_from_page('0570633637', markup(business(telephone='', **{'@type':'Person'})), 'https://example.org/') is None
    unrelated = [business(telephone=''), {'@type':'Person','name':'Someone','telephone':'0570633637'}]
    assert lookup.evidence_from_page('0570633637', markup(unrelated), 'https://example.org/') is None


def test_conflicting_businesses_are_not_automatically_assigned():
    assert lookup.evidence_from_page('0570633637', markup([business(), business(name='Other')]), 'https://example.org/') is None


def test_official_contact_page_fallback_and_directory_rejection():
    page = '''<title>Contact | Studio Perfect</title><meta property="og:site_name" content="Studio Perfect">
        <a href="tel:+31570633637">Bel ons</a><p>Ceintuurbaan 8A, 7415 AL Deventer</p>'''
    result = lookup.evidence_from_page('0570633637', page, 'https://studioperfect.nl/contact/')
    assert result['company'] == 'Studio Perfect'
    assert 'Deventer' in result['location']
    assert lookup.evidence_from_page('0570633637', page, 'https://telefoonboek.nl/contact/') is None
    assert lookup.evidence_from_page('0570633638', page, 'https://studioperfect.nl/contact/') is None


def test_queue_processes_existing_and_future_mentions_once(database, monkeypatch):
    result = lookup.evidence_from_page('0570633637', markup(business()), 'https://example.org/')
    discover = Mock(return_value=result)
    monkeypatch.setattr(lookup, 'discover', discover)
    assert lookup.lookup_once()
    assert db.one('SELECT * FROM phone_verifications')['company'] == 'Studio Perfect'
    assert db.one('SELECT * FROM phone_lookups')['status'] == 'matched'
    assert not lookup.lookup_once()
    assert discover.call_count == 1
    pipeline.scan_phone_mentions(database['id'], [{'text':'Bel 030 6560480.', 'start':2}])
    discover.return_value = None
    assert lookup.lookup_once()
    assert discover.call_args.args == ('0306560480',)
    assert not lookup.lookup_once()


def test_global_pause_stops_new_company_lookups(database, monkeypatch):
    discover = Mock()
    monkeypatch.setattr(lookup, 'discover', discover)
    db.set_processing_paused(True)
    assert lookup.lookup_once() is False
    assert db.one('SELECT * FROM phone_lookups') is None
    discover.assert_not_called()


def test_lookup_failures_are_persisted_and_retried(database, monkeypatch):
    monkeypatch.setattr(lookup, 'discover', Mock(side_effect=RuntimeError('connection failed')))
    assert lookup.lookup_once()
    row = db.one('SELECT * FROM phone_lookups')
    assert row['status'] == 'error' and row['retry_at'] > row['checked']
    assert db.one('SELECT * FROM phone_verifications') is None
    assert not lookup.lookup_once()
    with db.connect() as conn:
        conn.execute('UPDATE phone_lookups SET retry_at=0')
    monkeypatch.setattr(lookup, 'discover', lambda _: None)
    assert lookup.lookup_once()
    assert db.one('SELECT * FROM phone_lookups')['attempts'] == 2


def test_search_parser_and_challenge_detection(monkeypatch):
    monkeypatch.setattr(lookup, 'fetch_public', lambda *a, **k: b'<a class="result-link" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fcontact">Result</a>')
    assert lookup.search_links('test', 'duckduckgo') == ['https://example.org/contact']
    monkeypatch.setattr(lookup, 'fetch_public', lambda *a, **k: b'<a href="https://ads.org">Ad</a><h2><a href="https://example.org/contact">Result</a></h2>')
    assert lookup.search_links('test', 'bing') == ['https://example.org/contact']
    monkeypatch.setattr(lookup, 'fetch_public', lambda *a, **k: b'<form id="challenge-form">Captcha</form>')
    with pytest.raises(RuntimeError):
        lookup.search_links('test', 'duckduckgo')


def test_search_snippet_alone_cannot_create_a_mapping(database, monkeypatch):
    monkeypatch.setattr(lookup, 'search_links', lambda *a: ['https://example.org/'])
    monkeypatch.setattr(lookup, 'fetch_public', lambda *a, **k: markup(business(telephone='0570633638')).encode())
    assert lookup.discover('0570633637') is None


def test_seed_does_not_overwrite_a_more_recent_verification(database):
    details = dict(pipeline.PHONE_VERIFICATIONS['0880237500'], company='Updated name', verified=9999999999)
    db.put_phone_verification('0880237500', **details)
    pipeline.seed_phone_verifications()
    assert db.one("SELECT company FROM phone_verifications WHERE phone='0880237500'")['company'] == 'Updated name'


def test_iban_split_across_segments_and_alphanumeric_codes_are_not_searched():
    segments = [{'text':'Mijn rekeningnummer is NL... 30... Rabo...', 'start':0},
                {'text':'0261... 6253... 3-1. Bel 030 6560480. Code 0885403828G980KG.', 'start':5}]
    assert [m['phone'] for m in pipeline.extract_phone_mentions(segments)] == ['0306560480']


def test_context_query_uses_company_name_without_call_instruction(database):
    pipeline.scan_phone_mentions(database['id'], [{'start':0,'text':'Bel Studio Perfect, 0570633637.'}])
    assert lookup.queries_for('0570633637')[0] == '"Studio Perfect" Deventer'
