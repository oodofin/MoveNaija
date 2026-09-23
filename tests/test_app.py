import pytest
from fastapi.testclient import TestClient

@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.db as db
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'test.db')
    from app.main import app, _hits
    _hits.clear()
    with TestClient(app) as client:
        yield client

def register(client, email='user@example.com'):
    result=client.post('/api/auth/register',json={'name':'Lagos Rider','email':email,'password':'a secure password 123'})
    assert result.status_code == 201, result.text
    return result.json()['csrf_token']

def test_search_and_sample_labels(client):
    rows=client.get('/api/routes',params={'origin':'Yaba','destination':'Ikeja'}).json()
    assert len(rows)==2
    assert all(r['source']=='sample' and r['stops'] and len(r['instructions'])>=3 for r in rows)
    assert client.get('/api/routes',params={'origin':'%','destination':'Ikeja'}).json()==[]
    assert client.get('/api/routes/999').status_code==404
    assert client.get('/').status_code==200

def test_auth_saved_reports_and_csrf(client):
    csrf=register(client)
    assert client.get('/api/auth/me').json()['user']['role']=='user'
    assert client.post('/api/me/saved/1').status_code==403
    headers={'X-CSRF-Token':csrf}
    assert client.post('/api/me/saved/1',headers=headers).status_code==201
    assert len(client.get('/api/me/saved').json())==1
    report={'type':'fare_change','location':'Yaba','description':'The fare appears different from the sample estimate.','route_id':1}
    assert client.post('/api/reports',headers=headers,json=report).json()['status']=='pending'
    assert client.get('/api/me/reports').json()[0]['status']=='pending'
    assert client.get('/api/admin/stats').status_code==403
    assert client.delete('/api/me/saved/1',headers=headers).status_code==200
    assert client.post('/api/auth/logout',headers=headers).status_code==200
    assert client.get('/api/auth/me').status_code==401
    assert client.post('/api/auth/login',json={'email':'user@example.com','password':'wrong password'}).status_code==401

def test_admin_review_audit_and_escaping(client):
    csrf=register(client,'admin@example.com')
    import app.db as db
    with db.database() as conn:
        conn.execute("UPDATE users SET role='admin' WHERE email='admin@example.com'")
    assert client.get('/api/admin/stats').json()['users']==1
    headers={'X-CSRF-Token':csrf}
    payload={'origin':'New Yaba','destination':'New Ikeja','transport_type':'Bus','estimated_fare':500,'estimated_duration':35,'transfers':0,'instructions':'Walk to stop.|Board the bus.|Get down at destination.','status':'Available','source':'verified','source_url':'https://example.org/transport-record','city':'Lagos'}
    result=client.post('/api/admin/routes',json=payload,headers=headers)
    assert result.status_code==201,result.text
    route_id=result.json()['id']
    assert client.get('/api/routes',params={'origin':'New Yaba'}).json()[0]['source']=='verified'
    assert client.delete(f'/api/admin/routes/{route_id}',headers=headers).status_code==200
    assert client.get(f'/api/routes/{route_id}').status_code==404
    with db.database() as conn:
        assert conn.execute('SELECT COUNT(*) FROM audit_logs').fetchone()[0]==2

def test_network_uses_only_connected_stops_and_lamata_sequence(client):
    direct=client.get('/api/journeys',params={'origin':'Marina','destination':'Mile 2'}).json()
    assert direct['options']
    segment=direct['options'][0]['segments'][0]
    assert segment['mode']=='Rail'
    assert segment['via']==['Marina Station','National Theatre Station','Iganmu Station','Alaba Station','Mile 2 Station']
    assert direct['options'][0]['fare_min'] is None
    assert direct['options'][0]['transfers']==0
    partial=client.get('/api/journeys',params={'origin':'National Theatre','destination':'Iganmu'}).json()['options'][0]
    assert partial['duration_min'] is None  # End-to-end estimates are not reused for short rides.
    unknown=client.get('/api/journeys',params={'origin':'Yaba','destination':'Ikeja'}).json()
    assert unknown['options']==[]  # Old demonstration cards must not create transport edges.
    assert client.get('/api/locations/suggest',params={'q':'Iganmu'}).json()
    assert client.get('/api/stops/nearby',params={'lat':6.52,'lon':3.38}).json()==[]  # No guessed coordinates.


