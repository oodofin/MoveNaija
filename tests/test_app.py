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
    demonstration=client.get('/api/routes',params={'origin':'Ojuelegba','destination':'Mushin'}).json()
    assert len(demonstration)==1 and demonstration[0]['source']=='sample'
    assert demonstration[0]['estimated_fare']==0 and demonstration[0]['estimated_duration'] is None
    assert demonstration[0]['stops']==[]  # This demonstration cannot be used as a graph edge.

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
    rail_option=next(o for o in direct['options'] if o['segments'][0]['mode']=='Rail')
    segment=rail_option['segments'][0]
    assert segment['mode']=='Rail'
    assert segment['via']==['Marina Station','National Theatre Station','Iganmu Station','Alaba Station','Mile 2 Station']
    assert rail_option['fare_min'] is None
    assert rail_option['transfers']==0
    partial=client.get('/api/journeys',params={'origin':'National Theatre','destination':'Iganmu'}).json()['options'][0]
    assert partial['duration_min'] is None  # End-to-end estimates are not reused for short rides.
    red=client.get('/api/journeys',params={'origin':'Yaba','destination':'Ikeja'}).json()
    assert red['options']  # LAMATA now publishes the Red Line station sequence.
    assert red['options'][0]['segments'][0]['mode']=='Rail'
    assert red['options'][0]['segments'][0]['via']==[
        'Yaba Red Line Station','Mushin Red Line Station','Oshodi Red Line Station','Ikeja Red Line Station']
    assert red['options'][0]['fare_min'] is None  # No rail fare was established.
    assert all(s['source']!='sample' for option in red['options'] for s in option['segments'])
    assert client.get('/api/locations/suggest',params={'q':'Iganmu'}).json()
    assert client.get('/api/stops/nearby',params={'lat':6.52,'lon':3.38}).json()==[]  # No guessed coordinates.


def test_mapped_lagos_places_resolve_without_inventing_services(client,monkeypatch):
    from scripts.import_lagos_places import import_elements
    from app.locations import resolve_place
    import app.locations as locations
    import app.db as db
    monkeypatch.setattr(locations.urllib.request, 'urlopen', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('Should use local place import')))
    source={'elements':[
        {'type':'node','id':123,'lat':6.61,'lon':3.5,'tags':{'name':'Mapped Quarter','place':'neighbourhood'}},
        {'type':'way','id':456,'center':{'lat':6.62,'lon':3.51},'tags':{'name':'Mapped Hospital','amenity':'hospital','addr:suburb':'Mapped Quarter'}},
        {'type':'node','id':789,'lat':6.62,'lon':3.51,'tags':{'name':'Outside Lagos','place':'suburb','addr:state':'Ogun'}},
    ]}
    assert import_elements(source)==2
    assert import_elements(source)==2
    with db.database() as conn:
        assert conn.execute("SELECT COUNT(*) FROM place_cache WHERE query_normalized LIKE 'osm:%'").fetchone()[0]==2
    assert resolve_place('Mapped Quarter')['latitude']==6.61
    assert client.get('/api/locations/suggest',params={'q':'Mapped Hos'}).json()[0]['name'].startswith('Mapped Hospital')
    assert client.get('/api/journeys',params={'origin':'Mapped Quarter','destination':'Mapped Hospital'}).json()['options']==[]


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
    base={'origin':'Test A','destination':'Test B','transport_type':'Bus','estimated_fare':300,'estimated_max_fare':500,'estimated_duration':10,'transfers':0,'instructions':'Board the bus.|Get down at the next stop.','status':'Available','source':'verified','verified':True,'source_url':'https://example.org/test-service','city':'Lagos'}
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
    assert client.post('/api/admin/routes',headers=headers,json={**base,'source':'community','stop_ids':ids[:2]}).status_code==422

def test_unverified_admin_route_does_not_become_travel_advice(client):
    csrf=register(client,'draft-admin@example.com')
    import app.db as db
    with db.database() as conn:
        conn.execute("UPDATE users SET role='admin' WHERE email='draft-admin@example.com'")
    headers={'X-CSRF-Token':csrf}
    ids=[]
    for name in ('Draft A','Draft B'):
        response=client.post('/api/admin/stops',headers=headers,json={'name':name,'location':'Lagos'})
        ids.append(response.json()['id'])
    response=client.post('/api/admin/routes',headers=headers,json={'origin':'Draft A','destination':'Draft B',
        'transport_type':'Bus','estimated_fare':0,'estimated_duration':None,'transfers':0,
        'instructions':'Unreviewed submission only.','source':'community','stop_ids':ids})
    assert response.status_code==201,response.text
    result=client.get('/api/journeys',params={'origin':'Draft A','destination':'Draft B'}).json()
    assert result['options']==[]

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
    assert result['incoming_services']==[{'label':'Ikeja-Ikotun','mode':'Standard','from_area':'Ikeja',
        'to_area':'Ikotun','listed_fares':[630],'effective_date':'2026-03-01',
        'source_url':'https://www.lamata-ng.com/bus-fare/'}]
    nearby=client.post('/api/journeys',json={'lat':6.5,'lon':3.4,'destination':'Ikotun'}).json()
    assert nearby['options']==[] and nearby['incoming_services'][0]['from_area']=='Ikeja'
    assert nearby['incoming_services'][0]['listed_fares']==[630]
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
        assert result['status'] in {'no_nearby_stops','no_connected_route','published_corridors'}
        assert result['options']==[]
        assert all(segment['source_url'].startswith('https://www.lamata-ng.com/')
                   for corridor in result.get('published_corridors',[])
                   for segment in corridor['segments'])
    for destination in ['Ikotun','University of Lagos','Ikeja City Mall']:
        result=client.post('/api/journeys',json={'lat':6.5,'lon':3.4,'destination':destination}).json()
        assert result['status'] in {'no_nearby_stops','no_connected_route'} and not result['options']

