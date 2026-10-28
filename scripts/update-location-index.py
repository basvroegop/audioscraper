#!/usr/bin/env python3
"""Build the compact offline location index used by the dashboard.

Source data: GeoNames countryInfo/NL/cities15000 and Dutch territory names
from Unicode CLDR. The generated file is committed so production never calls
an external geocoder and location filters remain fast and private.
"""
import io
import json
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'app' / 'location_index.json'
URLS = {
    'NL.zip': 'https://download.geonames.org/export/dump/NL.zip',
    'cities15000.zip': 'https://download.geonames.org/export/dump/cities15000.zip',
    'countryInfo.txt': 'https://download.geonames.org/export/dump/countryInfo.txt',
    'territories.json': 'https://raw.githubusercontent.com/unicode-org/cldr-json/main/cldr-json/cldr-localenames-full/main/nl/territories.json',
}
PROVINCES = {
    '01': 'Drenthe', '02': 'Friesland', '03': 'Gelderland', '04': 'Groningen',
    '05': 'Limburg', '06': 'Noord-Brabant', '07': 'Noord-Holland',
    '08': 'Overijssel', '09': 'Utrecht', '10': 'Zeeland',
    '11': 'Zuid-Holland', '12': 'Flevoland',
}


def fetch(name):
    with urllib.request.urlopen(URLS[name], timeout=60) as response:
        return response.read()


def normalized(name):
    return ' '.join(name.casefold().split()).strip(' .,:;()[]')


def main():
    country_lines = fetch('countryInfo.txt').decode().splitlines()
    country_info = {}
    for line in country_lines:
        if line.startswith('#'):
            continue
        fields = line.split('\t')
        if len(fields) > 8:
            country_info[fields[0]] = {'english': fields[4], 'continent': fields[8]}
    territories = json.loads(fetch('territories.json'))['main']['nl']['localeDisplayNames']['territories']
    countries = {
        code: {'name': territories.get(code, details['english']), 'continent': details['continent']}
        for code, details in country_info.items()
    }
    places, scores = {}, {}

    def add(name, continent, country='', province='', score=0):
        key = normalized(name)
        if len(key) < 2 or key.isdigit() or len(key) > 100 or score < scores.get(key, -1):
            return
        places[key] = [continent, country, province]
        scores[key] = score

    for code, details in countries.items():
        info = country_info[code]
        add(details['name'], details['continent'], code, '', 2_000_000_000)
        add(info['english'], details['continent'], code, '', 1_900_000_000)
        add(code, details['continent'], code, '', 1_800_000_000)

    def add_geonames(archive, filename, nl_only=False):
        with zipfile.ZipFile(io.BytesIO(fetch(archive))) as bundle:
            lines = io.TextIOWrapper(bundle.open(filename), encoding='utf-8')
            for line in lines:
                fields = line.rstrip('\n').split('\t')
                if len(fields) < 15 or fields[6] != 'P':
                    continue
                code, admin1 = fields[8], fields[10]
                if nl_only and code != 'NL':
                    continue
                country = countries.get(code)
                if not country:
                    continue
                population = int(fields[14] or 0)
                province = PROVINCES.get(admin1, '') if code == 'NL' else ''
                # Prefer the most populous exact-name match. NL only wins an
                # exact population tie; otherwise Moscow and Kiel must not be
                # mistaken for tiny same-named Dutch localities.
                score = population * 2 + (1 if code == 'NL' else 0)
                names = [fields[1], fields[2], *fields[3].split(',')]
                for name in names:
                    add(name, country['continent'], code, province, score)

    add_geonames('cities15000.zip', 'cities15000.txt')
    add_geonames('NL.zip', 'NL.txt', nl_only=True)

    for province in PROVINCES.values():
        add(province, 'EU', 'NL', province, 2_100_000_000)
    for name in ('Nederland', 'Holland', 'The Netherlands'):
        add(name, 'EU', 'NL', '', 2_100_000_000)
    for name, continent, country in (
        ('Engeland', 'EU', 'GB'), ('Schotland', 'EU', 'GB'), ('Wales', 'EU', 'GB'),
        ('Amerika', 'NA', 'US'), ('Verenigde Staten', 'NA', 'US'),
        ('Europa', 'EU', ''), ('Afrika', 'AF', ''), ('Azië', 'AS', ''),
        ('Noord-Amerika', 'NA', ''), ('Zuid-Amerika', 'SA', ''),
        ('Oceanië', 'OC', ''), ('Antarctica', 'AN', ''),
    ):
        add(name, continent, country, '', 2_100_000_000)

    payload = {'countries': countries, 'places': dict(sorted(places.items()))}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print(f'{len(places)} namen geschreven naar {OUT} ({OUT.stat().st_size / 1024 / 1024:.1f} MiB)')


if __name__ == '__main__':
    main()
