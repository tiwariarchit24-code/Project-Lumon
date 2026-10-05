# Project Lumon

**LUMON COMMAND** is an India-focused OSINT + GEOINT analyst workstation.
It brings public open-source events, India-wide geography and satellite
change analysis into one map-first workspace, and it is designed to keep
working **air-gapped** once data has been staged.

The guiding rule: **never fabricate intelligence.** Every value shown is
read from a staged source, measured, or clearly marked as a placeholder
(`—`, `NOT STAGED`, `UNCALIBRATED`, `AVAILABLE LATER`).

## What works today

| Area | Status |
|---|---|
| India operating boundary | Staged from documented public datasets (land: Natural Earth *India point of view*; maritime: Marine Regions EEZ; states, districts: geoBoundaries). Configurable in `config/geo/`. |
| Map / globe | MapLibre, fully local style and fonts; flat and globe projection, zoom, compass, reset, measure, go-to coordinates, scale, coordinate readout, clustering, viewport-only loading. |
| OSINT sources | 19 registered sources: 16 running (USGS earthquakes, NASA EONET, GDACS, Open-Meteo, OpenSky aircraft, IODA outages, NOAA Kp, Launch Library, CelesTrak satellites, OurAirports, Natural Earth ports/rivers/lakes/roads/railways, WRI power plants), NASA FIRMS implemented but waiting for a free API key, AIS vessels and OpenAQ registered as planned. |
| Source health | Per-source health, last success/failure, data age, cache status, licence, quarantine counts; offline fallback to the last staged snapshot. |
| Events / entities | Normalised, validated, scoped to India, clickable, with source card, provenance, raw record and related events. |
| Query planner | Deterministic parser → editable plan chips (change, place, distance, reference, time, magnitude…) → capability check → ranked results with reasons. No language model required. |
| Satellite archive | Sentinel-2 L2A over a small demo AOI (63 usable scenes 2018–2026), radiometric harmonisation with a data check, quarantine of unusable or inconsistent scenes, incremental ingest. |
| Change engine | Yearly dry-season composites, persistent transitions, six false-alarm gates, honest *last clear before / earliest supported after* window, uncalibrated score. |
| Analyst review | Ranked queue, before/after evidence chips, confirm / reject / relabel, more-like-these, hash-chained audit trail. |
| Export | GeoPackage (changes, events, scenes, decisions, provenance). |
| Offline | `LUMON_MODE=airgapped` blocks all external requests; `verify-offline` proves the stack works with the network blocked. |
| Evaluation | Reproducible harness; reports only measured values and says "not measurable" otherwise. |

What is **not** built yet is listed in [docs/BUILD_STATUS.md](docs/BUILD_STATUS.md).

## Requirements

- Node.js (developed with 26.x) and npm
- Python 3.12+ (developed with 3.14)
- ~400 MB free disk for dependencies, plus ~250 MB for the staged demo data
- Google Chrome (optional, only for the UI smoke test)

No Docker, database server, API key or GPU is required.

## Install

```bash
npm install
python3 -m venv .venv
.venv/bin/pip install -r server/requirements.txt
cp .env.example .env.local      # then set LUMON_MODE=connected to download data
```

## Stage the data (connected mode, once)

```bash
npm run stage:all
```

This downloads the India boundaries, refreshes every OSINT source, stages
the Sentinel-2 demo AOI and runs the change engine. Individual steps:

```bash
npm run lumon -- stage-boundaries
npm run lumon -- refresh-all
npm run lumon -- ingest-imagery demo-01
npm run lumon -- analyse demo-01
```

## Run

```bash
npm run api       # terminal 1: local Lumon API on http://127.0.0.1:8000
npm run dev       # terminal 2: open http://localhost:5173
```

Air-gapped: set `LUMON_MODE=airgapped` in `.env.local` (or run
`npm run api:airgapped`). Single process for offline use: `npm run build`,
then `npm run api` also serves the built UI on http://127.0.0.1:8000.

## Build and test

```bash
npm run build                      # TypeScript check + production build
npm test                           # TypeScript + UI unit tests + backend tests
npm run lumon -- verify-offline    # whole backend with the network blocked
npm run lumon -- evaluate          # evaluation report -> data/evaluation/latest.json
node scripts/ui-smoke.mjs          # headless browser check at 1440x900 and 1280x800
```

## Project structure

```
config/            boundary datasets, operating area, sources, layers, AOIs
docs/              architecture, data sources, ingestion, offline mode, evaluation, development, build status
public/            logo, map label fonts
scripts/           UI smoke test
server/lumon/      Python backend (FastAPI)
  sources/         one adapter per OSINT source
  geo/             geometry, India boundary, gazetteer
  imagery/         Sentinel-2 archive, features, similarity, quicklooks
  change/          change engine and false-alarm gates
  query/           deterministic parser and query engine
  routes/          HTTP API
server/tests/      backend tests
src/               React + TypeScript frontend
data/              staged data, snapshots, imagery, database (NOT committed)
```

## Documentation

- [Architecture](docs/architecture.md)
- [Data sources](docs/data-sources.md) and the generated [source manifest](docs/SOURCE_MANIFEST.md)
- [Ingestion](docs/ingestion.md)
- [Change detection](docs/change-detection.md)
- [Offline mode](docs/offline-mode.md)
- [Evaluation](docs/evaluation.md)
- [Development](docs/development.md)
- [Build status](docs/BUILD_STATUS.md)

## Data and licences

Staged data is never committed (`data/` is ignored). Each dataset keeps its
own licence and attribution (see the Data & provenance panel and
[docs/data-sources.md](docs/data-sources.md)). Some sources are for
non-commercial use only (OpenSky, Open-Meteo free tier). Check the terms
before any redistribution.