def test_published_fare_is_a_reference_not_a_journey(client,monkeypatch):
    import app.fares as fares
    import app.main as main
    monkeypatch.setattr(main,'resolve_place',lambda q: {'display_name':q,'latitude':6.5,'longitude':3.4} if q=='Ajah' else None)
    assert len(fares.published_data()['rows'])==78
    direct=client.get('/api/fares/published',params={'origin':'Oshodi','destination':'Ajah'}).json()
    assert direct[0]['published_amounts']==[1320] and direct[0]['direction']=='listed'
    reverse=client.get('/api/fares/published',params={'origin':'Yaba','destination':'Ikotun'}).json()
    assert reverse[0]['published_amounts']==[940] and reverse[0]['direction']=='reverse_listing'
    assert client.get('/api/fares/published',params={'origin':'Yaba','destination':'Lekki Phase 1'}).json()==[]
    assert client.get('/api/journeys',params={'origin':'Oshodi','destination':'Ajah'}).json()['options']==[]
    # LAMATA's published list has two conflicting Mile 2–TBS BRT amounts.
    conflict=client.get('/api/fares/published',params={'origin':'Mile 2','destination':'TBS'}).json()
    assert conflict[0]['published_amounts']==[680,510]

def test_statewide_published_corridors_do_not_invent_missing_links(client,monkeypatch):
    import app.main as main
    monkeypatch.setattr(main,'resolve_place',lambda q: None)
    direct=client.get('/api/journeys',params={'origin':'Oshodi','destination':'Ajah'}).json()
    assert direct['options']==[]
    assert direct['published_corridors'][0]['segments'][0]['label']=='Oshodi-Ajah'
    assert direct['published_corridors'][0]['published_fare_sum']==1320
    change=client.get('/api/journeys',params={'origin':'Ikotun','destination':'Ajah'}).json()
    assert change['options']==[] and change['status']=='published_corridors'
    via_cms=next(c for c in change['published_corridors'] if c['transfers']==1)
    assert [s['label'] for s in via_cms['segments']]==['Ikotun-Marina/CMS','Marina/CMS - Ajah']
    assert via_cms['published_fare_sum']==1950
    for origin,destination in [('Ikorodu','Epe'),('Badagry','Ikeja'),('Epe','Lekki'),('Ojo','Victoria Island')]:
        result=client.get('/api/journeys',params={'origin':origin,'destination':destination}).json()
        assert not result.get('published_corridors') and not result['options']

def test_operator_published_ferry_routes_and_repeatable_seed(client):
    from app.network import init_network
    import app.db as db
    first=client.get('/api/journeys',params={'origin':'Ikorodu','destination':'Falomo'}).json()['options']
    assert first and first[0]['segments'][0]['mode']=='Ferry'
    assert first[0]['segments'][0]['via']==['Ipakodo Ferry Terminal','Five Cowries Ferry Terminal']
    assert first[0]['fare_min']==2200 and first[0]['duration_min']==50
    reverse=client.get('/api/journeys',params={'origin':'Falomo','destination':'Ikorodu'}).json()['options']
    assert reverse and reverse[0]['fare_min']==2200
    marina=client.get('/api/journeys',params={'origin':'Ikorodu','destination':'Marina'}).json()['options']
    assert any(option['segments'][0]['via']==['Ipakodo Ferry Terminal','Ebute Ero Jetty','Marina Ferry Terminal']
               and option['fare_min'] is None and option['duration_min'] is None for option in marina)
    return_ferry=client.get('/api/journeys',params={'origin':'Marina','destination':'Ikorodu'}).json()['options']
    assert any(option['segments'][0]['via']==['Marina Ferry Terminal','Ebute Ero Jetty','Ipakodo Ferry Terminal']
               for option in return_ferry)
    assert client.get('/api/journeys',params={'origin':'Badore','destination':'Ijede'}).json()['options'][0]['fare_min']==2500
    with db.database() as conn:
        route_count=conn.execute("SELECT COUNT(*) FROM routes WHERE external_id LIKE 'lagferry:%' OR external_id LIKE 'lamata:red-line:%'").fetchone()[0]
        stop_count=conn.execute("SELECT COUNT(*) FROM stops WHERE source='verified' AND stop_type IN ('rail','ferry')").fetchone()[0]
    init_network()
    with db.database() as conn:
        assert conn.execute("SELECT COUNT(*) FROM routes WHERE external_id LIKE 'lagferry:%' OR external_id LIKE 'lamata:red-line:%'").fetchone()[0]==route_count
        assert conn.execute("SELECT COUNT(*) FROM stops WHERE source='verified' AND stop_type IN ('rail','ferry')").fetchone()[0]==stop_count

