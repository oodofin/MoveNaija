"""Published endpoint fare references. These are not transport-network edges."""
import json
import re
from collections import defaultdict
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

def endpoint_name(label, side, alias):
    return next((name.strip() for name in label.split('-')[side].split('/') if normalized(name)==alias),alias)

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

def published_corridors(origin, destination, max_legs=3):
    """Find directed chains of published endpoint services only.

    Shared place names suggest a possible transfer; they do not establish a
    boarding platform, timetable, or guaranteed connection.
    """
    start,goal=normalized(origin),normalized(destination)
    if not start or not goal or start==goal: return []
    source=published_data()
    grouped={}
    for row in source['rows']:
        ends=endpoints(row['label'])
        if not ends: continue  # Multi-stop names cannot be safely decomposed.
        key=(row['label'],row['mode'])
        record=grouped.setdefault(key,{'label':row['label'],'mode':row['mode'],
            'from_names':ends[0],'to_names':ends[1],'amounts':set()})
        record['amounts'].add(row['fare'])
    adjacency=defaultdict(list)
    for record in grouped.values():
        for name in record['from_names']:
            adjacency[name].append(record)
    results=[]
    queue=[(start,[],frozenset([start]))]
    for _ in range(max_legs):
        next_queue=[]
        for node,legs,visited in queue:
            for record in adjacency[node]:
                for nxt in record['to_names']:
                    if nxt in visited: continue
                    # A service is listed in one order; the opposite direction
                    # must not become an inferred edge.
                    next_legs=legs+[(record,node,nxt)]
                    if nxt==goal:
                        key=tuple(x[0]['label'] for x in next_legs)
                        if not any(x['key']==key for x in results):
                            items=[]
                            for i,(service,a,b) in enumerate(next_legs):
                                amounts=sorted(service['amounts'])
                                items.append({'label':service['label'],'mode':service['mode'],
                                    'boarding_area':endpoint_name(service['label'],0,a),
                                    'alighting_area':endpoint_name(service['label'],1,b),
                                    'published_fare':amounts[0] if len(amounts)==1 else None,
                                    'conflicting_amounts':amounts if len(amounts)>1 else [],
                                    'source_url':source['source_url'],'effective_date':source['effective_date']})
                            known=all(s['published_fare'] is not None for s in items)
                            results.append({'key':key,'segments':items,'transfers':len(items)-1,
                                'published_fare_sum':sum(s['published_fare'] for s in items) if known else None,
                                'status':'Published endpoint services; boarding and transfers unconfirmed'})
                    else: next_queue.append((nxt,next_legs,visited|{nxt}))
        queue=next_queue
        if len(results)>=12: break
    results.sort(key=lambda x:(x['transfers'],x['published_fare_sum'] is None,x['published_fare_sum'] or 0))
    return [{k:v for k,v in r.items() if k!='key'} for r in results[:6]]
