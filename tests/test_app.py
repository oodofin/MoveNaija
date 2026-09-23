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
    payload={'origin':'New Yaba','destination':'New Ikeja','transport_type':'Bus','estimated_fare':500,'estimated_duration':35,'transfers':0,'instructions':'Walk to stop.|Board the bus.|Get down at destination.','status':'Available','source':'verified','city':'Lagos'}
    result=client.post('/api/admin/routes',json=payload,headers=headers)
    assert result.status_code==201,result.text
    route_id=result.json()['id']
    assert client.get('/api/routes',params={'origin':'New Yaba'}).json()[0]['source']=='verified'
    assert client.delete(f'/api/admin/routes/{route_id}',headers=headers).status_code==200
    assert client.get(f'/api/routes/{route_id}').status_code==404
    with db.database() as conn:
        assert conn.execute('SELECT COUNT(*) FROM audit_logs').fetchone()[0]==2
