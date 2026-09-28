"""Import actual Lagos place names and coordinates from a bounded Overpass JSON export.

Run independently of the web app: python -m scripts.import_lagos_places lagos-places.json
The optional --fetch makes one bounded Overpass request; it is not a background job.
"""
import argparse
import json
import os
import urllib.request
from pathlib import Path

from app.db import database, init_db
from app.locations import normalize
from app.network import init_network

OVERPASS = 'https://overpass-api.de/api/interpreter'
BBOX = '6.1,2.7,6.9,4.7'  # Approximate Lagos bounds, not a legal state polygon.
TAGS = {
    'place': {'city', 'town', 'village', 'suburb', 'neighbourhood', 'quarter', 'hamlet', 'locality'},
    'amenity': {'school', 'university', 'college', 'hospital', 'marketplace', 'bus_station', 'ferry_terminal', 'townhall'},
    'shop': {'mall', 'department_store'},
    'tourism': {'attraction', 'museum'},
    'aeroway': {'aerodrome', 'terminal'},
    'railway': {'station', 'halt'},
}
QUERY = '[out:json][timeout:120];(' + ''.join(
    f'{kind}["{tag}"~"^({"|".join(sorted(values))})$"][name]({BBOX});'
    for kind in ('node', 'way', 'relation') for tag, values in TAGS.items()
) + ');out center tags;'


def import_elements(payload):
    elements = payload.get('elements')
    if not isinstance(elements, list):
        raise ValueError('Expected an Overpass JSON elements array')
    count = 0
    with database() as db:
        for element in elements:
            tags = element.get('tags') or {}
            name = str(tags.get('name') or '').strip()
            kind, osm_id = element.get('type'), element.get('id')
            coords = element.get('center') or element
            lat, lon = coords.get('lat'), coords.get('lon')
            if (not name or len(name) > 180 or kind not in ('node', 'way', 'relation')
                    or not isinstance(osm_id, int) or not isinstance(lat, (float, int))
                    or not isinstance(lon, (float, int))):
                continue
            if not (6.1 <= lat <= 6.9 and 2.7 <= lon <= 4.7):
                continue
            if tags.get('addr:state') and 'lagos' not in tags['addr:state'].casefold():
                continue
            category = next(((tag, tags[tag]) for tag, allowed in TAGS.items() if tags.get(tag) in allowed), None)
            if category is None:
                continue
            external_id = f'{kind}:{osm_id}'
            area = tags.get('addr:suburb') or tags.get('addr:city') or tags.get('is_in:city') or 'Lagos'
            display = f'{name}, {area}, Lagos, Nigeria' if area.casefold() != 'lagos' else f'{name}, Lagos, Nigeria'
            city = tags.get('addr:city') or 'Lagos'
            data = (display, float(lat), float(lon), category[1], city, normalize(name), external_id)
            row = db.execute("SELECT id FROM place_cache WHERE source='openstreetmap' AND external_id=? ORDER BY id LIMIT 1",(external_id,)).fetchone()
            if row:
                db.execute('''UPDATE place_cache SET display_name=?,latitude=?,longitude=?,place_type=?,city=?,
                    name_normalized=? WHERE id=?''',data[:6]+(row['id'],))
            else:
                db.execute('''INSERT INTO place_cache(query_normalized,display_name,latitude,longitude,place_type,
                    source,external_id,city,state,country,name_normalized)
                    VALUES(?,?,?,?,?,'openstreetmap',?,?,'Lagos','Nigeria',?)''',
                    (f'osm:{external_id}',)+data[:4]+(external_id,city,normalize(name)))
            count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file', nargs='?', type=Path)
    parser.add_argument('--fetch', action='store_true', help='Make one bounded Overpass request, then import')
    args = parser.parse_args()
    if args.fetch:
        if args.file:
            parser.error('Choose a file or --fetch')
        agent = os.getenv('MOVENAIJA_USER_AGENT')
        if not agent:
            parser.error('Set MOVENAIJA_USER_AGENT to an app name and contact URL before fetching')
        request = urllib.request.Request(OVERPASS, data=QUERY.encode(), headers={'User-Agent': agent})
        with urllib.request.urlopen(request, timeout=140) as response:
            payload = json.load(response)
    elif args.file:
        payload = json.loads(args.file.read_text(encoding='utf-8'))
    else:
        parser.error('Provide an Overpass JSON file or --fetch')
    init_db()
    init_network()
    print(f'Imported or refreshed {import_elements(payload)} mapped Lagos places. No transport routes were inferred.')


if __name__ == '__main__':
    main()
