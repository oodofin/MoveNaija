import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field, field_validator

from .db import database, init_db
from .network import MODES, STOP_TYPES, init_network, journeys, nearby, suggestions
from .locations import resolve_place, cached_suggestions
from .fares import find_published_fares

ROOT = Path(__file__).resolve().parent
COOKIE = 'movenaija_session'
REPORT_TYPES = {'fare_change','incorrect_route','stop_change','road_closure','heavy_traffic','disruption','incorrect_information'}
_hits = defaultdict(deque)

@asynccontextmanager
async def lifespan(app):
    init_db()
    init_network()
    yield

app = FastAPI(title='MoveNaija', lifespan=lifespan)
app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')

@app.get('/')
def home():
    return FileResponse(ROOT / 'static' / 'index.html')

@app.middleware('http')
async def security(request: Request, call_next):
    if request.url.path.startswith('/api/'):
        key = (request.client.host if request.client else 'unknown', request.url.path.split('/')[2])
        now = time.monotonic()
        hits = _hits[key]
        while hits and hits[0] < now - 60:
            hits.popleft()
        limit = 12 if key[1] == 'auth' else 120
        if len(hits) >= limit:
            return Response('Too many requests. Try again shortly.', status_code=429)
        hits.append(now)
        if request.method in {'POST','PUT','PATCH','DELETE'}:
            origin = request.headers.get('origin')
            if origin and origin != str(request.base_url).rstrip('/'):
                return Response('Invalid origin', status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' data: https://*.tile.openstreetmap.org https://unpkg.com; style-src 'self' https://unpkg.com; script-src 'self' https://unpkg.com; connect-src 'self'; object-src 'none'; base-uri 'self'"
    return response

class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)

class Registration(Credentials):
    name: str = Field(min_length=2, max_length=80)

class ReportInput(BaseModel):
    type: str
    description: str = Field(min_length=15, max_length=1200)
    location: str = Field(min_length=2, max_length=120)
    route_id: int | None = None

class RouteInput(BaseModel):
    origin: str = Field(min_length=2, max_length=100)
    destination: str = Field(min_length=2, max_length=100)
    transport_type: str = Field(min_length=2, max_length=40)
    estimated_fare: int = Field(ge=0, le=1000000)
    estimated_duration: int | None = Field(default=None, ge=1, le=1440)
    transfers: int = Field(ge=0, le=10)
    instructions: str = Field(min_length=10, max_length=3000)
    status: str = Field(default='Available', max_length=60)
    source: str = Field(default='community', pattern='^(sample|community|verified)$')
    city: str = Field(default='Lagos', min_length=2, max_length=80)
    name: str = Field(default='', max_length=120)
    operator: str = Field(default='', max_length=120)
    estimated_max_fare: int | None = Field(default=None, ge=0, le=1000000)
    source_url: str = Field(default='', max_length=500)
    verified: bool = False
    stop_ids: list[int] = Field(default_factory=list, max_length=100)
    @field_validator('instructions')
    @classmethod
    def steps(cls, value):
        if not all(p.strip() for p in value.split('|')):
            raise ValueError('Separate nonempty steps with |')
        return value

    @field_validator('transport_type')
    @classmethod
    def mode(cls, value):
        if value not in MODES:
            raise ValueError('Choose a supported transport mode.')
        return value

    @field_validator('source_url')
    @classmethod
    def safe_source(cls, value):
        if value and not value.startswith('https://'):
            raise ValueError('Source link must use HTTPS.')
        return value