def test_admin_graph_transfers_coordinates_and_fare_moderation(client):
    import app.db as db
    csrf=register(client,'admin2@example.com')
    with db.database() as conn:
        conn.execute("UPDATE users SET role='admin' WHERE email='admin2@example.com'")
    headers={'X-CSRF-Token':csrf}
    ids=[]
    for index,name in enumerate(['Test A','Test B','Test C','Test D']):
        response=client.post('/api/admin/stops',headers=headers,json={'name':name,'location':'Lagos','latitude':6.5+index*.001,'longitude':3.4,'stop_type':'bus'})
        assert response.status_code==201,response.text
        ids.append(response.json()['id'])
    assert client.get('/api/stops/nearby',params={'lat':6.5,'lon':3.4}).json()[0]['name']=='Test A'
    assert client.post('/api/stops/nearby',json={'lat':6.5,'lon':3.4}).json()[0]['name']=='Test A'
    base={'origin':'Test A','destination':'Test B','transport_type':'Bus','estimated_fare':300,'estimated_max_fare':500,'estimated_duration':10,'transfers':0,'instructions':'Board the bus.|Get down at the next stop.','status':'Available','source':'community','city':'Lagos'}
    first=client.post('/api/admin/routes',headers=headers,json={**base,'stop_ids':ids[:2]})
    assert first.status_code==201,first.text
    second=client.post('/api/admin/routes',headers=headers,json={**base,'origin':'Test B','destination':'Test D','stop_ids':ids[1:]})
    assert second.status_code==201,second.text
    journey=client.get('/api/journeys',params={'origin':'Test A','destination':'Test D'}).json()['options'][0]
    geo=client.post('/api/journeys',json={'lat':6.5,'lon':3.4,'destination':'Test D'}).json()
    assert geo['options'][0]['origin'] in {'Test A','Test B'}
    assert geo['options'][0]['walk_to_board_m'] is not None
    assert journey['transfers']==1
    assert journey['fare_min']==600 and journey['fare_max']==1000
    assert journey['segments'][1]['via']==['Test B','Test C','Test D']
    report=client.post('/api/fare-reports',headers=headers,json={'route_id':first.json()['id'],'amount':450,'transport_type':'Bus','paid_on':'2026-09-23'})
    assert report.status_code==201,report.text
    assert client.patch('/api/admin/fare-reports/'+str(report.json()['id']),headers=headers,json={'status':'approved'}).status_code==200
    assert client.get('/api/routes/'+str(first.json()['id'])).json()['estimated_fare']==300
    assert client.post('/api/admin/routes',headers=headers,json={**base,'verified':True,'source':'verified','stop_ids':ids[:2]}).status_code==422

def test_place_resolution_cache_and_missing_connections(client,monkeypatch):
    import app.locations as locations
    calls=[]
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self): return b''
    def fake_open(request,timeout):
        calls.append(request.full_url)
        return Response()
    monkeypatch.setattr(locations.urllib.request,'urlopen',fake_open)
    monkeypatch.setattr(locations.json,'load',lambda _: [{'lat':'6.55','lon':'3.30','display_name':'Ikotun, Lagos, Nigeria','type':'suburb','osm_type':'relation','osm_id':123,'address':{'country_code':'ng','state':'Lagos'}}])
    first=client.get('/api/locations/search',params={'q':'Ikotun'})
    assert first.status_code==200 and first.json()['latitude']==6.55
    assert client.get('/api/locations/search',params={'q':' ikotun '}).status_code==200
    assert len(calls)==1
    result=client.get('/api/journeys',params={'origin':'Yaba','destination':'Ikotun'}).json()
    assert result['status']=='no_nearby_stops' and result['destination_place']['display_name'].startswith('Ikotun')
    assert result['options']==[]
    assert client.get('/api/locations/suggest',params={'q':'Iko'}).json()

def test_osm_import_is_idempotent_and_not_a_route(client):
    from scripts.import_lagos_transport import import_elements
    import app.db as db
    payload={'elements':[{'type':'node','id':71,'lat':6.5,'lon':3.4,'tags':{'highway':'bus_stop','name':'Reported test stop'}}]}
    assert import_elements(payload)==1
    assert import_elements(payload)==1
    with db.database() as conn:
        assert conn.execute("SELECT COUNT(*) FROM stops WHERE external_id='node:71'").fetchone()[0]==1
        stop=conn.execute("SELECT * FROM stops WHERE external_id='node:71'").fetchone()
        assert stop['verified']==0 and stop['source']=='openstreetmap'
        assert conn.execute('SELECT COUNT(*) FROM route_stops WHERE stop_id=?',(stop['id'],)).fetchone()[0]==0

def test_route_discovery_stays_out_of_graph_after_review(client):
    csrf=register(client,'reviewer@example.com')
    import app.db as db
    with db.database() as conn:
        conn.execute("UPDATE users SET role='admin' WHERE email='reviewer@example.com'")
    headers={'X-CSRF-Token':csrf}
    row=client.post('/api/reports/routes',headers=headers,json={'starting_stop':'One stop','destination_stop':'Another stop','transport_type':'Bus','instructions':'Change at a known junction.'})
    assert row.status_code==201 and row.json()['status']=='pending'
    assert client.patch('/api/admin/route-discoveries/'+str(row.json()['id']),headers=headers,json={'status':'approved'}).status_code==200
    with db.database() as conn:
        assert conn.execute("SELECT COUNT(*) FROM routes WHERE origin='One stop'").fetchone()[0]==0

def test_requested_searches_do_not_invent_connections(client,monkeypatch):
    import app.main as main
    known={'Ikotun','Lekki Phase 1','Ajah','Computer Village','Egbeda','Ikorodu','University of Lagos','Ikeja City Mall','Surulere','Oshodi','Ikeja','CMS','Yaba'}
    monkeypatch.setattr(main,'resolve_place',lambda q: {'display_name':q+', Lagos','latitude':6.5,'longitude':3.4,'source':'openstreetmap'} if q in known else None)
    searches=[('Yaba','Ikotun'),('Ikeja','Lekki Phase 1'),('Oshodi','Ajah'),('Surulere','Computer Village'),('Egbeda','CMS'),('Ikorodu','Yaba')]
    for origin,destination in searches:
        result=client.get('/api/journeys',params={'origin':origin,'destination':destination}).json()
        assert result['status'] in {'no_nearby_stops','no_connected_route'}
        assert result['options']==[]
    for destination in ['Ikotun','University of Lagos','Ikeja City Mall']:
        result=client.post('/api/journeys',json={'lat':6.5,'lon':3.4,'destination':destination}).json()
        assert result['status'] in {'no_nearby_stops','no_connected_route'} and not result['options']
