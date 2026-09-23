"""Stop-based transport graph. Coordinates are optional and never stored for users."""
import heapq
import math
import re
from collections import defaultdict
from datetime import date

from .db import database

MODES = {'Danfo', 'Bus', 'BRT', 'Keke', 'Rail', 'Ferry', 'Walking'}
STOP_TYPES = {'bus', 'brt', 'keke', 'rail', 'ferry', 'other'}

def init_network():
    with database() as db:
        for table, columns in {
            'stops': {
                'source': "TEXT NOT NULL DEFAULT 'sample'",
                'external_id': 'TEXT',
                'imported_at': 'TEXT',
                'last_synced_at': 'TEXT',
                'verification_method': 'TEXT',
                'state': "TEXT NOT NULL DEFAULT 'Lagos'",
                'country': "TEXT NOT NULL DEFAULT 'Nigeria'",
                'alternative_names': "TEXT NOT NULL DEFAULT ''",
                'area': "TEXT NOT NULL DEFAULT ''",
                'local_government_area': "TEXT NOT NULL DEFAULT ''",
                'stop_type': "TEXT NOT NULL DEFAULT 'bus'",
                'verified': 'INTEGER NOT NULL DEFAULT 0',
                'source_url': "TEXT NOT NULL DEFAULT ''",
                'created_at': 'TEXT',
                'updated_at': 'TEXT',
            },
            'routes': {
                'external_id': 'TEXT',
                'imported_at': 'TEXT',
                'last_synced_at': 'TEXT',
                'verification_method': 'TEXT',
                'state': "TEXT NOT NULL DEFAULT 'Lagos'",
                'country': "TEXT NOT NULL DEFAULT 'Nigeria'",
                'name': "TEXT NOT NULL DEFAULT ''",
                'operator': "TEXT NOT NULL DEFAULT ''",
                'estimated_max_fare': 'INTEGER',
                'verified': 'INTEGER NOT NULL DEFAULT 0',
                'source_url': "TEXT NOT NULL DEFAULT ''",
                'last_verified_at': 'TEXT',
                'duration_known': 'INTEGER NOT NULL DEFAULT 1',
            },
        }.items():
            present = {r['name'] for r in db.execute(f'PRAGMA table_info({table})')}
            for name, declaration in columns.items():
                if name not in present:
                    db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')
        db.executescript('''
            CREATE TABLE IF NOT EXISTS fare_reports (
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                route_id INTEGER NOT NULL REFERENCES routes(id), amount INTEGER NOT NULL CHECK(amount>0),
                transport_type TEXT NOT NULL, paid_on TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, reviewed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_route_stops_stop ON route_stops(stop_id,route_id);
            CREATE INDEX IF NOT EXISTS idx_stops_coords ON stops(latitude,longitude);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_stops_source_external ON stops(source,external_id) WHERE external_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_stops_source_status ON stops(source,status);
            CREATE INDEX IF NOT EXISTS idx_routes_source_external ON routes(source,external_id);
            CREATE TABLE IF NOT EXISTS place_cache (
                id INTEGER PRIMARY KEY, query_normalized TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL,
                place_type TEXT, source TEXT NOT NULL, external_id TEXT,
                city TEXT, state TEXT, country TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_used_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_places_name ON place_cache(display_name);
            CREATE TABLE IF NOT EXISTS route_discoveries (
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                starting_stop TEXT NOT NULL, destination_stop TEXT NOT NULL,
                transport_type TEXT NOT NULL, transfer_details TEXT NOT NULL DEFAULT '',
                approximate_fare INTEGER, instructions TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, reviewed_at TEXT
            );
        ''')
        # LAMATA publishes both directions and all five station names. Do not
        # guess coordinates or fares; those remain unknown until sourced.
        source='https://www.lamata-ng.com/blue-line-train-schedule/'
        names=['Marina Station','National Theatre Station','Iganmu Station','Alaba Station','Mile 2 Station']
        ids=[]
        for name in names:
            row=db.execute('SELECT id FROM stops WHERE name=?',(name,)).fetchone()
            if row:
                ids.append(row['id'])
                continue
            cur=db.execute('''INSERT INTO stops(name,location,description,transport_types,status,city,
                alternative_names,area,stop_type,verified,source_url,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)''',
                (name,name+', Lagos','Lagos Blue Line station','Rail','Active','Lagos',
                 name.replace(' Station',''),name.replace(' Station',''),'rail',1,source))
            ids.append(cur.lastrowid)
        for ordered in (ids,list(reversed(ids))):
            start,end=ordered[0],ordered[-1]
            route_name=f"Blue Line: {names[ids.index(start)]} → {names[ids.index(end)]}"
            row=db.execute('SELECT id FROM routes WHERE name=?',(route_name,)).fetchone()
            if row: continue
            cur=db.execute('''INSERT INTO routes(origin,destination,transport_type,estimated_fare,estimated_duration,
                transfers,instructions,status,source,city,name,operator,verified,source_url,last_verified_at)
                VALUES(?,?,?,0,18,0,?,'Published route','verified','Lagos',?,'LAMATA',1,?,CURRENT_DATE)''',
                (names[ids.index(start)], names[ids.index(end)], 'Rail',
                 'Board at the station.|Get down at your destination station.',route_name,source))
            db.executemany('INSERT INTO route_stops(route_id,stop_id,position) VALUES(?,?,?)',
                           [(cur.lastrowid,sid,n) for n,sid in enumerate(ordered,1)])