class StopInput(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    location: str = Field(min_length=2, max_length=150)
    description: str = Field(default='', max_length=500)
    landmarks: str = Field(default='', max_length=150)
    transport_types: str = Field(default='Bus', max_length=100)
    status: str = Field(default='Active', max_length=50)
    city: str = Field(default='Lagos', max_length=80)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    alternative_names: str = Field(default='', max_length=300)
    area: str = Field(default='', max_length=100)
    local_government_area: str = Field(default='', max_length=100)
    stop_type: str = Field(default='bus', pattern='^(bus|brt|keke|rail|ferry|other)$')
    verified: bool = False
    source_url: str = Field(default='', max_length=500)

    @field_validator('source_url')
    @classmethod
    def safe_source(cls, value):
        if value and not value.startswith('https://'):
            raise ValueError('Source link must use HTTPS.')
        return value

class RoleInput(BaseModel):
    role: str = Field(pattern='^(user|admin)$')

class Decision(BaseModel):
    status: str = Field(pattern='^(approved|rejected)$')

class FareReportInput(BaseModel):
    route_id: int
    amount: int = Field(gt=0, le=1000000)
    transport_type: str
    paid_on: str
    note: str = Field(default='', max_length=500)

    @field_validator('paid_on')
    @classmethod
    def valid_date(cls, value):
        from datetime import date
        try:
            paid = date.fromisoformat(value)
        except ValueError:
            raise ValueError('Enter a valid date.')
        if paid > date.today() or (date.today() - paid).days > 90:
            raise ValueError('Fare report date must be within the past 90 days.')
        return value

class OrderedStops(BaseModel):
    stop_ids: list[int] = Field(min_length=2, max_length=100)

def hash_password(password):
    salt = os.urandom(16)
    value = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return f'{salt.hex()}:{value.hex()}'

def check_password(password, stored):
    try:
        salt, expected = stored.split(':')
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False

def issue_session(response, user_id, request):
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    with database() as db:
        db.execute('INSERT INTO sessions VALUES(?,?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), user_id, csrf, expires))
    response.set_cookie(COOKIE, token, httponly=True, secure=request.url.scheme == 'https', samesite='lax', max_age=604800)
    return csrf

def current_user(request: Request):
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, 'Please log in first.')
    with database() as db:
        user = db.execute('SELECT users.id,name,email,role,created_at,csrf_token FROM sessions JOIN users ON users.id=sessions.user_id WHERE token_hash=? AND expires_at>?', (hashlib.sha256(token.encode()).hexdigest(), datetime.now(timezone.utc).isoformat())).fetchone()
    if not user:
        raise HTTPException(401, 'Your session has expired. Please log in again.')
    if request.method in {'POST','PUT','PATCH','DELETE'} and not hmac.compare_digest(request.headers.get('X-CSRF-Token',''), user['csrf_token']):
        raise HTTPException(403, 'Missing or invalid security token.')
    return dict(user)

def admin(user=Depends(current_user)):
    if user['role'] != 'admin':
        raise HTTPException(403, 'Administrator access required.')
    return user

def public_user(user):
    return {k:user[k] for k in ('id','name','email','role','created_at')}

def route_dict(row):
    item = dict(row)
    if not item.get('duration_known', 1):
        item['estimated_duration'] = None
    item['instructions'] = item['instructions'].split('|')
    with database() as db:
        item['stops'] = [dict(r) for r in db.execute('SELECT stops.id,stops.name,stops.location,route_stops.position FROM route_stops JOIN stops ON stops.id=route_stops.stop_id WHERE route_id=? ORDER BY position', (item['id'],))]
    return item

@app.post('/api/auth/register', status_code=201)
def register(data: Registration, response: Response, request: Request):
    name = data.name.strip()
    if len(name) < 2:
        raise HTTPException(422, 'Enter your name.')
    try:
        with database() as db:
            cursor = db.execute('INSERT INTO users(name,email,password_hash) VALUES(?,?,?)', (name,data.email.lower(),hash_password(data.password)))
            user_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(409, 'An account already uses this email.')
    csrf = issue_session(response,user_id,request)
    with database() as db:
        user = db.execute('SELECT id,name,email,role,created_at FROM users WHERE id=?',(user_id,)).fetchone()
    return {'user':dict(user),'csrf_token':csrf}

@app.post('/api/auth/login')
def login(data: Credentials, response: Response, request: Request):
    with database() as db:
        user = db.execute('SELECT * FROM users WHERE email=?',(data.email.lower(),)).fetchone()
    if not user or not check_password(data.password,user['password_hash']):
        raise HTTPException(401, 'Incorrect email or password.')
    return {'user':public_user(user),'csrf_token':issue_session(response,user['id'],request)}

@app.get('/api/auth/me')
def me(user=Depends(current_user)):
    return {'user':public_user(user),'csrf_token':user['csrf_token']}

@app.post('/api/auth/logout')
def logout(request: Request, response: Response, user=Depends(current_user)):
    token = request.cookies.get(COOKIE)
    with database() as db:
        db.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),))
    response.delete_cookie(COOKIE)
    return {'ok':True}

