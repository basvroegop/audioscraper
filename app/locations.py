import json
import re
from functools import lru_cache
from pathlib import Path

CONTINENTS = {
    'EU': 'Europa', 'AF': 'Afrika', 'AS': 'Azië', 'NA': 'Noord-Amerika',
    'SA': 'Zuid-Amerika', 'OC': 'Oceanië', 'AN': 'Antarctica',
}
NORTH = {'Groningen', 'Friesland', 'Drenthe'}


def _key(value):
    return ' '.join(value.casefold().split()).strip(' .,:;()[]')


@lru_cache(maxsize=1)
def _index():
    return json.loads(Path(__file__).with_name('location_index.json').read_text(encoding='utf-8'))


@lru_cache(maxsize=8192)
def classify_location(name):
    """Return deterministic offline geography for an extracted place name."""
    data = _index()
    match = data['places'].get(_key(name))
    if not match:
        # Entity detection sometimes includes a municipality prefix or a city
        # district suffix. Only use an embedded match for a clear multiword
        # phrase and prefer the longest known place name.
        cleaned = re.sub(r'^(?:de |het )?(?:gemeente |provincie )', '', _key(name))
        words = cleaned.replace('-', ' ').split()
        candidates = []
        for size in range(min(4, len(words)), 0, -1):
            for start in range(len(words) - size + 1):
                candidate = ' '.join(words[start:start + size])
                found = data['places'].get(candidate)
                if found and (size > 1 or len(candidate) >= 5):
                    candidates.append((size, len(candidate), found))
            if candidates:
                break
        match = max(candidates, default=(0, 0, None))[2]
    if not match:
        return None
    continent, country_code, province = match
    country = data['countries'].get(country_code, {}).get('name', '')
    return {
        'continent_code': continent,
        'continent': CONTINENTS.get(continent, continent),
        'country_code': country_code,
        'country': country,
        'province': province,
        'north_netherlands': province in NORTH,
    }


def matches_region(geography, region):
    if not region:
        return True
    if not geography:
        return region == 'unknown'
    if region == 'nl':
        return geography['country_code'] == 'NL'
    if region == 'north':
        return geography['north_netherlands']
    if region in {'Groningen', 'Friesland', 'Drenthe'}:
        return geography['province'] == region
    if region == 'europe':
        return geography['continent_code'] == 'EU'
    if region == 'outside-europe':
        return geography['continent_code'] != 'EU'
    if region.startswith('continent:'):
        return geography['continent_code'] == region.split(':', 1)[1]
    return False