def distance_m(lat1, lon1, lat2, lon2):
    r1, r2 = math.radians(lat1), math.radians(lat2)
    dlat, dlon = r2-r1, math.radians(lon2-lon1)
    a = math.sin(dlat/2)**2 + math.cos(r1)*math.cos(r2)*math.sin(dlon/2)**2
    return round(6371000 * 2 * math.atan2(math.sqrt(a),math.sqrt(1-a)))

def nearby(lat, lon, limit=8, radius=None):
    with database() as db:
        # Bounding box keeps the geographic lookup bounded even after a large import.
        search_radius=radius or 3000
        dlat=search_radius/111000
        dlon=search_radius/(111000*max(.1,math.cos(math.radians(lat))))
        rows=[dict(r) for r in db.execute("SELECT * FROM stops WHERE latitude BETWEEN ? AND ? AND longitude BETWEEN ? AND ? AND status='Active' AND city='Lagos'",(lat-dlat,lat+dlat,lon-dlon,lon+dlon))]
        for row in rows:
            row['distance_m']=distance_m(lat,lon,row['latitude'],row['longitude'])
            row['routes_served']=[dict(r) for r in db.execute('''SELECT routes.id,routes.name,routes.origin,routes.destination,routes.transport_type
                FROM route_stops JOIN routes ON routes.id=route_stops.route_id
                WHERE route_stops.stop_id=? AND routes.active=1 AND routes.source!='sample' ORDER BY routes.id''',(row['id'],))]
    return sorted((r for r in rows if r['distance_m']<=search_radius),key=lambda x:x['distance_m'])[:limit]

def suggestions(query, limit=8):
    query=query.strip()
    if len(query)<2:
        return []
    pattern='%'+re.sub(r'([%_\\])',r'\\\1',query)+'%'
    with database() as db:
        return [dict(r) for r in db.execute('''SELECT id,name,alternative_names,area,location,latitude,longitude,stop_type,verified
            FROM stops WHERE status='Active' AND city='Lagos' AND (name LIKE ? ESCAPE '\\' OR alternative_names LIKE ? ESCAPE '\\' OR area LIKE ? ESCAPE '\\' OR landmarks LIKE ? ESCAPE '\\')
            ORDER BY verified DESC,name LIMIT ?''',(pattern,pattern,pattern,pattern,limit))]

def resolve(db, query):
    """A stop name, common name or area; only actual stops become graph nodes."""
    term=query.strip().casefold()
    rows=db.execute("SELECT id,name,alternative_names,area,landmarks,latitude,longitude FROM stops WHERE status='Active' AND city='Lagos'").fetchall()
    exact=[]; partial=[]
    for row in rows:
        names=[row['name'],row['area'],row['landmarks'],*[a.strip() for a in row['alternative_names'].split(',')]]
        if any(term==str(n).casefold() for n in names if n): exact.append(dict(row))
        elif any(term in str(n).casefold() for n in names if n): partial.append(dict(row))
    return exact or partial[:4]