@app.get('/api/routes')
def routes(origin: str='', destination: str=''):
    origin, destination = origin.strip(), destination.strip()
    if len(origin)>100 or len(destination)>100:
        raise HTTPException(422,'Location is too long.')
    sql = 'SELECT * FROM routes WHERE active=1'
    params=[]
    for field,value in (('origin',origin),('destination',destination)):
        if value:
            sql += f' AND {field} LIKE ? ESCAPE \'\\\''
            params.append('%'+re.sub(r'([%_\\])',r'\\\1',value)+'%')
    sql += ' ORDER BY estimated_fare ASC LIMIT 50'
    with database() as db:
        rows = db.execute(sql,params).fetchall()
    return [route_dict(r) for r in rows]

@app.get('/api/routes/{route_id}')
def route_detail(route_id:int):
    with database() as db:
        row=db.execute('SELECT * FROM routes WHERE id=? AND active=1',(route_id,)).fetchone()
    if not row:
        raise HTTPException(404,'Route not found.')
    return route_dict(row)

@app.get('/api/stops')
def stops():
    with database() as db:
        rows = [dict(r) for r in db.execute('SELECT * FROM stops ORDER BY name')]
        for stop in rows:
            stop['routes_served'] = [dict(r) for r in db.execute('SELECT routes.id,routes.origin,routes.destination FROM route_stops JOIN routes ON routes.id=route_stops.route_id WHERE route_stops.stop_id=? AND routes.active=1 ORDER BY routes.origin', (stop['id'],))]
        return rows

@app.get('/api/locations/suggest')
def suggest(q: str = ''):
    if len(q) > 100:
        raise HTTPException(422, 'Search is too long.')
    if len(q.strip())<2: return []
    return suggestions(q) + [{'name':p['display_name'],'area':p['city'] or p['state'], 'location':p['display_name'],
                               'latitude':p['latitude'],'longitude':p['longitude'],'source':p['source']} for p in cached_suggestions(q)]

@app.get('/api/locations/search')
def location_search(q: str):
    if not 2 <= len(q.strip()) <= 100:
        raise HTTPException(422,'Enter at least two characters.')
    place=resolve_place(q)
    if not place: raise HTTPException(404,'We could not find this location. Try another spelling or a nearby landmark.')
    return place

@app.get('/api/fares/published')
def published_fares(origin: str, destination: str):
    if not (2<=len(origin.strip())<=100 and 2<=len(destination.strip())<=100):
        raise HTTPException(422,'Enter an origin and destination.')
    return find_published_fares(origin,destination)

def plan(origin, destination, lat=None, lon=None):
    # Stop aliases remain usable even before coordinates are sourced.
    from .network import resolve
    with database() as db:
        known_dest=resolve(db,destination)
        known_origin=resolve(db,origin) if lat is None else []
    dest_place=None if known_dest else resolve_place(destination)
    origin_place=None if lat is not None or known_origin else resolve_place(origin)
    if not known_dest and not dest_place:
        return {'options':[],'reason':'We could not find this location. Try another spelling or a nearby landmark.', 'status':'place_not_found'}
    if lat is None and not known_origin and not origin_place:
        return {'options':[],'reason':'We could not find the starting location. Try another spelling or a nearby landmark.', 'status':'place_not_found'}
    result=journeys(origin,destination,lat,lon,origin_place,dest_place)
    result['origin_place']=origin_place or ({'display_name':'Current location','latitude':lat,'longitude':lon} if lat is not None else None)
    result['destination_place']=dest_place
    result['status']='route_found' if result['options'] else ('no_nearby_stops' if 'transport information nearby' in result['reason'] else 'no_connected_route')
    return result

@app.get('/api/stops/nearby')
def nearby_stops(lat: float, lon: float, radius: int = 2000):
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(422, 'Invalid coordinates.')
    if radius not in (500,1000,2000,3000): raise HTTPException(422,'Unsupported radius.')
    return nearby(lat, lon, radius=radius)

@app.get('/api/journeys')
def find_journeys(destination: str, origin: str = '', lat: float | None = None, lon: float | None = None):
    if not 2 <= len(destination.strip()) <= 100 or len(origin) > 100:
        raise HTTPException(422, 'Enter a valid destination and starting point.')
    if (lat is None) != (lon is None):
        raise HTTPException(422, 'Provide both latitude and longitude.')
    if lat is not None and not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(422, 'Invalid coordinates.')
    if lat is None and len(origin.strip()) < 2:
        raise HTTPException(422, 'Enter a starting point or use your current location.')
    return plan(origin, destination, lat, lon)

