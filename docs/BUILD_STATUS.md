# Build status

State of Project Lumon after the continuous build of 2026-10-04 and the
follow-up UI audit fixes (honest freshness statuses, timeline/map
consistency, satellite playback, upcoming events, audit write
serialisation), the NIT Raipur pilot foundation, the layer-toggle
repair + live-tracking audit, and the first machine-learning capability,
semantic satellite image search with RemoteCLIP, extended to
image-to-image similarity on the same embeddings, and learned change
detection with BTC-B (model-generated candidate changes), and discovery by
embedding-based clustering (PS 26227 §2.2.4) (see those sections). Every number below was measured on the development
machine (Apple M4, 10 CPUs, 16 GB RAM, macOS, Node 26.4, Python 3.14.6);
data counts are as of the last source refresh at 2026-10-04 13:04Z.
Nothing is estimated.

## Stages

| Stage | Status | Notes |
|---|---|---|
| 0 Frontend foundation | **Complete** | Superseded by the full workstation. |
| 0.5 Polish | **Complete** | Official logo integrated (`public/assets/project-lumon-logo.png`, blended into the dark top bar without editing the image). Floating-panel layout, central motion system with reduced-motion support. |
| 1 India geographic foundation | **Complete** | Land (Natural Earth, India point of view) + EEZ (Marine Regions) operating area, states, districts and cities from documented datasets with checksums. Gazetteer search; flat and globe projection. Records are scoped to the boundary at ingest. India is highlighted by the boundary fill; areas outside India are not masked out. |
| 2 OSINT infrastructure | **Complete** | Source registry, adapters, snapshots with checksums, validation, India scoping, quarantine, provenance, source health, jobs. |
| 3 OSINT map/event system | **Complete** | Events and entities on the map (clustered, viewport and time filtered; event and infrastructure clusters styled differently), event/entity panels, source cards, related events, layer registry. Every source and layer shows its freshness: LIVE, SNAPSHOT · age, STALE · age, UNAVAILABLE, NOT STAGED, KEY REQUIRED or NOT IMPLEMENTED, from per-source `stale_after_hours` in the registry config. Stale aircraft/satellite positions are faded and labelled. |
| 4 OSINT breadth | **Partial** | 16 sources running: earthquakes, natural events, disaster alerts, weather, aircraft, internet outages, space weather, launches, satellites, airports, ports, power plants, rivers, lakes, roads, railways. NASA FIRMS is implemented but waits for a free key. AIS vessels and OpenAQ are registered but not implemented (both also need keys). No source yet for news/public events, power outages, protected areas or land cover. Scheduled launches appear in a separate UPCOMING window (next 90 days), labelled SCHEDULED. |
| 5 Satellite data foundation | **Complete (Sentinel-2, one AOI)** | 63 usable Sentinel-2 L2A scenes (2018-01 to 2026-10) for the demo AOI. Metadata validation, radiometric harmonisation checked against the data, SCL quality masks, quarantine (17 scenes), incremental ingest, checksums and provenance. Sentinel-1 and Landsat not staged. Local copies are tiled GeoTIFFs, not formally validated COGs. |
| 6 Semantic retrieval | **Partial** | **Text-to-image search, image-to-image similarity and discovery clusters work with RemoteCLIP ViT-B-32** (model-powered) on the demo AOI: 2,118 chips from 63 scenes, exact cosine search over cached embeddings in SQLite. Not validated against ground truth; see the AI/ML section and SEMANTIC_SEARCH_EVAL.md. The older tile tools ("find tiles like this", Dossiers "more like these") still use explainable spectral and change-history features (non-semantic, not ML) and are labelled so. Neither uses pgvector. |
| 7 Temporal change engine | **Complete** (rule-based) + **learned candidates** | Rule-based: yearly dry-season composites, persistent transitions, grouping, change classes, last-clear-before / earliest-supported-after windows. Separately, the BTC-B network produces MODEL-GENERATED CANDIDATE CHANGES for an analyst-chosen scene pair (see the AI/ML capability 02 section); the two never replace each other. |
| 8 False-alarm verification | **Complete (heuristic)** | Six gates with stored reasons and SHOW SUPPRESSED. Explicit BRDF/terrain illumination correction is not implemented. Confidence is an **uncalibrated** score; calibration waits for analyst labels. |
| 9 Analyst workflow | **Complete** | Ranked queue, evidence chips, gates, confirm/reject/relabel, decisions log, hash-chained audit with verification, more like these. |
| 10 OSINT + GEOINT fusion | **Partial** | Event → nearest AOI → change context; change queries attach nearby OSINT events; the timeline playhead drives the imagery shown on the map. Limited by having one AOI. |
| 11 Offline operation | **Complete (backend verified)** | `verify-offline`: 13 passed, 0 failed, 0 blocked connection attempts. It runs on a temporary copy of the database (deleted afterwards), so it no longer changes the real source health. One-process air-gapped serving (UI + API) passed the UI smoke test. The browser side is offline by construction (local origin only); it was not re-tested with the network physically disabled. |
| 12 Evaluation | **Complete (harness)** | Latency, runtime, storage and hardware are measured. Precision@K, false alarms per 100 km², earliest-date error and decision time report "not measurable" until analysts review candidates or reference dates are provided. |
| 13 Final polish | **Mostly complete** | Loading, empty, error, offline, not-staged and stale states; status bar shows MODE (what the backend may do) and DATA (actual freshness); timeline main band counts exactly the map's events, with telemetry in a separate band; satellite playback updates the image in place; search shows SUPPORTED / PARTIAL / UNSUPPORTED; keyboard shortcuts (`/`, `Esc`, `L`); documentation. See the limitations below. |

