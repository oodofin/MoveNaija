# MoveNaija

A beginner-friendly Lagos public transport route finder built with FastAPI, SQLite and plain HTML/CSS/JavaScript.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000. The SQLite database and sample routes are created automatically on first start. API docs are at `/docs`.

## Demonstration data

The six seeded routes and their fares, journey times, stops and instructions are **illustrative samples**, not validated travel advice or live data. Search Yaba → Ikeja or browse Routes. Do not deploy them as verified commuter guidance. Newly submitted community reports stay pending and never automatically alter route data. Admins can enter separately researched routes and set the appropriate source label.

## Create the first administrator

Register a normal account in the UI, stop the server, then promote that exact email with:

```bash
python -m app.promote_admin admin@example.com
```

Run this only in a trusted shell with access to the application database; never expose it as a public endpoint. Restart the server and log in. Admins can manage routes, stops, report decisions and user roles. Deleting a route archives it so old reports remain traceable. Admin changes have audit entries in SQLite.

## Configuration and deployment

Set `MOVENAIJA_DB` to a writable absolute SQLite path if needed. Run behind HTTPS in production so session cookies are marked Secure. Keep a single app worker for the simple in-memory rate limiter. Use a writable persistent disk; ephemeral filesystems erase accounts and edits. SQLite is suited to this small MVP; migrate the SQL and storage layer to PostgreSQL before scaling to multiple workers. No external map or real-time provider is needed for this release. Back up your database.

Run tests with `pytest`. The app uses parameterized SQLite queries, scrypt password hashes, HttpOnly SameSite session cookies, CSRF header validation and role checks. The first admin bootstrap is intentionally command-line-only.