@app.get('/api/me/recent')
def recent(user=Depends(current_user)):
    with database() as db:
        return [dict(r) for r in db.execute('SELECT origin,destination,created_at FROM searches WHERE user_id=? ORDER BY id DESC LIMIT 6',(user['id'],))]

class SearchInput(BaseModel):
    origin: str = Field(min_length=2,max_length=100)
    destination: str = Field(min_length=2,max_length=100)

class Coordinates(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)

class JourneyQuery(Coordinates):
    destination: str = Field(min_length=2, max_length=100)

@app.post('/api/stops/nearby')
def nearby_stops_private(data: Coordinates):
    return nearby(data.lat,data.lon)

@app.post('/api/journeys')
def journey_private(data: JourneyQuery):
    return plan('',data.destination,data.lat,data.lon)

@app.post('/api/me/recent',status_code=201)
def save_search(data:SearchInput,user=Depends(current_user)):
    with database() as db:
        db.execute('INSERT INTO searches(user_id,origin,destination) VALUES(?,?,?)',(user['id'],data.origin.strip(),data.destination.strip()))
    return {'ok':True}

@app.get('/api/me/saved')
def saved(user=Depends(current_user)):
    with database() as db:
        rows=db.execute('SELECT routes.* FROM saved_routes JOIN routes ON routes.id=saved_routes.route_id WHERE saved_routes.user_id=? AND routes.active=1 ORDER BY routes.origin',(user['id'],)).fetchall()
    return [route_dict(r) for r in rows]

@app.post('/api/me/saved/{route_id}',status_code=201)
def save_route(route_id:int,user=Depends(current_user)):
    with database() as db:
        if not db.execute('SELECT 1 FROM routes WHERE id=? AND active=1',(route_id,)).fetchone():
            raise HTTPException(404,'Route not found.')
        db.execute('INSERT OR IGNORE INTO saved_routes VALUES(?,?)',(user['id'],route_id))
    return {'ok':True}

@app.delete('/api/me/saved/{route_id}')
def unsave(route_id:int,user=Depends(current_user)):
    with database() as db:
        db.execute('DELETE FROM saved_routes WHERE user_id=? AND route_id=?',(user['id'],route_id))
    return {'ok':True}

class RouteDiscoveryInput(BaseModel):
    starting_stop: str = Field(min_length=2,max_length=120)
    destination_stop: str = Field(min_length=2,max_length=120)
    transport_type: str = Field(min_length=2,max_length=40)
    transfer_details: str = Field(default='',max_length=500)
    approximate_fare: int | None = Field(default=None,gt=0,le=1000000)
    instructions: str = Field(min_length=10,max_length=1200)

@app.post('/api/reports/routes',status_code=201)
def report_route_discovery(data:RouteDiscoveryInput,user=Depends(current_user)):
    with database() as db:
        cur=db.execute('''INSERT INTO route_discoveries(user_id,starting_stop,destination_stop,transport_type,transfer_details,approximate_fare,instructions)
            VALUES(?,?,?,?,?,?,?)''',(user['id'],*data.model_dump().values()))
    return {'id':cur.lastrowid,'status':'pending'}

@app.get('/api/me/route-discoveries')
def my_route_discoveries(user=Depends(current_user)):
    with database() as db:
        return [dict(r) for r in db.execute('SELECT * FROM route_discoveries WHERE user_id=? ORDER BY id DESC LIMIT 50',(user['id'],))]


@app.post('/api/reports',status_code=201)
def create_report(data:ReportInput,user=Depends(current_user)):
    if data.type not in REPORT_TYPES:
        raise HTTPException(422,'Choose a valid report type.')
    with database() as db:
        if data.route_id and not db.execute('SELECT 1 FROM routes WHERE id=? AND active=1',(data.route_id,)).fetchone():
            raise HTTPException(404,'Route not found.')
        cur=db.execute('INSERT INTO reports(user_id,route_id,type,description,location) VALUES(?,?,?,?,?)',(user['id'],data.route_id,data.type,data.description.strip(),data.location.strip()))
    return {'id':cur.lastrowid,'status':'pending'}