## NIT Raipur pilot foundation (added 2026-10-04)

India remains Lumon's broad OSINT/GEOINT coverage area. NIT Raipur is the
detailed demonstrator. This step adds **structure only**: configuration,
honest status, UI, search scoping and the evidence contract. It adds no
model, no institutional data and no NIT ground truth.

| Part | What exists now |
|---|---|
| Pilot configuration | `config/pilots/nit-raipur.json`: canonical name, two study areas (`nit-raipur`, `nit-raipur-campus`), boundary rules, description, status. |
| Study-area geometry | **PROVISIONAL.** No campus boundary is in the repository, and no coordinates are typed in. The provisional area is derived at runtime from the staged Natural Earth "Raipur" city point with a ±8 km square. It is labelled "search area, not campus boundary" everywhere. A file at `data/pilots/nit-raipur/campus-boundary.geojson` replaces it automatically (STAGED · UNVERIFIED, or VERIFIED when its `.meta.json` says `"verified": true`), with no UI change. |
| Pilot data registry | 13 expected datasets: boundary, master plan, buildings, roads, land cover, drainage, historical plans, construction records, historical imagery, ground-truth photos, ground-truth change events, DEM, authorized drone imagery. States: **0 available, 0 partial, 9 expected, 3 not staged, 1 not available.** |
| AI/ML capability registry | `config/ai/capabilities.json`: 11 capabilities (semantic search, image similarity, discovery, EO embedding, segmentation, learned change detection, land cover, construction, vegetation, temporal analysis, evidence-grounded summary). All NOT STAGED except **semantic search, image-to-image similarity and discovery** (RemoteCLIP) **and learned change detection** (BTC-B), **which are STAGED** (model present, not validated, so never READY) with a separate retrieval status (READY today). Each lists required data, candidate model, component, output and limitations. Where a non-ML component already runs on the demo AOI it is named as the `fallback`. |
| Model registry | `config/ai/models.json`: Prithvi-EO-2.0 family, SAM 2, ChangeFormer, RemoteCLIP, BTC-B. **RemoteCLIP ViT-B-32 and BTC-B (OSCD) are STAGED** (weights, checksum, licence and measured CPU cost recorded; status read at runtime from the files present). The others remain NOT STAGED, with no weights downloaded; ChangeFormer's public checkpoints were rejected (0.5 m Google Earth training data). |
| Evidence contract | `server/lumon/ai_outputs.py`: every future AI output must carry capability, study area, source, acquisition and processing dates, geometry, model id and version, confidence with its kind (uncalibrated vs calibrated), evidence references, temporal evidence, a provenance record and review state. It is validated and stored with provenance. The real database holds **no** AI outputs. |
| UI | Overview → OPEN NIT RAIPUR PILOT, or "Open NIT Raipur pilot" from a NIT-scoped search. The panel shows the study area and its geometry status, data counts with the registry, AI/ML counts and one row per capability (status, one line, ⓘ), and the candidate models. A map layer (REFERENCE → "NIT Raipur study area") draws the provisional outline labelled with its status. The navigation structure is unchanged. |
| Search | Plans now carry a **study area** (India / NIT Raipur / NIT Raipur Campus), an **operation** (the capability needed, with its status), a **time range** (adds "after YYYY") and an optional **target class**. NIT-scoped imagery questions return **NOT STAGED** with reasons and no results. India-scope change queries still run the heuristic engine but report **PARTIAL** (learned model not staged; fallback named). OSINT searches scoped to NIT run on the provisional polygon with a warning. Unsupported requests are refused as before. |

**To plug in later** (no code change needed for the first two): a campus
boundary file; pilot imagery via the existing `ingest-imagery` pipeline
once the study area is confirmed (add an AOI for it); building footprints
and inventory (for "near the hostels" / "buildings added"); ground-truth
change events (into `config/evaluation/reference_dates.json`); model
weights, with a capability's status changed only after real validation.

## AI/ML capability 01: semantic satellite image search (added 2026-10-04)

