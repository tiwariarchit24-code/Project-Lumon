# Architecture

Project Lumon is deliberately simple: **one browser app, one API process
with an embedded worker, one database, one folder of files.**

```
Browser (React + MapLibre)
   │  only /api on this machine
   ▼
Lumon API (FastAPI, server/lumon/main.py)
   │  + job worker thread (server/lumon/worker.py)
   ▼
SQLite database  data/lumon.db          ← PostGIS/pgvector later
Local files      data/boundaries, data/reference, data/snapshots, data/imagery
   ▲
   │  CONNECTED mode only, through server/lumon/net.py
Public sources (USGS, NASA, GDACS, Earth Search STAC, …)
```

## Rules the structure enforces

- **The browser never calls third-party services.** It talks only to
  `/api` on this machine. Map style, label fonts and the logo are local
  files.
- **One network gateway.** All outbound requests go through
  `server/lumon/net.py`: `fetch_bytes()` for HTTP and `require_connected()`
  before GDAL reads remote imagery. In air-gapped mode both refuse.
- **Business logic lives in the backend.** Normalisation, validation,
  scoping, change detection, provenance and the query engine are Python.
  The frontend presents and filters.
- **Configuration is data.** The India boundary datasets, the operating
  area, the source registry, the layer registry and the AOIs are JSON files
  in `config/`, not constants in code.

## Logical subsystems

| Subsystem | Where |
|---|---|
| UI / analyst console | `src/pages/CommandPage.tsx`, `src/components/` |
| Map / globe engine | `src/components/MapWorkspace.tsx`, `src/utils/mapStyle.ts` |
| Layer registry | `config/layers.json`, `/api/layers` (`routes/geo.py`) |
| OSINT source registry | `config/sources/registry.json`, `sources/registry.py` |
| OSINT ingestion | `ingest.py`, `sources/*.py` |
| Entity and event store | `events` and `entities` tables (`db.py`) |
| India boundary and gazetteer | `geo/boundary.py`, `geo/places.py`, `staging/boundaries.py` |
| Satellite archive ingestion | `imagery/archive.py` |
| Satellite preprocessing | `imagery/sentinel2.py`, `imagery/features.py`, `imagery/observations.py` |
| Embedding / vector retrieval | `imagery/similarity.py` (feature vectors; no neural model staged) |
| Temporal change engine | `change/engine.py` |
| False-alarm verification and confidence | `change/engine.py` (six gates, uncalibrated score) |
| Analyst review | `review.py`, `routes/review.py`, `src/components/intel/ChangeDetail.tsx` |
| Provenance | `provenance.py`, `provenance` table |
| Audit | `audit.py` (hash chain), `audit_log` table |
| Query planner | `query/parser.py`, `query/engine.py`, `src/components/QueryPlan.tsx` |
| Jobs | `worker.py`, `jobs` table |
| Export | `export.py` (GeoPackage) |
| Offline verification | `offline_check.py` |
| Evaluation | `evaluation.py` |

## The two processing directions

**Ingest path** (`ingest.refresh_source`, `imagery.archive.ingest_aoi`):
source → snapshot (raw, checksummed) → normalise → validate → scope to
India → store → provenance → source health → audit.

**Query path** (`query.parser.parse` → `query.engine.run`):
question → plan (editable) → capability check → area/time/type filters →
spatial relation → ranking → results with reasons → evidence → review.
Normal queries never reprocess imagery; they read stored observations and
candidates.

## Database

SQLite now (no server needed), with tables shaped for PostGIS later:
geometries are GeoJSON text plus bounding-box columns, and JSON columns
hold source-specific details.

`sources`, `source_runs`, `events`, `entities`, `provenance`, `audit_log`,
`jobs`, `scenes`, `tiles`, `tile_observations`, `change_candidates`,
`decisions`. Change events are change candidates with
`status = 'accepted'`; suppressed ones keep their gate results.

Moving to PostgreSQL + PostGIS + pgvector means changing `db.py` and the
SQL in a few query functions (bbox filters become `ST_Intersects`, the
feature matrix becomes a `vector` column). The API and UI do not change.

## Operating modes

`LUMON_MODE` in `.env.local`:

- `connected`: staging and refresh commands may download.
- `airgapped` (default): no external requests; refreshes re-read the last
  snapshot. See [offline-mode.md](offline-mode.md).

## Frontend structure

`CommandPage` owns shared state (selection, layers, time window, plan) and
passes it down by props. No global state library is used. Panels float
over the map: navigation rail and drawer on the left, intelligence panel
on the right, layer stack, timeline at the bottom, status bar. Motion uses
CSS variables (`--motion-fast/normal/slow`) and switches off under
`prefers-reduced-motion`.