@app.get('/api/me/reports')
def my_reports(user=Depends(current_user)):
    with database() as db:
        return [dict(r) for r in db.execute('SELECT id,route_id,type,description,location,status,created_at,reviewed_at FROM reports WHERE user_id=? ORDER BY id DESC',(user['id'],))]

@app.post('/api/fare-reports', status_code=201)
def report_fare(data: FareReportInput, user=Depends(current_user)):
    if data.transport_type not in MODES:
        raise HTTPException(422, 'Choose a supported mode.')
    with database() as db:
        route = db.execute('SELECT transport_type FROM routes WHERE id=? AND active=1', (data.route_id,)).fetchone()
        if not route:
            raise HTTPException(404, 'Route not found.')
        if data.transport_type != route['transport_type']:
            raise HTTPException(422, 'Transport mode does not match the route.')
        cur = db.execute('INSERT INTO fare_reports(user_id,route_id,amount,transport_type,paid_on,note) VALUES(?,?,?,?,?,?)',
            (user['id'],data.route_id,data.amount,data.transport_type,data.paid_on,data.note.strip()))
    return {'id':cur.lastrowid,'status':'pending'}

@app.get('/api/me/fare-reports')
def my_fare_reports(user=Depends(current_user)):
    with database() as db:
        return [dict(r) for r in db.execute('SELECT id,route_id,amount,transport_type,paid_on,note,status,created_at FROM fare_reports WHERE user_id=? ORDER BY id DESC', (user['id'],))]

def audit(db, user, action, kind, target):
    db.execute('INSERT INTO audit_logs(admin_id,action,target_type,target_id) VALUES(?,?,?,?)',(user['id'],action,kind,target))

def validate_route_data(data: RouteInput):
    if data.estimated_max_fare is not None and data.estimated_max_fare < data.estimated_fare:
        raise HTTPException(422, 'Maximum fare must be at least minimum fare.')
    if data.source == 'verified' and not data.source_url.startswith('https://'):
        raise HTTPException(422, 'A published route requires an HTTPS source.')
    if data.verified and data.source != 'verified':
        raise HTTPException(422, 'Verified routes must use a published source.')
    if len(data.stop_ids) == 1 or len(set(data.stop_ids)) != len(data.stop_ids):
        raise HTTPException(422, 'Use at least two different stops in journey order.')

def set_route_stops(db, route_id, ids):
    if not ids:
        return
    rows=db.execute('SELECT id FROM stops WHERE id IN ('+','.join('?' for _ in ids)+') AND status=?', (*ids,'Active')).fetchall()
    if len(rows)!=len(ids):
        raise HTTPException(422, 'All stops must exist and be active.')
    db.execute('DELETE FROM route_stops WHERE route_id=?', (route_id,))
    db.executemany('INSERT INTO route_stops(route_id,stop_id,position) VALUES(?,?,?)', [(route_id,sid,n) for n,sid in enumerate(ids,1)])

@app.get('/api/admin/stats')
def stats(user=Depends(admin)):
    with database() as db:
        return {name:db.execute(f'SELECT COUNT(*) FROM {table} {condition}').fetchone()[0] for name,table,condition in [('users','users',''),('routes','routes','WHERE active=1'),('stops','stops',''),('pending_reports','reports',"WHERE status='pending'"),('pending_fares','fare_reports',"WHERE status='pending'")]}

@app.get('/api/admin/routes/stale')
def stale_routes(user=Depends(admin)):
    with database() as db:
        return [dict(r) for r in db.execute("SELECT id,name,origin,destination,last_verified_at,source_url FROM routes WHERE active=1 AND source!='sample' AND (verified=0 OR last_verified_at IS NULL OR last_verified_at<date('now','-180 days')) ORDER BY last_verified_at LIMIT 100")]

@app.get('/api/admin/transport/stops')
def transport_stops(q: str = '', source: str = '', verified: bool | None = None, user=Depends(admin)):
    with database() as db:
        return [dict(r) for r in db.execute('''SELECT * FROM stops WHERE name LIKE ? AND (?='' OR source=?) AND (? IS NULL OR verified=?) ORDER BY id DESC LIMIT 100''',
            ('%'+q[:100].replace('%','\\%').replace('_','\\_')+'%',source,source,verified,verified))]