| Item | State |
|---|---|
| Model | **RemoteCLIP ViT-B-32** (Liu et al., IEEE TGRS 2024; Apache-2.0), `data/models/remoteclip/RemoteCLIP-ViT-B-32.pt`, 605,208,421 bytes, SHA-256 `60014e39…85c4` (matches Hugging Face; checked again at every load). open_clip_torch 3.3.0 + torch 2.14.1, CPU. The only model downloaded. |
| What is ML | The image embedding of every chip, the text embedding of the query, and therefore the ranking. |
| What is rule-based | Scene eligibility (quality status, file checksum, radiometric offset, band layout), chip cutting (2.24 km and 1.12 km squares edge to edge), cloud exclusion (> 10 % SCL cloud/shadow/no-data), true-colour rendering (B04/B03/B02, reflectance = DN × 0.0001 + offset, fixed 0–0.3 stretch, same as evidence chips), time filters. The stored GeoTIFFs are only read. |
| Index | 63 scenes → **2,118 chips** embedded in 23 s (first run); a repeat run embeds 0 and finishes in 1.4 s (cache keyed by weights checksum + preprocessing version). 24 chips and 18 scenes (quarantined/unusable) excluded with reasons. Embeddings: 4.3 MB in SQLite (`semantic_chips`, `semantic_scenes`). |
| Search | ~10–30 ms per query (text encoding + exact cosine over 2,118 vectors) after a one-time model load of 2.2–2.9 s per API process. Each result: chip id, scene id, acquisition date, AOI, chip footprint (from the raster's geotransform), cosine score labelled "model similarity, not a probability or detection", rank, provenance. |
| Where | Imagery → SEMANTIC IMAGE SEARCH (status, model version, indexed counts, query time, thumbnails); command bar with explicit phrasing ("show satellite images of an airport runway since 2023") → Search results with thumbnails. A chip opens in the intelligence panel with its footprint outlined, its scene shown as the Sentinel-2 layer, and TILE HISTORY for the existing tile workflow. Other questions are parsed as before. CLI: `semantic-index`, `semantic-search`, `semantic-eval`. |
| States | NOT STAGED (packages/weights missing, or nothing indexed), INDEXING (job queued/running), PARTIAL (some eligible scenes not indexed), READY, UNAVAILABLE (checksum/load error). Without the model, search is refused (HTTP 409); nothing is substituted. |
| Evaluation | `docs/SEMANTIC_SEARCH_EVAL.md`, 9 queries in `config/ai/semantic_eval.json`. No accuracy metric (no ground truth). Water queries agree with a water index (top-10 0.27–0.32 water vs 0.09 archive vs ≤ 0.02 bottom-10); runway, river and bare-soil queries ranked relevant chips above irrelevant ones on visual review. **Failures:** vegetation/farmland queries (bare soil ranked above green fields); negative controls ("snow covered mountains", "ships in a harbour") still score 0.31–0.32, close to real matches. |
| Image-to-image similarity (added 2026-10-05) | Same model and embedding cache, no inference at query time (~10 ms). Example = a chip, or the chip under a map point (latest indexed scene). Scopes OTHER PLACES (best date per place, overlap > 50 % of the smaller window = same place), SAME PLACE (every date at the example's window), ALL; same chip size only; "not like this" negatives (query = mean(examples) − 0.5 × mean(negatives)). UI: chip detail → SIMILAR IMAGE CHIPS; map click inside the AOI → tile panel → SIMILAR IMAGERY (RemoteCLIP), next to the unchanged non-semantic SIMILAR TILES; command bar "Find imagery similar to this location" with a location selected in indexed imagery now runs (24 results) — every other similarity question is refused exactly as before. API `POST /api/semantic/similar`, `GET /api/semantic/chips/at`. Capability `image-similarity` now points to RemoteCLIP (status from the files present; the spectral tool is listed as a separate non-ML tool, never substituted). Evaluation in SEMANTIC_SEARCH_EVAL.md §3: same-place ranking tracks the airport construction (Spearman 0.81 with year); water look-alikes agree with a water index; vegetation does not; haze influences similarity. |
| NIT Raipur | No pilot imagery is staged, so pilot-scoped image searches report NOT STAGED. No pilot results were produced. |
| Resources | +778 MB in `.venv` (torch etc., `server/requirements-ml.txt`), 592 MiB weights, 4.3 MB embeddings; API process ~500 MB resident with the model loaded (~1.5 GB peak while loading, measured standalone). |

## AI/ML capability 03: discovery & embedding clustering, PS 26227 §2.2.4 (added 2026-10-05)

| Item | State |
|---|---|
| Input | The 2,118 cached RemoteCLIP embeddings (512-d, unit length, 4.3 MB in memory). No re-embedding, no model loaded, no download, no new dependency. Invalid vectors (wrong length, NaN, not unit) are excluded and reported. |
| Method | Spherical k-means per chip size (2.24 km, 1.12 km) in numpy; k by highest cosine silhouette from `round(sqrt(n/2) × {0.25,0.35,0.5,0.7,1})`; k-means++ seeds 0–4; deterministic (2,118/2,118 identical across two real builds). Raw embeddings kept (scene-centring tested and rejected: it collapses clusters onto single places). |
| Result | Version `disc-v002-0ebf8f33`: **18 clusters** (4 + 14), all 34 places, 0 excluded; silhouette 0.215 / 0.191 (weak structure). Neutral names only (`CLUSTER 07 · 1.12 km`). |
| Records | `discovery_versions` (method, parameters incl. the k/silhouette table, input fingerprint, provenance, incremental counts), `discovery_clusters` (frozen centroid, assignment radius, size, places, dates, AOIs, extent, representatives, compactness), `discovery_members` (cluster, similarity, assigned_by, AOI, scene, date, window, footprint, embedding model key + preprocessing version). |
| Workflow | Any chip (semantic result, similarity result, cluster member on the map, or the imagery tile under a map click) → DISCOVER CLUSTER → the cluster's other places ranked by similarity to the reference, with dates-in-cluster → SHOW CLUSTER ON MAP (cyan footprints, reference place in white; click → chip detail). Imagery → DISCOVERY / CLUSTERS lists all clusters with representatives, BUILD (new version) and UPDATE (incremental). API `/api/discovery/...`; CLI `discovery-build`, `discovery-update`, `discovery-eval`. Image similarity is unchanged and separate. |
| Incremental | `update()` assigns chips embedded after the build to the frozen centroid of their family when within its radius (least similar member, or mean − 3 SD if lower), else UNASSIGNED (status PARTIAL). Real test on a scratch copy: 1,914 pre-2026 chips clustered, 204 chips of 2026 added: 195 assigned, 9 unassigned, 28 ms; agreement with a full re-cluster (Rand index) 0.98 / 0.89. Incremental assignment, **not** incremental re-clustering. |
| Evaluation | `docs/DISCOVERY_EVAL.md`: 10-NN in same cluster 0.79–0.83 (chance 0.09–0.25), 0.68–0.80 counting other places only; no single-place clusters; useful cross-place groups (airport complex, earthworks, bare ground, creek embankment), haze-driven (224-01, 112-03, 112-13), season/clipping-driven (112-14), one-dominant-place (112-09, 112-11, 112-12, 112-07) and weak clusters. No accuracy claimed. |
| States | NOT STAGED (model not staged, no embeddings or no version), INDEXING (job), PARTIAL (pending or unassigned chips), READY, UNAVAILABLE (embedding version changed since the build). |
| Also fixed | Dossiers no longer shows a fixed "SEMANTIC TEXT SEARCH — MODEL NOT STAGED": it reads the live RemoteCLIP and discovery states. The chip panel header now reads "IMAGE CHIP · REMOTECLIP INDEX" for every chip (it said "SEMANTIC SEARCH" also for similarity and cluster results). |
| Cost | ~1.1 MB SQLite per version (2 stored), ~130 MB peak memory, 0.5 s per build. |

## AI/ML capability 02: learned change detection, BTC-B (added 2026-10-05)

| Item | State |
|---|---|
| Model | **BTC-B** (Swin-B + UperNet, feature subtraction; Rolih et al., IEEE TGRS 2025), OSCD checkpoint `blaz-r/BTC-B_oscd96` @ 8666e51: 484,579,612 bytes, SHA-256 `8c8e57dc…fa0d` (matches Hugging Face; checked at every load). Code from github.com/blaz-r/BTC-change-detection @ db41090 (MIT); the UperNet head is vendored (Apache-2.0, from Hugging Face transformers via BTC). **Licence:** checkpoint licence unstated, trained on OSCD (CC BY-NC-SA) → non-commercial research use only. |
| Compatibility | Python 3.14: `transformers` 5.18.0 installed (17 MB); pip moved `huggingface_hub` 2.1.1 → 1.33.0 (required by `tokenizers`); `pip check` clean; semantic search output verified byte-identical before/after. transformers 5 renamed Swin parameters; the checkpoint (saved with 4.x) is mapped with transformers' own legacy renaming rules, written out in `btc_model.py`; every tensor must match (one documented exception: the unused final LayerNorm) and the 24 stored relative-position indices must equal the recomputed ones. |
| Verified | On OSCD's own test split (385 lossless tile pairs, official preprocessing): **F1 0.540, precision 0.620, recall 0.479, IoU 0.370**, vs published 0.537–0.549 / 0.622–0.657 / 0.469–0.472 / 0.367–0.379. Same image twice → 0 % flagged. |
| Pairs | 63 usable scenes on one identical grid. A pair is **refused** for misregistration > 1.0 px (measured by phase correlation; nothing is resampled), < 50 % clear in both, different grid, wrong order, or an unusable scene; warned for > 0.5 px, cloud, or a season gap. 499 "suggested" pairs (different years, ≤ 1 month apart in season, ≥ 95 % clear, ≤ 0.5 px). |
| Inference | True colour as for evidence chips (B04/B03/B02, reflectance 0–0.3 fixed); 96 px tiles (OSCD size) every 64 px → 256 px, ImageNet-normalised, before first; sigmoid; resized back and overlap-averaged on the native 10 m grid. ~18 s per scene pair on the M4 CPU (56 tiles, 0.31 s per tile pair); ~3.2 GB peak while loading, 5.4 GB peak process in a CLI run. |
| Outputs | Per run: `probability.tif` (float32 score) and `candidates.tif` (1 / 0 / 255 = not clear in both) on the scenes' own grid, tagged with both scene ids, dates, model key and label; regions ≥ 9 px (0.09 ha) as polygons with mean/max score, spectral before/after (NDVI, MNDWI, brightness), rule-based flags (season, water, cloud edge, thin shape + misregistration, near resolution limit, not persistent in later same-season scenes), persistence and overlap with the rule-based baseline; provenance record; audit entries for runs and reviews. Tables `ml_change_runs`, `ml_change_regions`; files in `data/change_ml/`. |
| Separation | Own tables, files, API (`/api/ml-change/...`), map layer "ML change candidates" (magenta, dashed; one run at a time) and UI section (Change Explorer → LEARNED CHANGE DETECTION, below the RULE-BASED PIPELINE). Rule-based change events (orange) unchanged and labelled "rule-based". Command-bar change questions still answer from the rule-based engine and now say so. Missing weights → NOT STAGED; load/checksum failure → UNAVAILABLE; nothing is substituted. |
| Review | Region detail: before / after / score for the same window, flags, baseline overlap, source scenes, model, provenance; REJECT or PLAUSIBLE (audited). There is no "confirmed" state. |
| States | NOT STAGED, INDEXING (a run queued/running), READY, UNAVAILABLE. PARTIAL is not used by this capability. |
| Evaluation | `docs/LEARNED_CHANGE_EVAL.md`: pairs A–E + control. Successes: the new terminal, aprons, buildings and roads (A, B). False positives: tidal/seasonal creek surfaces across the monsoon (D: 12.4 % flagged vs 4.1 % same-season). False negatives: the runway strip and levelled ground (A). Agreement with the rule-based engine is low (14–16 % of model pixels inside rule-based polygons); neither is ground truth. |
| Runs stored | 5 demo-AOI runs (four from the evaluation, one queued from the UI test); one region rejected during the UI test. |

## Layer toggles and live tracking (added 2026-10-04)

| Item | State |
|---|---|
| Layer switches | **Repaired.** The old 24×12 px switch had an 8 px grey knob on a near-black track and no text, so OFF (and at some display scales ON) read as an empty outlined capsule. Now `components/LayerSwitch.tsx`: 42×18 px, ON = filled accent track + knob right + "ON", OFF = dark track + grey knob left + "OFF"; `role="switch"` + `aria-checked`, focus ring, Space/Enter, tooltip "X is ON — click to hide it". Used in the layer panel and the OSINT drawer. |
| Selection vs visibility | Clicking a layer's **name** selects the row and opens its details (description, coverage, provider, on-map state, minimum zoom). Only the **switch** changes visibility. "Show only staged layers" filters rows without changing any switch. |
| Verified on the real map | Headless Chrome pixel diff at 1440×900 and 1280×800: India boundary OFF changes ~74–76k map pixels, back ON restores the image exactly (0 px difference); Rivers OFF ~5.8–6.3k px; selecting a row changes 0 map pixels; keyboard Space toggles. Districts are drawn only from zoom 4.5 (by design); the row now says "from zoom 4.5". |
| Why aircraft were ~4.6 h old | **Verified cause: there was no scheduler.** The worker only runs queued jobs. `opensky-aircraft` had exactly three connected downloads (08:32, 09:33, 13:03Z), all manual `refresh-all`; other runs were offline snapshot re-reads. Health was `ok`, no failures, no auth or rate-limit errors. The UI correctly showed STALE. |
| OpenSky adapter | OAuth2 client credentials (`OPENSKY_CLIENT_ID` / `OPENSKY_CLIENT_SECRET`, server-side, token cached until 60 s before its 30-min expiry), else anonymous. 20 s timeout. Records `X-Rate-Limit-Remaining`; 429 → `rate-limited` health + `next_allowed_at` from `X-Rate-Limit-Retry-After-Seconds`; 401/403 → token dropped. Failures never delete stored positions; `consecutive_failures` is tracked. |
| Freshness | New **DELAYED** status (live feeds with `delayed_after_hours`; OpenSky: > 3 min, STALE > 15 min). Labels: LIVE / DELAYED / SNAPSHOT / STALE / UNAVAILABLE. Observation time (`time`) and ingestion time (`ingested_at`, event detail "INGESTED") are separate fields. |
| Automatic polling | `lumon/live_poll.py`, started with the API **only** when connected + credentials + `OPENSKY_AGREEMENT_CONFIRMED=yes`. Interval from the credit budget (India query = 4 credits; 4,000/day with 20 % margin → every 109 s, minimum 90 s), exponential backoff to 30 min, honours retry-after. **Status now: NOT CONFIGURED** (no credentials, no agreement). `GET /api/live/status`; shown as AUTO REFRESH on the source card. |
| Fresh positions | One manual on-demand refresh at 17:54:26Z: OpenSky response time 17:54:14Z, 319 state vectors, 248 inside India, credits 400 → 396. Showed LIVE · 1 min. They will turn DELAYED, then STALE, without automatic polling. |
| Vessels | **NOT CONFIGURED.** No AIS feed exists; ports are static entities, not vessels. `sources/vessel_contract.py` defines the minimal vessel position (MMSI, position, observed_at, course, speed, heading, name, source, provider ref) and refuses incomplete reports. aisstream.io would need a free API key, a WebSocket client (new dependency, not added) and accepts 3 connections per account/IP, no SLA; pricing is not stated in its docs. |

## Target architecture vs this build

Target: one API, one worker, PostgreSQL + PostGIS + pgvector, one file
store. Built: one FastAPI process with an embedded worker thread, **SQLite**
(tables shaped for PostGIS: GeoJSON geometry plus bbox columns), and one
`data/` folder. Docker is not installed on the development machine, so the
database server was deliberately not introduced. Migration notes are in
[architecture.md](architecture.md).

## Installed dependencies

Frontend runtime: `react` 19.3, `react-dom` 19.3, `maplibre-gl` 6.12.
Frontend dev: `typescript` 7.0, `vite` 8.3, `@vitejs/plugin-react` 6.1,
`@types/react`, `@types/react-dom`.

Backend (`server/requirements.txt`, project venv only): `fastapi` 0.142.2,
`uvicorn` 0.54.0, `pyshp` 3.1.6, `certifi` 2026.7.22, `rasterio` 1.5.2
(bundles GDAL 3.12.2), `numpy` 2.5.3, `sgp4` 2.27, `pytest` 9.1.1,
`httpx` 0.28.1.

Optional ML (`server/requirements-ml.txt`, project venv only): `torch` 2.14.1,
`torchvision` 0.29.1, `open_clip_torch` 3.3.0 (pulls timm, pillow,
huggingface_hub, ftfy, regex, safetensors and others; the hub is never
contacted: `HF_HUB_OFFLINE=1`, weights load from `data/models/`), and
`transformers` 5.18.0 for BTC-B's Swin backbone (with tokenizers 0.23.2,
typer, rich; `huggingface_hub` moved to 1.33.0). Not installed in the project:
`pyarrow` was placed in a scratch folder only to read the OSCD test parquet.

Nothing was installed system-wide; no Docker, Homebrew or global npm
changes.

## Data sources and models

See [data-sources.md](data-sources.md) and the generated
[SOURCE_MANIFEST.md](SOURCE_MANIFEST.md) (retrieval times, checksums).
19 sources are registered: 16 running, 1 implemented but waiting for a key
(FIRMS), 2 planned (AIS, OpenAQ). Models: **RemoteCLIP ViT-B-32 staged**
(semantic image search); language model and learned classifier not staged. The deterministic and feature-based
fallbacks are documented and labelled in the UI.

## Current data state (2026-10-04, after the 13:04Z refresh)

| Item | Count |
|---|---|
| OSINT events | 388: aircraft 245, Kp 60, weather (40 cities) 40, launches 30 (6 scheduled in the next 90 days), earthquakes 5, internet outage signals 4, satellites over India 3, EONET 1 |
| Entities | 1,900: power plants 1,571, airports 317, ports 12 |
| Source statuses at refresh | 16 LIVE, 1 KEY REQUIRED (FIRMS), 2 NOT IMPLEMENTED. Without a refresh, fast sources turn STALE after their limit (aircraft 15 min, weather and satellites 1 h). |
| Satellite scenes (demo AOI) | 63 usable (2018-01-03 to 2026-10-03), 17 quarantined (15 unrecognised baseline `00.01`, 2 radiometric metadata contradicted by data), 1 unusable (cloud) |
| Change candidates | 12 accepted, 97 suppressed (2,160 tiles, 136,080 tile observations) |
| Analyst decisions | 0 |
| Audit log | 129 entries, chain intact; entry 104 is an exact duplicate of 103 from a concurrent double-write before writes were serialised. It is reported by `verify-audit`, not removed. |

## Storage footprint (measured)

Measured with `du -sh` (binary units, MiB).

| Item | Size |
|---|---|
| `data/` total | 1.3 GiB |
| ↳ RemoteCLIP weights (`data/models/remoteclip`) | 592 MiB |
| ↳ BTC-B weights (`data/models/btc`) | 462 MiB |
| ↳ learned change outputs (`data/change_ml`, 5 runs) | 3.9 MiB |
| ↳ satellite imagery (63 usable scenes, AOI windows) | 105 MiB |
| ↳ SQLite database (65,449,984 bytes, incl. 4.3 MB embeddings) | 62 MiB |
| ↳ source snapshots | 48 MiB |
| ↳ raw boundary downloads | 20 MiB |
| ↳ processed boundaries | 3.5 MiB |
| ↳ reference layers | 0.7 MiB |
| `.venv/` | 1.0 GiB (154 MiB before the optional ML packages) |
| `node_modules/` | 112 MiB |
| `dist/` (production build) | 2.9 MiB |
| `public/` (logo + fonts) | 0.9 MiB |

## Tests and checks (final run)

| Check | Result |
|---|---|
| `npm run build` (TypeScript + Vite) | **Pass** |
| TypeScript `tsc --noEmit` | **Pass** |
| UI unit tests (`npm run test:ui`) | **9 passed** (includes event-kind consistency and 4 layer-toggle tests: flip/restore, unstaged never ON, visibility follows switch, every boundary/reference/pilot/analysis layer maps to real map layers) |
| Backend tests (`npm run test:api`) | **117 passed** (105 below + 12 discovery tests on synthetic embeddings: k-means/silhouette, build with neutral labels, determinism, reference → cluster → ranked places, metadata preservation, map footprints, incremental assignment with frozen centroids and unassigned outliers, version change → UNAVAILABLE, invalid embeddings excluded, missing embeddings/model, similarity and BTC-B/rule-based tables untouched, API). Earlier **105 passed** (86 below + 18 learned-change tests on synthetic GeoTIFF pairs with controlled changes and a fake network: tile coverage, shift measurement, controlled change found with georeferenced outputs and tags, cloud masked, small change flagged, minimum region size, cached reruns, 5 refusal cases, season warning, baseline overlap reported without touching the rule-based table, audited review with no "confirmed" state, NOT STAGED without weights, API incl. GeoTIFF/PNG, one run at a time on the map, command-bar rule-based notice, and the REAL BTC-B weights giving no change for identical images; + 1 registry test). Earlier **86 passed** (77 below + 9 image-similarity tests: window overlap, ranking without repeating the example, same-size and one-per-place rules, scopes, negatives, metadata, chip under a point, refusal without the model, API incl. route order, command-bar selected-location path and unchanged refusals). Earlier **77 passed**: 66 earlier (2 pilot tests made independent of whether the weights exist) + 1 staged-registry test + 10 semantic-search tests (chip coverage, cache reuse, ranking, time filter, metadata/footprint preservation, invalid imagery exclusions, status states, missing weights → NOT STAGED with no substitute, API refusal, parser routing) on synthetic GeoTIFFs and a fake embedding model. Earlier: 8 live-tracking tests ( credits and observation time, 429 keeps positions, 503 counts failures, OAuth token reuse, LIVE/DELAYED/STALE/SNAPSHOT/UNAVAILABLE labels, poller NOT CONFIGURED/DISABLED, interval and backoff limits, vessel contract; 1 Starlette deprecation warning) |
| Offline verification (`verify-offline`) | **17 passed, 0 failed, 0 blocked connections**, on a temporary database copy, including real RemoteCLIP inference, image-to-image similarity, real BTC-B inference and discovery clusters with the network blocked |
| Discovery UI (headless Chrome) | **Pass** at 1440×900 and 1280×800: 18 clusters listed, representatives, SHOW CLUSTER ON MAP (layer ON, member click opens the chip), DISCOVER CLUSTER from a semantic result, a similarity result and a map tile; Dossiers shows "RemoteCLIP · READY"; no console errors |
| Real-data regressions | semantic search + image similarity outputs byte-identical to the fingerprint taken before this stage; BTC-B runs/regions and rule-based candidates unchanged |
| Learned change UI (headless Chrome) | **Pass** at 1440×900 and 1280×800: status READY, 60 suggested pairs, refused pair explained and RUN disabled, pair check facts, run queued from the UI and finished, run images, region list, region detail (3 images, evidence, baseline, provenance), REJECT audited, map layer ON; no console errors |
| Image similarity UI (headless Chrome) | **Pass** at 1440×900 and 1280×800: chip → similar chips (8 other places / 12 same-place dates), NOT LIKE re-rank, open result, tile panel SIMILAR IMAGERY (12) next to the old SIMILAR TILES (8, unchanged), command bar with selected location (24); no console errors |
| Semantic search UI (headless Chrome) | **Pass** at 1440×900 and 1280×800: search, 24 chip thumbnails load, chip detail, footprint, scene layer ON, tile history; command-bar image query (30 results) and an unrelated query (still FIND EVENTS); no console errors |
| Layer toggle map test (headless Chrome pixel diff) | **Pass** at 1440×900 and 1280×800 (see Layer toggles section) |
| Audit chain (`verify-audit`) | **Intact**, 183 entries, 1 exact duplicate noted (id 104) |
| UI smoke test (headless Chrome, dev server) | **Pass** at 1440×900 and 1280×800: no console errors, no horizontal scroll |
| Change engine on demo AOI | 63 scenes, 2,160 tiles, 220 tiles with a transition, **12 accepted / 97 suppressed** candidates, 0.74–0.76 s (last evaluation run 2026-10-04 09:38Z) |
| Query latency (median, 6 fixed queries, same evaluation run) | 0.4–63 ms |

## Known limitations

- **Discovery clusters are groupings, not classes**; weak structure
  (silhouette ≈ 0.2); some clusters follow haze, season or one place; the
  2.24 km family has only 9 places in the single AOI; updates assign to
  existing clusters only (re-cluster for new kinds of imagery).
- **Learned change detection gives candidates, not verified change.**
  Trained on urban labels in L1C imagery (Lumon: L2A); misses large
  homogeneous changes (runways, levelled ground); flags seasonal and tidal
  surfaces across seasons; scores smoothed by tile resizing (~1 px halo);
  non-commercial-only checkpoint; ~3–5 GB memory while loading.
- **Low free disk:** about 2.5 GB free on this machine at the end of the
  discovery stage (most of the drop since 2026-10-04 is outside the project).
- **Image-to-image similarity is visual likeness of 10 m chips**, also
  driven by haze and date-specific colour; it fails for vegetation
  examples; one small AOI gives few "other places".
- **Semantic image search ranks; it does not detect.** Scores are cosine
  similarity, comparable only within one query; absent things still get
  ranked results. RemoteCLIP is used on 10 m Sentinel-2, coarser than most
  of its training imagery, and vegetation/farmland queries fail on this
  archive. Results repeat the same place across dates. One AOI only.

- **Change classes come from spectral-index thresholds and a relative
  colour rule** (built vs soil). They are not validated against ground
  truth. One accepted construction event was checked visually, which is
  not a validation.
- Changes are mapped on a **100 m tile grid**, so polygons are blocky and
  areas are reported as ranges.
- Only **one AOI** (≈ 22 km²) has imagery. Satellite questions about the
  rest of India answer "NO IMAGERY STAGED" (shown as a capability warning).
- The "near river" relation uses the **imagery-derived water mask inside
  AOIs**; Natural Earth rivers are too generalised for distances under
  2 km.
- **NASA EONET flood polygons for India are quarantined** because of the
  upstream latitude/longitude swap (by design; see data-sources.md).
- OpenSky aircraft and CelesTrak satellites are **snapshots at refresh
  time**, not live streams. OpenSky coverage over India is incomplete, and
  some state vectors carry a position time several minutes older than the
  response. Automatic OpenSky polling is built but NOT CONFIGURED: OpenSky's
  terms require a written agreement for any automated REST use, plus an API
  client for a usable credit budget. When it runs, each poll writes a
  `source-refresh` audit entry (~800 per day at 109 s).
- Weather is **model analysis at 40 cities**, not station observations or
  radar.
- The query parser has a fixed vocabulary. Free regional phrases ("western
  coast") are reported as not understood. A single time expression applies
  to the whole plan.
- Analyst identity is a free-text name (no authentication or multi-user
  roles).
- GeoPackage export is checked structurally by tests. It was not opened in
  desktop GIS software on this machine.
- Map label glyphs cover Latin scripts only.
- The production bundle is ~1.35 MB JS (mostly MapLibre); not code-split.
- **No automatic source refresh** (except OpenSky once configured). Fast sources show STALE once older than
  their limit until someone refreshes them (Sources → REFRESH, or
  `refresh-all`). The freshness limits are judgement calls based on each
  source's update rhythm and can be changed in
  `config/sources/registry.json`.
- **EVENTS IN VIEW vs the timeline's main band** measure different things
  by design: the status bar counts everything drawn on the map (including
  visible aircraft/satellite/weather points), while the main timeline band
  excludes telemetry, which has its own band covering all of India.
- In the **development server**, React StrictMode runs effects twice, so
  opening a change's evidence writes two sequential `open-evidence` audit
  entries. Production builds write one.
- Audit entry **104** remains in the log as a reported exact duplicate
  (see Current data state).
- **NIT Raipur pilot:** the study area is a PROVISIONAL search area
  centred on Raipur's gazetteer point, not the campus. No pilot imagery,
  institutional data, ground truth or AI/ML model is staged, so every pilot
  capability is NOT STAGED and NIT-scoped imagery questions return no
  results by design. The "Dossiers" section is still example-based
  similar-site discovery; entity dossiers are not implemented.

## Next recommended step

1. **Review session**: confirm/reject the 12 accepted candidates (and
   sample suppressed ones) in the UI, then run `evaluate`. That turns on
   precision@K, false alarms per 100 km² and decision time, and gives the
   first labels for score calibration.
2. **Keys**: add `NASA_FIRMS_MAP_KEY` (free) to activate fire detections.
   Decide on AIS (aisstream.io key + streaming client).
   For live aircraft: obtain OpenSky's written agreement, create an API
   client, then set `OPENSKY_CLIENT_ID`, `OPENSKY_CLIENT_SECRET` and
   `OPENSKY_AGREEMENT_CONFIRMED=yes` in `.env.local` and restart the API.
3. **Database**: when Docker/PostgreSQL is available, move to
   PostGIS + pgvector following the notes in architecture.md.
4. **Learned change**: review the candidates of the suggested pairs and
   record decisions; use them, not the rule-based engine, to estimate the
   model's precision on this archive. Free disk space before staging more
   models.
5. **Semantic search**: collect analyst relevance judgements on search
   results to measure precision for a fixed query set; stage NIT Raipur
   imagery (an AOI for the study area) so pilot image search can run;
   consider de-duplicating results by place.
6. **Second AOI** in a different landscape (inland river, agriculture) to
   test how the gates generalise.
