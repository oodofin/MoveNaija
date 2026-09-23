"""Place resolution. Public Nominatim is used for submitted searches, never autocomplete."""
import json
import os
import threading
import time
import urllib.parse
import urllib.request

from .db import database

_lock = threading.Lock()
_last_request = 0.0

def normalize(query):
    return ' '.join(query.casefold().strip().split())

def cached_suggestions(query):
    term = '%' + normalize(query).replace('\\','\\\\').replace('%','\\%').replace('_','\\_') + '%'
    with database() as db:
        return [dict(row) for row in db.execute("SELECT display_name,latitude,longitude,place_type,source,external_id,city,state,country FROM place_cache WHERE query_normalized LIKE ? ESCAPE '\\' OR display_name LIKE ? ESCAPE '\\' LIMIT 8",(term,term))]

def resolve_place(query):
    global _last_request
    key=normalize(query)
    if len(key)<2 or len(key)>100:
        return None
    with database() as db:
        row=db.execute('SELECT * FROM place_cache WHERE query_normalized=?',(key,)).fetchone()
        if row:
            db.execute('UPDATE place_cache SET last_used_at=CURRENT_TIMESTAMP WHERE id=?',(row['id'],))
            return dict(row)
    # Configure a different Nominatim-compatible endpoint for production scale.
    base=os.getenv('MOVENAIJA_GEOCODER_URL','https://nominatim.openstreetmap.org/search')
    if not base.startswith('https://'):
        raise ValueError('Geocoder URL must use HTTPS')
    agent=os.getenv('MOVENAIJA_USER_AGENT','MoveNaija/0.2 (https://github.com/oodofin/MoveNaija)')
    params=urllib.parse.urlencode({'q':query+', Lagos, Nigeria','format':'jsonv2','addressdetails':1,'limit':5,'countrycodes':'ng',
                                   'viewbox':'2.7,6.9,4.7,6.1','bounded':0})
    try:
        with _lock:
            delay=max(0,1.1-(time.monotonic()-_last_request))
            if delay: time.sleep(delay)
            _last_request=time.monotonic()
            req=urllib.request.Request(base+'?'+params,headers={'User-Agent':agent,'Accept':'application/json'})
            with urllib.request.urlopen(req,timeout=6) as response:
                matches=json.load(response)
    except (OSError,ValueError,TimeoutError):
        return None
    # Reject results outside Lagos; a nearby non-Lagos match is not a Lagos destination.
    for item in matches:
        try: lat,lon=float(item['lat']),float(item['lon'])
        except (KeyError,ValueError,TypeError): continue
        if not (6.1<=lat<=6.9 and 2.7<=lon<=4.7): continue
        address=item.get('address') or {}
        if address.get('country_code','ng')!='ng': continue
        if address.get('state') and 'lagos' not in address['state'].casefold(): continue
        result=dict(query_normalized=key,display_name=item['display_name'],latitude=lat,longitude=lon,
                    place_type=item.get('type'),source='openstreetmap',external_id=f"{item.get('osm_type','')}:{item.get('osm_id','')}",
                    city=address.get('city') or address.get('town') or address.get('suburb') or 'Lagos',
                    state=address.get('state') or 'Lagos',country=address.get('country') or 'Nigeria')
        with database() as db:
            db.execute('''INSERT OR IGNORE INTO place_cache(query_normalized,display_name,latitude,longitude,place_type,source,external_id,city,state,country)
                VALUES(:query_normalized,:display_name,:latitude,:longitude,:place_type,:source,:external_id,:city,:state,:country)''',result)
        return result
    return None