@app.get('/api/admin/transport/routes')
def transport_routes(q: str = '', source: str = '', verified: bool | None = None, user=Depends(admin)):
    with database() as db:
        return [dict(r) for r in db.execute('''SELECT * FROM routes WHERE (name LIKE ? OR origin LIKE ?) AND (?='' OR source=?) AND (? IS NULL OR verified=?) ORDER BY id DESC LIMIT 100''',
            ('%'+q[:100]+'%','%'+q[:100]+'%',source,source,verified,verified))]

class TransportDecision(BaseModel):
    action: str = Field(pattern='^(verify|deactivate)$')
    source_url: str = Field(default='', max_length=500)

class MergeStops(BaseModel):
    keep_id: int
    duplicate_id: int

@app.post('/api/admin/transport/stops/merge')
def merge_stops(data: MergeStops,user=Depends(admin)):
    if data.keep_id==data.duplicate_id: raise HTTPException(422,'Select two different stops.')
    with database() as db:
        rows=db.execute('SELECT id FROM stops WHERE id IN (?,?)',(data.keep_id,data.duplicate_id)).fetchall()
        if len(rows)!=2: raise HTTPException(404,'One of the stops was not found.')
        overlap=db.execute('''SELECT 1 FROM route_stops a JOIN route_stops b ON a.route_id=b.route_id
                              WHERE a.stop_id=? AND b.stop_id=? LIMIT 1''',(data.keep_id,data.duplicate_id)).fetchone()
        if overlap: raise HTTPException(409,'Both stops are on the same route. Review its stop order manually before merging.')
        db.execute('UPDATE route_stops SET stop_id=? WHERE stop_id=?',(data.keep_id,data.duplicate_id))
        db.execute('UPDATE stops SET status="Inactive",updated_at=CURRENT_TIMESTAMP WHERE id=?',(data.duplicate_id,))
        audit(db,user,'merge_into_'+str(data.keep_id),'stop',data.duplicate_id)
    return {'ok':True}