@pytest.mark.parametrize(('phrase','origin','destination'),[
    ('Ikeja to Obalende','Ikeja','Obalende'),
    ('How do I get from Oshodi to Ajah?','Oshodi','Ajah'),
    ("I'm at Yaba going to Computer Village",'Yaba','Computer Village'),
    ('How I fit reach CMS from Ikeja?','Ikeja','CMS'),
    ('From Badagry to Victoria Island','Badagry','Victoria Island'),
])
def test_journey_phrase_requires_confirmation_before_route_search(client,phrase,origin,destination):
    result=client.post('/api/journeys/interpret',json={'query':phrase})
    assert result.status_code==200 and result.json()=={'origin':origin,'destination':destination}

def test_journey_phrase_rejects_uncertain_input(client):
    assert client.post('/api/journeys/interpret',json={'query':'Ikeja'}).status_code==422
    assert client.post('/api/journeys/interpret',json={'query':'Ikeja to Ikeja'}).status_code==422
    assert client.post('/api/journeys/interpret',json={'query':'How far is it?'}).status_code==422

def test_short_location_alias_uses_geocoder_not_guessed_coordinates(client,monkeypatch):
    import app.locations as locations
    urls=[]
    class Reply:
        def __enter__(self):return self
        def __exit__(self,*args):pass
    def fake_open(request,timeout):
        urls.append(request.full_url)
        return Reply()
    monkeypatch.setattr(locations.urllib.request,'urlopen',fake_open)
    monkeypatch.setattr(locations.json,'load',lambda _: [{'lat':'6.43','lon':'3.41',
        'display_name':'Victoria Island, Lagos, Nigeria','type':'suburb',
        'osm_type':'relation','osm_id':987,'address':{'country_code':'ng','state':'Lagos'}}])
    result=client.get('/api/locations/search',params={'q':'VI'})
    assert result.status_code==200 and result.json()['display_name'].startswith('Victoria Island')
    assert 'Victoria+Island' in urls[0]

def test_verified_conductor_note_and_anonymous_moderated_feedback(client):
    import app.db as db
    csrf=register(client,'conductor-admin@example.com')
    with db.database() as conn:
        conn.execute("UPDATE users SET role='admin' WHERE email='conductor-admin@example.com'")
    headers={'X-CSRF-Token':csrf}
    ids=[]
    for name in ['Source A','Source B']:
        response=client.post('/api/admin/stops',headers=headers,json={'name':name,'location':'Lagos','verified':True,'source_url':'https://example.org/checked-stop'})
        assert response.status_code==201,response.text
        ids.append(response.json()['id'])
    route=client.post('/api/admin/routes',headers=headers,json={
        'origin':'Source A','destination':'Source B','transport_type':'Bus','estimated_fare':300,
        'estimated_duration':12,'transfers':0,'instructions':'Board at Source A.|Get down at Source B.',
        'source':'verified','verified':True,'source_url':'https://example.org/route',
        'stop_ids':ids,'conductor_call':'Source B','boarding_instruction':'Use the marked bay.',
        'dropoff_instruction':'Exit at the final stop.'})
    assert route.status_code==201,route.text
    rid=route.json()['id']
    segment=client.get('/api/journeys',params={'origin':'Source A','destination':'Source B'}).json()['options'][0]['segments'][0]
    assert segment['conductor_call']=='Source B' and segment['boarding_instruction']=='Use the marked bay.'
    assert client.post('/api/reports/journey-feedback',json={'route_id':rid,'helpful':False}).status_code==422
    assert client.post('/api/reports/journey-feedback',json={'route_id':1,'helpful':True}).status_code==404  # Sample is not a reviewed journey.
    assert client.post('/api/reports/journey-feedback',json={'route_id':rid,'helpful':True,'route_sequence':[1]}).status_code==422
    feedback=client.post('/api/reports/journey-feedback',json={'route_id':rid,'helpful':False,'reason':'wrong_boarding','note':'Bay was closed.','route_sequence':[rid]})
    assert feedback.status_code==201 and feedback.json()['status']=='pending'
    assert client.get('/api/admin/journey-feedback').json()[0]['reason']=='wrong_boarding'
    assert client.get('/api/admin/journey-feedback').json()[0]['route_sequence']==str(rid)
    assert client.patch('/api/admin/journey-feedback/'+str(feedback.json()['id']),headers=headers).status_code==200
    assert client.get('/api/admin/journey-feedback').json()[0]['status']=='reviewed'
    assert client.get('/api/journeys',params={'origin':'Source A','destination':'Source B'}).json()['options'][0]['segments'][0]['conductor_call']=='Source B'
