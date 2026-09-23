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

The Ojuelegba → Mushin demo entry is also marked **sample**. It intentionally has no fare, duration, exact boarding stop or graph links. When an exact search has no documented journey, the app may show its sample card below the missing-data message to demonstrate the intended result format. It is not a confirmed bus service. For real commuter guidance, add reviewed stop sequences and source-backed fares through the administrator tools. Geocoding can locate Mushin but cannot tell MoveNaija which bus serves it.

## Create the first administrator

Register a normal account in the UI, stop the server, then promote that exact email with:

```bash
python -m app.promote_admin admin@example.com
```

Run this only in a trusted shell with access to the application database; never expose it as a public endpoint. Restart the server and log in. Admins can manage routes, stops, report decisions and user roles. Deleting a route archives it so old reports remain traceable. Admin changes have audit entries in SQLite.

## Configuration and deployment

Set `MOVENAIJA_DB` to a writable absolute SQLite path if needed. Run behind HTTPS in production so session cookies are marked Secure. Keep a single app worker for the simple in-memory rate limiter. Use a writable persistent disk; ephemeral filesystems erase accounts and edits. SQLite is suited to this small MVP; migrate the SQL and storage layer to PostgreSQL before scaling to multiple workers. Place search and map tiles use external OpenStreetMap services when enabled and reachable. Back up your database.

Run tests with `pytest`. The app uses parameterized SQLite queries, scrypt password hashes, HttpOnly SameSite session cookies, CSRF header validation and role checks. The first admin bootstrap is intentionally command-line-only.

## Network planner (second iteration)

The new `/api/journeys` endpoint plans paths through ordered stops (`route_stops`) rather than matching just an origin/destination pair. It supports up to three transfers, never traverses the original illustrative `sample` journeys, and does not infer a service merely because two stops exist. Search suggestions match stop names, area names, landmarks and comma-separated alternatives. A result with missing fare data says the fare is unavailable. Partial rides do not inherit an end-to-end fare or time estimate.

