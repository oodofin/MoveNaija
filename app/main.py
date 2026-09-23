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

ROOT = Path(__file__).resolve().parent
COOKIE = 'movenaija_session'
REPORT_TYPES = {'fare_change','incorrect_route','stop_change','road_closure','heavy_traffic','disruption','incorrect_information'}
_hits = defaultdict(deque)

@asynccontextmanager
async def lifespan(app):
    init_db()
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
    response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'"
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
    estimated_duration: int = Field(ge=1, le=1440)
    transfers: int = Field(ge=0, le=10)
    instructions: str = Field(min_length=10, max_length=3000)
    status: str = Field(default='Available', max_length=60)
    source: str = Field(default='verified', pattern='^(sample|community|verified)$')
    city: str = Field(default='Lagos', min_length=2, max_length=80)
    @field_validator('instructions')
    @classmethod
    def steps(cls, value):
        if not all(p.strip() for p in value.split('|')):
            raise ValueError('Separate nonempty steps with |')
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

class RoleInput(BaseModel):
    role: str = Field(pattern='^(user|admin)$')

class Decision(BaseModel):
    status: str = Field(pattern='^(approved|rejected)$')

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

@app.get('/api/me/recent')
def recent(user=Depends(current_user)):
    with database() as db:
        return [dict(r) for r in db.execute('SELECT origin,destination,created_at FROM searches WHERE user_id=? ORDER BY id DESC LIMIT 6',(user['id'],))]

class SearchInput(BaseModel):
    origin: str = Field(min_length=2,max_length=100)
    destination: str = Field(min_length=2,max_length=100)

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

def audit(db, user, action, kind, target):
    db.execute('INSERT INTO audit_logs(admin_id,action,target_type,target_id) VALUES(?,?,?,?)',(user['id'],action,kind,target))

@app.get('/api/admin/stats')
def stats(user=Depends(admin)):
    with database() as db:
        return {name:db.execute(f'SELECT COUNT(*) FROM {table} {condition}').fetchone()[0] for name,table,condition in [('users','users',''),('routes','routes','WHERE active=1'),('stops','stops',''),('pending_reports','reports',"WHERE status='pending'")]}

@app.get('/api/admin/reports')
def admin_reports(user=Depends(admin)):
    with database() as db:
        return [dict(r) for r in db.execute('SELECT reports.*,users.name AS reporter FROM reports JOIN users ON users.id=reports.user_id ORDER BY CASE reports.status WHEN \'pending\' THEN 0 ELSE 1 END, reports.id DESC LIMIT 100')]

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
    fields=list(data.model_dump())
    with database() as db:
        cur=db.execute(f"INSERT INTO routes({','.join(fields)},last_updated) VALUES({','.join('?' for _ in fields)},CURRENT_DATE)",list(data.model_dump().values()))
        audit(db,user,'create','route',cur.lastrowid)
    return {'id':cur.lastrowid}

@app.put('/api/admin/routes/{route_id}')
def edit_route(route_id:int,data:RouteInput,user=Depends(admin)):
    values=data.model_dump()
    with database() as db:
        cur=db.execute('UPDATE routes SET '+','.join(f'{field}=?' for field in values)+',last_updated=CURRENT_DATE WHERE id=? AND active=1',(*values.values(),route_id))
        if not cur.rowcount: raise HTTPException(404,'Route not found.')
        audit(db,user,'update','route',route_id)
    return {'ok':True}

@app.delete('/api/admin/routes/{route_id}')
def delete_route(route_id:int,user=Depends(admin)):
    with database() as db:
        cur=db.execute('UPDATE routes SET active=0 WHERE id=? AND active=1',(route_id,))
        if not cur.rowcount: raise HTTPException(404,'Route not found.')
        audit(db,user,'archive','route',route_id)
    return {'ok':True}

@app.post('/api/admin/stops',status_code=201)
def add_stop(data:StopInput,user=Depends(admin)):
    values=data.model_dump()
    with database() as db:
        try: cur=db.execute(f"INSERT INTO stops({','.join(values)}) VALUES({','.join('?' for _ in values)})",tuple(values.values()))
        except sqlite3.IntegrityError: raise HTTPException(409,'Stop name already exists.')
        audit(db,user,'create','stop',cur.lastrowid)
    return {'id':cur.lastrowid}

@app.put('/api/admin/stops/{stop_id}')
def edit_stop(stop_id:int,data:StopInput,user=Depends(admin)):
    values=data.model_dump()
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