def journeys(origin='', destination='', lat=None, lon=None, origin_place=None, destination_place=None):
    with database() as db:
        targets=nearby(destination_place['latitude'],destination_place['longitude'],limit=8,radius=2000) if destination_place else resolve(db,destination)
        if not targets:
            return {'options':[], 'reason':'We found the location, but transport information nearby is not available yet.' if destination_place else 'We could not find this location. Try another spelling or a nearby landmark.', 'nearby_destination_stops':[]}
        if lat is not None and lon is not None:
            starts=nearby(lat,lon,limit=8,radius=2000)
            if not starts:
                return {'options':[], 'reason':'We found the location, but transport information nearby is not available yet.', 'nearby_destination_stops':targets}
        elif origin_place:
            starts=nearby(origin_place['latitude'],origin_place['longitude'],limit=8,radius=2000)
            if not starts:
                return {'options':[], 'reason':'We found the location, but transport information nearby is not available yet.', 'nearby_destination_stops':targets}
        else:
            starts=resolve(db,origin)
            if not starts:
                return {'options':[], 'reason':'We could not find this location. Try another spelling or a nearby landmark.', 'nearby_destination_stops':targets}
        route_rows=[dict(r) for r in db.execute("SELECT * FROM routes WHERE active=1 AND source!='sample' AND city='Lagos'")]
        if not route_rows:
            return {'options':[], 'reason':'We found nearby transport stops, but we do not yet have enough reviewed route information for this journey.', 'nearby_destination_stops':targets}
        route_by_id={r['id']:r for r in route_rows}
        stops={r['id']:dict(r) for r in db.execute("SELECT id,name,latitude,longitude,stop_type,verified FROM stops WHERE status='Active'")}
        adj=defaultdict(list)
        route_stops=defaultdict(list)
        for row in db.execute('SELECT route_id,stop_id,position FROM route_stops ORDER BY route_id,position'):
            if row['route_id'] in route_by_id:
                route_stops[row['route_id']].append(row['stop_id'])
        for rid, ordered in route_stops.items():
            if len(ordered)<2: continue
            for a,b in zip(ordered,ordered[1:]):
                if a!=b and a in stops and b in stops:
                    adj[a].append((b,rid))
        target_ids={t['id'] for t in targets}
        target_walks={t['id']:t.get('distance_m',0) for t in targets}
        results=[]; serial=0
        # Prioritize fewer transfers, then fewer stops. Hard bounds prevent runaway cycles.
        queue=[]
        for start in starts:
            walk=start.get('distance_m',0)
            heapq.heappush(queue,(walk/80,0,serial,start['id'],None,[],walk,(start['id'],)))
            serial+=1
        best={}; examined=0
        while queue and examined<25000 and len(results)<12:
            score,transfers,_,node,rid,edges,walk,visited=heapq.heappop(queue)
            examined+=1
            if node in target_ids and edges:
                key=tuple((x[0],x[1],x[2]) for x in edges)
                if not any(r[0]==key for r in results): results.append((key,edges,walk))
                continue
            if len(edges)>=35: continue
            for nxt,new_rid in adj[node]:
                if nxt in visited: continue
                next_transfers=transfers+int(rid is not None and rid!=new_rid)
                if next_transfers>3: continue
                next_score=score+1+(12 if rid is not None and rid!=new_rid else 0)
                state=(nxt,new_rid,next_transfers)
                if best.get(state,float('inf'))<next_score-1:continue
                best[state]=min(best.get(state,float('inf')),next_score)
                heapq.heappush(queue,(next_score,next_transfers,serial,nxt,new_rid,edges+[(node,nxt,new_rid)],walk,visited+(nxt,)))
                serial+=1
        options=[]
        for _, edges,walk in results:
            segments=[]
            for a,b,rid in edges:
                route=route_by_id[rid]
                if segments and segments[-1]['route_id']==rid:
                    segments[-1]['alight_stop']=stops[b]
                    segments[-1]['via'].append(stops[b]['name'])
                    segments[-1]['path_stops'].append(stops[b])
                else:
                    segments.append({'route_id':rid,'route_name':route['name'] or f"{route['origin']} → {route['destination']}",
                        'mode':route['transport_type'],'board_stop':stops[a],'alight_stop':stops[b],
                        'towards':route['destination'],'via':[stops[a]['name'],stops[b]['name']],
                        'path_stops':[stops[a],stops[b]],
                        'fare_min':route['estimated_fare'] if route['estimated_fare'] else None,
                        'fare_max':route['estimated_max_fare'] or route['estimated_fare'] or None,
                        'duration':route['estimated_duration'] if route['duration_known'] else None,
                        'verified':bool(route['verified']),'source':route['source'],'source_url':route['source_url'],
                        'last_updated':route['last_updated']})
            for segment in segments:
                ordered=route_stops[segment['route_id']]
                if (segment['board_stop']['id'],segment['alight_stop']['id']) != (ordered[0],ordered[-1]):
                    # A published end-to-end fare or duration cannot be assumed
                    # to apply to a shorter portion of that service.
                    segment['fare_min']=segment['fare_max']=segment['duration']=None
            fare_known=all(s['fare_min'] is not None for s in segments)
            duration_known=all(s['duration'] is not None for s in segments)
            end=segments[-1]['alight_stop']
            target_walk=target_walks.get(end['id']) if destination_place else None
            options.append({'segments':segments,'transfers':len(segments)-1,
                'fare_min':sum(s['fare_min'] for s in segments) if fare_known else None,
                'fare_max':sum(s['fare_max'] for s in segments) if fare_known else None,
                'duration_min':sum(s['duration'] for s in segments) if duration_known else None,
                'walk_to_board_m':walk if lat is not None or origin_place else None,'walk_to_destination_m':target_walk,
                'all_verified':all(s['verified'] and s['board_stop']['verified'] and s['alight_stop']['verified'] for s in segments),'origin':segments[0]['board_stop']['name'],
                'destination':end['name']})
        # Deduplicate same boarding service sequence and stops, then present varied choices.
        unique={}
        for option in options:
            key=tuple((s['route_id'],s['board_stop']['id'],s['alight_stop']['id']) for s in option['segments'])
            unique.setdefault(key,option)
        return {'options':list(unique.values())[:6],
            'reason':None if unique else 'We found nearby transport stops, but we do not yet have enough reviewed route information for this journey.',
            'searched_stops':len(starts),'nearby_origin_stops':starts,'nearby_destination_stops':targets}