The five Blue Line stations and both travel directions are sourced from [LAMATA's Blue Line schedule](https://www.lamata-ng.com/blue-line-train-schedule/). The approximately 18-minute end-to-end time comes from [LAMATA's published schedule table](https://www.lamata-ng.com/our-schedule/). No fare or station coordinates have been seeded for this service, as neither is substantiated by those sources. The seven original demonstration stops and six sample journeys remain separate, unverified sample data. Broad Lagos coverage requires verified imports and field checks; a missing route is shown as missing, not fabricated.

### Admin data entry

An admin can edit a stop to add source-backed GPS coordinates and alternative names, then add a route with a comma-separated ordered list of stop IDs (displayed in the admin stop list). A route needs at least two distinct connected stops before it enters the planner. Add a trusted HTTPS source URL and tick Verified only after checking it. To model both directions, enter two directed route records. The app currently draws straight lines between mapped stops; these **are not walking paths or track geometry**. Approximate walking distance to the first stop is straight-line distance. Precise user coordinates stay in the current browser tab and are sent in request bodies to the nearby/journey endpoints only when a user explicitly requests location access. They are not saved to the database.

OpenStreetMap map tiles and Leaflet are loaded from public services when online; textual directions continue to work if map resources cannot load. Add attribution-compatible local assets or a suitable tile provider for production scale. An approved community report does not itself rewrite a verified route or fare. An administrator must make a separate audited data update after checking a report.

## Place resolution and transport coverage

The submitted journey search first checks existing stop names. Other place names are resolved by a Nominatim-compatible geocoder (`app/locations.py`) to coordinates and stored in `place_cache` keyed by normalized query. Repeated exact queries use the SQLite cache. `/api/locations/search?q=...` resolves on demand; `/api/locations/suggest` uses **only local stops and previously cached places**. The public Nominatim server expressly prohibits client autocomplete. The browser waits 350 ms and at least three typed characters before requesting local suggestions. The service limits external geocoding calls to about one per second **per process**, uses a recognizable User-Agent, prefers the Lagos region and rejects results outside an approximate Lagos bounding box. If no place is found or the service is unavailable, the search reports a missing location. Production deployments should use a dedicated geocoder with its own limits and availability guarantees; set `MOVENAIJA_GEOCODER_URL` to its HTTPS Nominatim-compatible search endpoint and `MOVENAIJA_USER_AGENT` to your app name and contact URL. Set the latter even in development if you deploy a public instance. Do not use public Nominatim for bulk prepopulation or autocomplete. See [Nominatim usage policy](https://operations.osmfoundation.org/policies/nominatim/).

### Route-first results and published fare references

The results screen leads with transport options, fares and step-by-step instructions. The map is optional and collapsed by default. Original sample route cards appear as examples when browsing, but do not appear as real search results or enter the network graph.

`GET /api/fares/published?origin=Oshodi&destination=Ajah` searches a bundled snapshot of [LAMATA's published Bus Reform Initiative fare list](https://www.lamata-ng.com/bus-fare/), labelled effective 1 March 2026. The snapshot has 78 entries, including two conflicting Mile 2–TBS BRT amounts. Those are shown as conflicting instead of selecting one. It matches explicitly named endpoint pairs; reverse matches are identified as reverse listings, not confirmed return services. Published endpoint fares do not supply boarding stops, intervening stops, transfers, schedules or exact walking directions. They are **fare references**, never automatically connected journey edges. A published corridor fare must not be presented as the total price for a different multimode itinerary. Refresh the source snapshot after checking a newer published list, and disclose its date to commuters. Informal danfo and keke fares remain unknown until reviewed reports support them.

With a resolved place, nearby mapped stops are selected by a bounding-box SQL query and Haversine distance, within 2 km by default. `/api/stops/nearby` also accepts `radius=500`, `1000`, `2000` or `3000` metres. The planner evaluates multiple nearby boarding and arrival stops, adds approximate straight-line walking distances and finds up to three changes along **ordered route-stop links only**. Longer walks are excluded by the 2 km access limit. Short-distance walking transfers between separate stops are not yet modelled. An imported stop alone never creates a service. Unknown fare or duration remains unknown; map lines joining station coordinates are schematic and are not pedestrian, road or rail geometry. A resolved place with no stops and a place with stops but no reviewed connections have different messages. A network outage and a truly absent place currently share the same generic place-not-found message.

### Import discovered OpenStreetMap stops

The importer is an offline command and never runs on server startup. Supply an Overpass JSON export containing named transport objects:

```bash
python -m scripts.import_lagos_transport /path/to/lagos.json
```

For a deliberate one-off bounded Overpass query, set `MOVENAIJA_USER_AGENT` with your contact and use `python -m scripts.import_lagos_transport --fetch`. The query uses approximate Lagos bounds and is **not a legal Lagos State polygon**; review its output before use. Respect the [Overpass service limits](https://wiki.openstreetmap.org/wiki/Overpass_API) and use regional extracts or a hosted provider at scale. No bulk import was performed as part of this change. The importer upserts by OSM object type and ID, refreshes coordinates and sync timestamps, and leaves records that disappear from a source in place for review. Imported stops are unverified and have no route links. Because the legacy database has unique stop names, new OSM records display their source ID as a suffix; an admin can merge duplicates after review. Existing accounts, sample data and routes remain in place. Schema additions occur at startup and preserve existing tables.

### Review and structured route datasets

An admin can use Profile → Transport data to filter records, inspect source and sync time, verify with an HTTPS source, deactivate or merge stops, and paste an array of reviewed routes. `/api/admin/transport/import` validates the entire JSON array (maximum 100 records) and inserts it in one database transaction. Each route needs `origin`, `destination`, `transport_type`, `estimated_fare` (use 0 for unknown), `estimated_duration` (null if unknown), `transfers`, `instructions`, `status`, `source: "verified"`, `verified: true`, `source_url` (HTTPS), and `stop_ids` in journey order. A trusted reviewer must check the source, station order and both directions; no official API is assumed. CSV must be converted to this JSON format before upload. Reviewing a community route suggestion leaves it outside the routing graph until an admin separately creates a source-backed route. Original sample data never enters the network planner.

To add another city, extend the geocoder's region settings and spatial bounds and import local geographic data into stops with the corresponding `city`, `state` and `country`. The planner still filters network data to Lagos and requires a city parameterization before another city's journeys can be served. Public map attribution and source restrictions also apply to new cities.
