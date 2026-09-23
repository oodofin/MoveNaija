"""Import Lagos transport objects from an Overpass JSON export. Run separately from the web server.

python -m scripts.import_lagos_transport lagos.json
Use --fetch only for a deliberate one-off bounded query, with a contact User-Agent.
"""
import argparse
import json
import os
import urllib.request
from pathlib import Path

from app.db import database, init_db
from app.network import init_network

OVERPASS='https://overpass-api.de/api/interpreter'
BBOX='6.1,2.7,6.9,4.7'  # Approximate Lagos search bounds; not a legal state boundary.
QUERY=f'''[out:json][timeout:90];(node[highway=bus_stop]({BBOX});node[public_transport=platform]({BBOX});node[amenity=bus_station]({BBOX});node[railway=station]({BBOX});node[amenity=ferry_terminal]({BBOX});way[amenity=bus_station]({BBOX});way[railway=station]({BBOX}););out center tags;'''

def import_elements(payload):
    elements=payload.get('elements')
    if not isinstance(elements,list): raise ValueError('Expected Overpass JSON elements array')
    count=0
    with database() as db:
        for element in elements:
            tags=element.get('tags') or {}
            name=tags.get('name')
            coords=element.get('center') or element
            lat,lon=coords.get('lat'),coords.get('lon')
            kind=element.get('type')
            eid=element.get('id')
            if not name or kind not in ('node','way','relation') or not isinstance(eid,int) or not isinstance(lat,(float,int)) or not isinstance(lon,(float,int)):
                continue
            if not (6.1<=lat<=6.9 and 2.7<=lon<=4.7): continue
            external=f'{kind}:{eid}'
            typ='rail' if tags.get('railway')=='station' else 'ferry' if tags.get('amenity')=='ferry_terminal' else 'bus'
            source_url=f'https://www.openstreetmap.org/{kind}/{eid}'
            row=db.execute('SELECT id FROM stops WHERE source=? AND external_id=?',('openstreetmap',external)).fetchone()
            if row:
                db.execute('''UPDATE stops SET latitude=?,longitude=?,last_synced_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                    WHERE id=?''',(lat,lon,row['id']))
            else:
                # Legacy schema has a global unique name. Keep the source ID in the
                # display name so distinct OSM features cannot overwrite one another.
                display=f'{name} (OSM {external})'
                db.execute('''INSERT INTO stops(name,location,description,landmarks,transport_types,status,city,
                    latitude,longitude,stop_type,source,external_id,source_url,state,country,created_at,updated_at,imported_at,last_synced_at)
                    VALUES(?,?,'','','Bus','Active','Lagos',?,?,?,'openstreetmap',?,?,'Lagos','Nigeria',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)''',
                    (display,f'{name}, Lagos',lat,lon,typ,external,source_url))
            count+=1
    return count

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file',nargs='?',type=Path)
    parser.add_argument('--fetch',action='store_true',help='Fetch a bounded Overpass extract once')
    args=parser.parse_args()
    if args.fetch:
        if args.file: parser.error('Choose a file or --fetch')
        agent=os.getenv('MOVENAIJA_USER_AGENT')
        if not agent: parser.error('Set MOVENAIJA_USER_AGENT to an app name and contact URL before fetching')
        request=urllib.request.Request(OVERPASS,data=QUERY.encode(),headers={'User-Agent':agent})
        with urllib.request.urlopen(request,timeout=110) as response: payload=json.load(response)
    elif args.file: payload=json.loads(args.file.read_text())
    else: parser.error('Provide an Overpass JSON file or --fetch')
    init_db();init_network()
    print(f'Imported or refreshed {import_elements(payload)} named OSM stops. No route services were inferred.')

if __name__=='__main__': main()
