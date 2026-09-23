import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(os.getenv('MOVENAIJA_DB', Path(__file__).resolve().parent.parent / 'movenaija.db'))

@contextmanager
def database():
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with database() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('user','admin')), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, csrf_token TEXT NOT NULL, expires_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS stops (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, location TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', landmarks TEXT NOT NULL DEFAULT '', transport_types TEXT NOT NULL DEFAULT 'Bus', status TEXT NOT NULL DEFAULT 'Active', city TEXT NOT NULL DEFAULT 'Lagos', latitude REAL, longitude REAL);
        CREATE TABLE IF NOT EXISTS routes (id INTEGER PRIMARY KEY, origin TEXT NOT NULL, destination TEXT NOT NULL, transport_type TEXT NOT NULL, estimated_fare INTEGER NOT NULL CHECK(estimated_fare>=0), estimated_duration INTEGER NOT NULL CHECK(estimated_duration>0), transfers INTEGER NOT NULL DEFAULT 0 CHECK(transfers>=0), instructions TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Sample', source TEXT NOT NULL DEFAULT 'sample' CHECK(source IN ('sample','community','verified')), last_updated TEXT NOT NULL DEFAULT CURRENT_DATE, city TEXT NOT NULL DEFAULT 'Lagos', active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS route_stops (route_id INTEGER NOT NULL REFERENCES routes(id) ON DELETE CASCADE, stop_id INTEGER NOT NULL REFERENCES stops(id) ON DELETE CASCADE, position INTEGER NOT NULL, PRIMARY KEY(route_id,stop_id));
        CREATE TABLE IF NOT EXISTS reports (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, route_id INTEGER REFERENCES routes(id) ON DELETE SET NULL, type TEXT NOT NULL, description TEXT NOT NULL, location TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, reviewed_at TEXT);
        CREATE TABLE IF NOT EXISTS saved_routes (user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, route_id INTEGER NOT NULL REFERENCES routes(id) ON DELETE CASCADE, PRIMARY KEY(user_id,route_id));
        CREATE TABLE IF NOT EXISTS searches (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, origin TEXT NOT NULL, destination TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS audit_logs (id INTEGER PRIMARY KEY, admin_id INTEGER NOT NULL REFERENCES users(id), action TEXT NOT NULL, target_type TEXT NOT NULL, target_id INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX IF NOT EXISTS idx_routes_search ON routes(origin,destination,active);
        CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status);
        ''')
        if db.execute('SELECT COUNT(*) FROM stops').fetchone()[0]:
            return
        stops = [
            ('Yaba Bus Stop','Yaba, Lagos','Main road bus stop near Yaba market','Yaba Market','Bus,Danfo'),
            ('Ikeja Along','Ikeja, Lagos','Bus stop on the Ikeja corridor','Computer Village','Bus,Danfo'),
            ('Ojota Bus Stop','Ojota, Lagos','Major interchange on Ikorodu Road','Ojota interchange','Bus,Danfo'),
            ('CMS Bus Stop','Lagos Island, Lagos','Island interchange near Marina','Marina','Bus,Danfo'),
            ('Oshodi Terminal','Oshodi, Lagos','Major bus terminal','Oshodi interchange','Bus,Danfo'),
            ('Lekki Phase 1 Gate','Lekki, Lagos','Bus stop at the Lekki Phase 1 entrance','Lekki Phase 1 gate','Bus,Danfo'),
            ('Maryland Bus Stop','Maryland, Lagos','Bus stop along Ikorodu Road','Maryland Mall','Bus,Danfo'),
        ]
        db.executemany('INSERT INTO stops(name,location,description,landmarks,transport_types) VALUES(?,?,?,?,?)', stops)
        routes = [
            ('Yaba','Ikeja','Danfo',900,55,1,'Walk to Yaba Bus Stop.|Take a bus towards Ojota and get down at Ojota Bus Stop.|Change to a bus heading towards Ikeja.|Get down at Ikeja Along and walk to your destination.'),
            ('Yaba','Ikeja','Bus',1100,50,1,'Walk to Yaba Bus Stop.|Board a bus heading towards Oshodi.|Get down at Oshodi Terminal and change to a bus towards Ikeja.|Get down at Ikeja Along and walk to your destination.'),
            ('Yaba','CMS','Danfo',700,40,0,'Walk to Yaba Bus Stop.|Ask for a bus going to CMS before boarding.|Get down at CMS Bus Stop.|Walk to your destination.'),
            ('Oshodi','Ikeja','Danfo',500,30,0,'Go to Oshodi Terminal.|Board a bus heading towards Ikeja.|Get down at Ikeja Along.|Walk to your destination.'),
            ('Ojota','Yaba','Danfo',650,35,0,'Go to Ojota Bus Stop.|Ask for a bus going to Yaba before boarding.|Get down at Yaba Bus Stop.|Walk to your destination.'),
            ('CMS','Lekki Phase 1','Bus',1200,65,0,'Go to CMS Bus Stop.|Ask for a bus going towards Lekki Phase 1.|Get down at Lekki Phase 1 Gate.|Walk to your destination.'),
        ]
        db.executemany("INSERT INTO routes(origin,destination,transport_type,estimated_fare,estimated_duration,transfers,instructions) VALUES(?,?,?,?,?,?,?)", routes)
        pairs = [(1,1,1),(1,3,2),(1,2,3),(2,1,1),(2,5,2),(2,2,3),(3,1,1),(3,4,2),(4,5,1),(4,2,2),(5,3,1),(5,1,2),(6,4,1),(6,6,2)]
        db.executemany('INSERT INTO route_stops(route_id,stop_id,position) VALUES(?,?,?)', pairs)
