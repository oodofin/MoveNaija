"""Published endpoint fare references. These are not transport-network edges."""
import json
import re
from functools import lru_cache
from pathlib import Path

SOURCE_FILE=Path(__file__).parent/'data'/'lamata_bri_fares_2026_03.json'

@lru_cache(maxsize=1)
def published_data():
    return json.loads(SOURCE_FILE.read_text(encoding='utf-8'))

def normalized(value):
    return re.sub(r'[^a-z0-9]', '', value.casefold())

def endpoints(label):
    # A multi-hyphen name may describe intermediate stops, so do not guess
    # which two places the published fare actually connects.
    parts=label.split('-')
    if len(parts)!=2: return None
    return ([normalized(x) for x in parts[0].split('/')],
            [normalized(x) for x in parts[1].split('/')])

def find_published_fares(origin, destination):
    source=published_data()
    o,d=normalized(origin),normalized(destination)
    if not o or not d: return []
    found={}
    for row in source['rows']:
        ends=endpoints(row['label'])
        if not ends: continue
        start,end=ends
        direction='listed' if o in start and d in end else 'reverse_listing' if o in end and d in start else None
        if not direction: continue
        key=(row['label'],row['mode'],direction)
        entry=found.setdefault(key,{'label':row['label'],'mode':row['mode'],'direction':direction,
            'published_amounts':[],'source':'LAMATA BRI fare list','source_url':source['source_url'],
            'effective_date':source['effective_date']})
        if row['fare'] not in entry['published_amounts']:
            entry['published_amounts'].append(row['fare'])
    return list(found.values())[:12]
