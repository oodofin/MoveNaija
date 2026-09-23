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

## Network planner (second iteration)

The new `/api/journeys` endpoint plans paths through ordered stops (`route_stops`) rather than matching just an origin/destination pair. It supports up to three transfers, never traverses the original illustrative `sample` journeys, and does not infer a service merely because two stops exist. Search suggestions match stop names, area names, landmarks and comma-separated alternatives. A result with missing fare data says the fare is unavailable. Partial rides do not inherit an end-to-end fare or time estimate.

The five Blue Line stations and both travel directions are sourced from [LAMATA's Blue Line schedule](https://www.lamata-ng.com/blue-line-train-schedule/). The approximately 18-minute end-to-end time comes from [LAMATA's published schedule table](https://www.lamata-ng.com/our-schedule/). No fare or station coordinates have been seeded for this service, as neither is substantiated by those sources. The seven original demonstration stops and six sample journeys remain separate, unverified sample data. Broad Lagos coverage requires verified imports and field checks; a missing route is shown as missing, not fabricated.

### Admin data entry

An admin can edit a stop to add source-backed GPS coordinates and alternative names, then add a route with a comma-separated ordered list of stop IDs (displayed in the admin stop list). A route needs at least two distinct connected stops before it enters the planner. Add a trusted HTTPS source URL and tick Verified only after checking it. To model both directions, enter two directed route records. The app currently draws straight lines between mapped stops; these **are not walking paths or track geometry**. Approximate walking distance to the first stop is straight-line distance. Precise user coordinates stay in the current browser tab and are sent in request bodies to the nearby/journey endpoints only when a user explicitly requests location access. They are not saved to the database.

OpenStreetMap map tiles and Leaflet are loaded from public services when online; textual directions continue to work if map resources cannot load. Add attribution-compatible local assets or a suitable tile provider for production scale. An approved community report does not itself rewrite a verified route or fare. An administrator must make a separate audited data update after checking a report.
