# Development

## Setup

```bash
npm install
python3 -m venv .venv
.venv/bin/pip install -r server/requirements.txt
cp .env.example .env.local
```

`.env.local` (ignored by Git):

```
LUMON_MODE=connected          # or airgapped
NASA_FIRMS_MAP_KEY=           # optional, free from NASA FIRMS
```

Never commit keys. `.env.example` lists the names only.

On macOS with the python.org build of Python, HTTPS needs a certificate
bundle. Lumon uses `certifi` from the project venv, so no system changes
are needed.

## Everyday commands

| Command | What it does |
|---|---|
| `npm run api` | Lumon API + worker on 127.0.0.1:8000 |
| `npm run dev` | Frontend on localhost:5173 (proxies `/api`) |
| `npm run build` | Type check + production build into `dist/` |
| `npm test` | Type check, UI unit tests, backend tests |
| `npm run lumon -- <command>` | Backend CLI (run with no command to list them) |
| `node scripts/ui-smoke.mjs` | Headless Chrome check at 1440×900 and 1280×800 |

Backend CLI commands: `stage-boundaries`, `refresh`, `refresh-all`,
`ingest-imagery`, `recheck-radiometry`, `analyse`, `evaluate`, `export`,
`verify-audit`, `verify-offline`, `write-manifest`, `worker`.

## Code style (project rules)

- Simple code a first-year developer can follow: plain functions, plain
  React components with `useState` / `useEffect` and props. No global
  state library, no dependency injection, no factories.
- Comments explain **what, why, inputs, output and assumptions**, and
  describe algorithms in plain language before the code.
- Keep files small and named after what they do.
- Never fabricate data. Use `—`, `NOT STAGED`, `UNCALIBRATED` and similar
  honest states. Test fixtures are synthetic and labelled `TEST FIXTURE`.
- No new dependency without a concrete need. The current frontend
  dependencies are `react`, `react-dom` and `maplibre-gl`.

## Tests

- `server/tests/`: geometry and India scoping, swap quarantine, source
  normalisation, time parsing, query parsing, change logic (composites,
  transitions, gates, honest windows), audit chain tampering, provenance
  ids, decisions, GeoPackage structure, satellite frame conversion. Every
  test uses a temporary data folder in air-gapped mode.
- `src/utils/utils.test.ts`: formatting, coordinates, time ranges, grid.
- `scripts/ui-smoke.mjs`: renders the app in Chrome and checks for console
  errors, horizontal scroll and the main panels.
- `npm run lumon -- verify-offline`: the backend against real staged data
  with the network blocked.

## Where things live

See [architecture.md](architecture.md). To add a source, an AOI or a
layer, see [ingestion.md](ingestion.md). Layers are defined in
`config/layers.json` and need no frontend change unless they need a new
drawing style (`src/utils/mapStyle.ts`).

## Git

The project owner manages Git. Generated data (`data/`), the venv,
`node_modules/` and `dist/` are ignored.