@app.patch('/api/admin/transport/stops/{stop_id}')
def decide_stop(stop_id: int, data: TransportDecision, user=Depends(admin)):
    if data.action=='verify' and not data.source_url.startswith('https://'):
        raise HTTPException(422,'Verification requires a trusted HTTPS source.')
    with database() as db:
        cur=db.execute('UPDATE stops SET verified=CASE WHEN ?="verify" THEN 1 ELSE verified END,status=CASE WHEN ?="deactivate" THEN "Inactive" ELSE status END,source_url=CASE WHEN ?="verify" THEN ? ELSE source_url END,verification_method=CASE WHEN ?="verify" THEN "admin review" ELSE verification_method END,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                       (data.action,data.action,data.action,data.source_url,data.action,stop_id))
        if not cur.rowcount: raise HTTPException(404,'Stop not found.')
        audit(db,user,data.action,'stop',stop_id)
    return {'ok':True}

@app.patch('/api/admin/transport/routes/{route_id}')
def decide_transport_route(route_id: int, data: TransportDecision, user=Depends(admin)):
    if data.action=='verify' and not data.source_url.startswith('https://'):
        raise HTTPException(422,'Verification requires a trusted HTTPS source.')
    with database() as db:
        cur=db.execute('UPDATE routes SET verified=CASE WHEN ?="verify" THEN 1 ELSE verified END,active=CASE WHEN ?="deactivate" THEN 0 ELSE active END,source_url=CASE WHEN ?="verify" THEN ? ELSE source_url END,verification_method=CASE WHEN ?="verify" THEN "admin review" ELSE verification_method END,last_verified_at=CASE WHEN ?="verify" THEN CURRENT_DATE ELSE last_verified_at END WHERE id=? AND source!="sample"',
                       (data.action,data.action,data.action,data.source_url,data.action,data.action,route_id))
        if not cur.rowcount: raise HTTPException(404,'Route not found.')
        audit(db,user,data.action,'route',route_id)
    return {'ok':True}

@app.post('/api/admin/transport/import',status_code=201)
def import_approved_routes(data: list[RouteInput],user=Depends(admin)):
    if not 1<=len(data)<=100: raise HTTPException(422,'Submit between 1 and 100 routes.')
    for route in data:
        validate_route_data(route)
        if route.source!='verified' or not route.verified or len(route.stop_ids)<2:
            raise HTTPException(422,'Import only source-backed verified routes with ordered stop IDs.')
    ids=[]
    with database() as db:
        for route in data:
            values=route.model_dump(exclude={'stop_ids'})
            values['duration_known']=int(route.estimated_duration is not None)
            values['estimated_duration']=route.estimated_duration or 1
            values['last_verified_at']=datetime.now(timezone.utc).date().isoformat()
            values['verification_method']='admin dataset review'
            cur=db.execute(f"INSERT INTO routes({','.join(values)}) VALUES({','.join('?' for _ in values)})",tuple(values.values()))
            set_route_stops(db,cur.lastrowid,route.stop_ids)
            audit(db,user,'import','route',cur.lastrowid)
            ids.append(cur.lastrowid)
    return {'ids':ids}

@app.get('/api/admin/reports')
def admin_reports(user=Depends(admin)):
    with database() as db:
        return [dict(r) for r in db.execute('SELECT reports.*,users.name AS reporter FROM reports JOIN users ON users.id=reports.user_id ORDER BY CASE reports.status WHEN \'pending\' THEN 0 ELSE 1 END, reports.id DESC LIMIT 100')]

@app.get('/api/admin/route-discoveries')
def admin_route_discoveries(user=Depends(admin)):
    with database() as db:
        return [dict(r) for r in db.execute('SELECT route_discoveries.*,users.name AS reporter FROM route_discoveries JOIN users ON users.id=route_discoveries.user_id ORDER BY CASE route_discoveries.status WHEN "pending" THEN 0 ELSE 1 END,route_discoveries.id DESC LIMIT 100')]

@app.patch('/api/admin/route-discoveries/{report_id}')
def review_route_discovery(report_id:int,data:Decision,user=Depends(admin)):
    with database() as db:
        cur=db.execute('UPDATE route_discoveries SET status=?,reviewed_at=CURRENT_TIMESTAMP WHERE id=? AND status="pending"',(data.status,report_id))
        if not cur.rowcount: raise HTTPException(404,'Pending suggestion not found.')
        audit(db,user,data.status,'route_discovery',report_id)
    return {'ok':True,'note':'Review does not create or verify a transport connection.'}

@app.get('/api/admin/fare-reports')
def admin_fare_reports(user=Depends(admin)):
    with database() as db:
        return [dict(r) for r in db.execute('SELECT fare_reports.*,users.name AS reporter,routes.origin,routes.destination FROM fare_reports JOIN users ON users.id=fare_reports.user_id JOIN routes ON routes.id=fare_reports.route_id ORDER BY CASE fare_reports.status WHEN \'pending\' THEN 0 ELSE 1 END,fare_reports.id DESC LIMIT 100')]

@app.patch('/api/admin/fare-reports/{report_id}')
def review_fare(report_id: int, data: Decision, user=Depends(admin)):
    with database() as db:
        cur=db.execute("UPDATE fare_reports SET status=?,reviewed_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",(data.status,report_id))
        if not cur.rowcount:
            raise HTTPException(404, 'Pending fare report not found.')
        audit(db,user,data.status,'fare_report',report_id)
    return {'ok':True}

@app.patch('/api/admin/reports/{report_id}')
def review(report_id:int,data:Decision,user=Depends(admin)):
    with database() as db:
        cur=db.execute("UPDATE reports SET status=?,reviewed_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",(data.status,report_id))
        if not cur.rowcount:
            raise HTTPException(404,'Pending report not found.')
        audit(db,user,data.status,'report',report_id)
    return {'ok':True}

@app.post('/api/admin/routes',status_code=201)
def add_route(data:RouteInput,user=Depends(admin)):
    validate_route_data(data)
    values=data.model_dump(exclude={'stop_ids'})
    values['duration_known']=int(data.estimated_duration is not None)
    values['estimated_duration']=data.estimated_duration or 1
    values['last_verified_at']=datetime.now(timezone.utc).date().isoformat() if data.verified else None
    fields=list(values)
    with database() as db:
        cur=db.execute(f"INSERT INTO routes({','.join(fields)},last_updated) VALUES({','.join('?' for _ in fields)},CURRENT_DATE)",list(values.values()))
        set_route_stops(db,cur.lastrowid,data.stop_ids)
        audit(db,user,'create','route',cur.lastrowid)
    return {'id':cur.lastrowid}

@app.put('/api/admin/routes/{route_id}')
def edit_route(route_id:int,data:RouteInput,user=Depends(admin)):
    validate_route_data(data)
    values=data.model_dump(exclude={'stop_ids'})
    values['duration_known']=int(data.estimated_duration is not None)
    values['estimated_duration']=data.estimated_duration or 1
    values['last_verified_at']=datetime.now(timezone.utc).date().isoformat() if data.verified else None
    with database() as db:
        cur=db.execute('UPDATE routes SET '+','.join(f'{field}=?' for field in values)+',last_updated=CURRENT_DATE WHERE id=? AND active=1',(*values.values(),route_id))
        if not cur.rowcount: raise HTTPException(404,'Route not found.')
        set_route_stops(db,route_id,data.stop_ids)
        audit(db,user,'update','route',route_id)
    return {'ok':True}

@app.delete('/api/admin/routes/{route_id}')
def delete_route(route_id:int,user=Depends(admin)):
    with database() as db:
        cur=db.execute('UPDATE routes SET active=0 WHERE id=? AND active=1',(route_id,))
        if not cur.rowcount: raise HTTPException(404,'Route not found.')
        audit(db,user,'archive','route',route_id)
    return {'ok':True}

@app.put('/api/admin/routes/{route_id}/stops')
def connect_route_stops(route_id: int, data: OrderedStops, user=Depends(admin)):
    if len(set(data.stop_ids)) != len(data.stop_ids):
        raise HTTPException(422, 'Stop IDs must be unique.')
    with database() as db:
        if not db.execute('SELECT 1 FROM routes WHERE id=? AND active=1',(route_id,)).fetchone():
            raise HTTPException(404, 'Route not found.')
        set_route_stops(db,route_id,data.stop_ids)
        audit(db,user,'connect_stops','route',route_id)
    return {'ok':True}

@app.post('/api/admin/stops',status_code=201)
def add_stop(data:StopInput,user=Depends(admin)):
    values=data.model_dump()
    values['source']='admin'
    if data.verified and not data.source_url.startswith('https://'):
        raise HTTPException(422, 'Verified stops require a trusted HTTPS source.')
    values['created_at']=values['updated_at']=datetime.now(timezone.utc).isoformat()
    with database() as db:
        try: cur=db.execute(f"INSERT INTO stops({','.join(values)}) VALUES({','.join('?' for _ in values)})",tuple(values.values()))
        except sqlite3.IntegrityError: raise HTTPException(409,'Stop name already exists.')
        audit(db,user,'create','stop',cur.lastrowid)
    return {'id':cur.lastrowid}

@app.put('/api/admin/stops/{stop_id}')
def edit_stop(stop_id:int,data:StopInput,user=Depends(admin)):
    values=data.model_dump()
    if data.verified and not data.source_url.startswith('https://'):
        raise HTTPException(422, 'Verified stops require a trusted HTTPS source.')
    values['updated_at']=datetime.now(timezone.utc).isoformat()
    with database() as db:
        try: cur=db.execute('UPDATE stops SET '+','.join(f'{field}=?' for field in values)+' WHERE id=?',(*values.values(),stop_id))
        except sqlite3.IntegrityError: raise HTTPException(409,'Stop name already exists.')
        if not cur.rowcount: raise HTTPException(404,'Stop not found.')
        audit(db,user,'update','stop',stop_id)
    return {'ok':True}

@app.get('/api/admin/users')
def users(user=Depends(admin)):
    with database() as db:
        return [dict(r) for r in db.execute('SELECT id,name,email,role,created_at FROM users ORDER BY id DESC LIMIT 100')]

@app.patch('/api/admin/users/{user_id}')
def update_user(user_id:int,data:RoleInput,user=Depends(admin)):
    if user_id==user['id'] and data.role!='admin': raise HTTPException(400,'You cannot remove your own admin role.')
    with database() as db:
        cur=db.execute('UPDATE users SET role=? WHERE id=?',(data.role,user_id))
        if not cur.rowcount: raise HTTPException(404,'User not found.')
        audit(db,user,'set_role_'+data.role,'user',user_id)
    return {'ok':True}
