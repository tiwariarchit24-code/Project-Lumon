# Offline (air-gapped) mode

Lumon has two modes, set with `LUMON_MODE` in `.env.local`:

| Mode | Purpose | Network |
|---|---|---|
| `connected` | Stage boundaries, refresh sources, ingest imagery | Outbound HTTPS through `server/lumon/net.py` only |
| `airgapped` (default) | Analysis | **None.** Every external request is refused |

## What guarantees "no hidden requests"

- **Backend.** Only `net.fetch_bytes()` makes HTTP requests, and it
  raises `OfflineError` in air-gapped mode. Remote imagery reads with
  GDAL/rasterio are preceded by `net.require_connected()`. No other module
  opens connections.
- **Frontend.** The browser only calls `/api` on the same machine. The map
  style, the grid, label fonts (`public/fonts/`) and the logo are local;
  there are no CDNs, remote fonts, tile servers or API keys.
- **Freshness** is shown per source and layer: `LIVE` (downloaded within
  the source's `stale_after_hours`, connected mode), `SNAPSHOT · age`
  (recent staged copy, e.g. air-gapped), `STALE · age` (older than its
  limit; stale aircraft/satellite positions are also faded on the map),
  `UNAVAILABLE`, `NOT STAGED`, `KEY REQUIRED`, `NOT IMPLEMENTED`. Re-reading
  a snapshot offline never makes data look newer. The status bar's MODE
  (`INGEST ENABLED` / `AIR-GAPPED`) is what the backend is allowed to do,
  not a network test; DATA shows the actual freshness.

## Staging checklist (connected, once)

```bash
npm install
.venv/bin/pip install -r server/requirements.txt
npm run stage:all          # boundaries, sources, demo AOI imagery, change engine
npm run build              # UI bundle, served by the API for single-process use
```

Copy the whole project folder, including `data/`, `.venv/` and
`node_modules/` (or only `dist/` if you serve the built UI from the API),
to the air-gapped machine.

## Running air-gapped

```bash
npm run api:airgapped      # http://127.0.0.1:8000 serves the API and the built UI
```

## Verifying it (Stage 11)

```bash
npm run lumon -- verify-offline
```

This forces air-gapped mode, makes every non-local socket connection fail,
and runs the stack against the real staged data: API endpoints, every
source refresh (each must fall back to its snapshot), a natural-language
query, satellite evidence chips, the change engine, similarity search,
GeoPackage export and audit verification. The checks write (refreshes,
change engine, export), so they run on a temporary copy of the database
and writable folders (`data/offline-check/`, deleted afterwards); the
real source health is never changed. Result on the development
machine (2026-10-04): **13 passed, 0 failed, 0 blocked connection
attempts.**

To confirm the browser side, open the app with the network disabled (or
in the browser's DevTools Network panel, filter by "third-party"). The
only requests are to the local origin.

## What does not work offline

- Refreshing sources with new data, and imagery ingest (they need the
  network by definition; offline they re-read snapshots or refuse
  clearly).
- Any source that was never staged.
