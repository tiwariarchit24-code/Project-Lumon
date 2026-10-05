# Ingestion

How data enters Lumon, and how to add more. All ingestion runs in the
backend. Downloads happen only in `LUMON_MODE=connected`.

## OSINT sources

`server/lumon/ingest.py → refresh_source(source_id)`

1. **Fetch.** Connected: the adapter downloads through `lumon.net`.
   Air-gapped: the newest saved snapshot is re-read instead.
2. **Snapshot.** The raw bytes are saved untouched to
   `data/snapshots/<source>/<UTC time>.<ext>` with a SHA-256 checksum. Ten
   snapshots are kept for event feeds; one for large static downloads.
3. **Normalise.** The adapter returns simple records
   (`sources/common.py`): events (time-based), entities (long-lived) or
   reference features (lines and areas). The original record is kept as
   `raw`.
4. **Validate.** Records with missing or impossible coordinates, or no
   parseable time, are quarantined and counted, never stored.
5. **Scope.** Records outside the India operating area are dropped and
   counted. National records (about India, no location) and global context
   (space weather) are kept and labelled. Suspected lat/lon swaps are
   quarantined (see [data-sources.md](data-sources.md)).
6. **Store.** `accumulate` sources upsert by id. `replace` sources replace
   their previous records (positions, current weather, static datasets).
   Reference layers are written to `data/reference/<source>.geojson`.
7. **Record.** A provenance row, a `source_runs` row, source health
   (`ok`, `offline-snapshot`, `failed`, `key-required`) and an audit entry.

A failing source never stops the others and never deletes data stored
earlier. "Last success" always means the last successful **download**.

Commands:

```bash
npm run lumon -- refresh <source_id>
npm run lumon -- refresh-all
```

From the UI: SYSTEM → Sources → REFRESH (queues a job for the worker).

### Adding a source

1. Add an entry to `config/sources/registry.json` (licence, coverage,
   attribution, key requirement, kind, mode, evidence type).
2. Write `server/lumon/sources/<adapter>.py` with `fetch(context) -> bytes`
   and `normalize(data, context) -> list[dict]`. Use `make_event`,
   `make_entity` or `make_reference`.
3. Map its event or entity types to a layer in `config/layers.json`, and
   to the source in `EVENT_TYPE_SOURCES` / `ENTITY_TYPE_SOURCES`
   (`query/engine.py`) so capability checks know about it.
4. Add a small test with a synthetic fixture to `server/tests/test_sources.py`.

## Boundaries

`npm run lumon -- stage-boundaries` downloads every dataset in
`config/geo/boundary-datasets.json`, keeps the raw copy in `data/raw/`,
filters to India, simplifies (Douglas–Peucker), records checksums in
`data/boundaries/manifest.json` and writes provenance. To change what
"inside India" means, edit `config/geo/operating-area.json`.

## Satellite imagery

Imagery enters Lumon in two ways. Both register scenes in the same `scenes`
table, so every downstream pipeline (semantic index, image similarity,
discovery, rule-based change, BTC-B, review, export) treats them alike.

| | EARTH SEARCH / REMOTE INGESTION | LOCAL OFFLINE INGESTION |
|---|---|---|
| Command | `ingest-imagery <aoi>` | `ingest-local <file\|folder> --aoi <id>` |
| Network | required (STAC search + remote COG windows) | **none** |
| Input | Sentinel-2 L2A from Earth Search | local GeoTIFF / COG files |
| AOI | configured in `config/aois.json` | an existing AOI with the same grid, or a new local AOI defined by the raster |

### Earth Search / remote ingestion

`server/lumon/imagery/archive.py → ingest_aoi(aoi_id)`

1. **Search** the Earth Search STAC API for the AOI, year by year.
2. **Select** at most one scene per month: full AOI coverage first, then
   lowest cloud, then newest reprocessing. Months already in the archive
   are skipped, so runs are incremental and back-filled scenes are just
   added.
3. **Validate metadata.** Missing time or CRS, unexpected CRS, unknown
   processing baseline or undecidable radiometric offset leads to
   **quarantine with a reason**.
4. **Download only the AOI window** of B02, B03, B04, B08, B11 and SCL
   from the remote COGs (about 1.7 MB per scene).
5. **Check radiometry.** The dark-pixel test must agree with the metadata
   offset, otherwise the scene is quarantined.
6. **Measure quality** from SCL: usable (≥ 90 % clear), degraded
   (60–90 %), unusable (< 60 %, file not kept, next candidate tried).
7. **Store** one GeoTIFF per scene with the original numbers (no
   corrections baked in), and register checksum, baseline, offset and
   provenance.

```bash
npm run lumon -- ingest-imagery demo-01        # all new months
npm run lumon -- ingest-imagery demo-01 5      # at most 5 new scenes
npm run lumon -- recheck-radiometry demo-01    # re-decide offsets for staged scenes
npm run lumon -- analyse demo-01               # change engine (see change-detection.md)
```

### Local offline ingestion (GeoTIFF / COG)

`server/lumon/imagery/local_ingest.py → ingest_file(path, aoi_id, ...)`

```bash
npm run lumon -- ingest-local /data/organiser/scene_2024-01-12.tif --aoi organiser-aoi
npm run lumon -- ingest-local /data/organiser/rgbn.tif --aoi organiser-aoi \
    --acquired 2024-01-12 --sensor sentinel-2b --reflectance-offset 0 \
    --bands B02=3,B03=2,B04=1,B08=4,B11=5,SCL=6
npm run lumon -- ingest-local /data/organiser/ --aoi organiser-aoi --index   # every .tif in the folder, then index
```

**Supported formats.** GeoTIFF (tiled or striped) and Cloud Optimized
GeoTIFF; the format is reported (COG when GDAL reports `LAYOUT=COG`). Files
are read in place: no copy, no conversion, no reprojection, no resampling.
Remote paths (`http://`, `s3://`, `/vsicurl`…) are refused.

**Validation** (each failure prints `LOCAL INGEST FAILED` and the reason):
file missing or unreadable, not GeoTIFF, empty raster, no CRS, no
georeferencing (identity transform), rotated/sheared transform,
inconsistent band mapping, an acquisition tag without a time zone, file
and operator dates that disagree, a pixel grid that differs from the
chosen existing AOI, a scene id already used by another file.

**Metadata preserved** (in the scene record and its provenance record):
original path, filename, SHA-256 (streamed), size, format, overviews, CRS,
affine transform, width/height, resolution, native and WGS84 bounds, band
count, band names, data types, nodata, file tags, acquisition time,
sensor, processing level, radiometric offset, band mapping, and where each
value came from (`file tag …`, `operator`, or `unknown`).

**Required metadata.** Only the acquisition date is required to register a
scene. It is read from a recognised tag (`datetime`, `ACQUISITION_DATETIME`,
`ACQUISITION_DATE`, `SENSING_TIME`, `PRODUCT_START_TIME`,
`DATATAKE_1_DATATAKE_SENSING_START`; never the TIFF `DateTime` tag, which is
the file's write time) or given with `--acquired`. Without one the file is
recorded as **PENDING METADATA** (`ACQUISITION DATE: UNKNOWN`) and not added
to the scenes table until a date is supplied.

**Optional metadata.** `--sensor` (else a sensor tag, else `unknown`),
`--level` (else a level tag, else none), `--reflectance-offset` (else the
Sentinel-2 tag pair `processing_baseline` + `boa_offset_applied`, else
unknown), `--scene-id` (default `LOCAL_<first 12 of SHA-256>`), `--aoi-name`.

**Band mapping.** Lumon's imagery pipelines read B02, B03, B04, B08, B11
and SCL (quality) as uint16 Sentinel-2 L2A digital numbers at 10 m. Bands
are identified only by the file's own band names (when they are exactly
these names) or by an explicit `--bands NAME=INDEX,...` mapping — never by
position alone. A file already in canonical order is used as is; otherwise
a small GDAL VRT (a few KB of XML referencing the original) presents the
mapped bands in canonical order. No pixel data is duplicated.

**Compatibility** is reported per capability — `SUPPORTED`,
`NOT COMPATIBLE: <reason>` or `NOT ENOUGH METADATA: <reason>`:
registration, semantic-index, image-similarity, discovery,
rule-based-change, learned-change. A file that cannot serve the pipelines
(missing canonical bands, not uint16, not 10 m, CRS not projected in metres,
offset unknown) is still registered, but **quarantined** with that reason,
so no pipeline uses it. Re-running the same file with the missing operator
metadata (`--bands`, `--reflectance-offset`, …) replaces such a quarantined
record (it was never used). Quality (usable / degraded / unusable) is
measured from the scene's own SCL band, and the existing dark-pixel test
checks the radiometric offset (a conflict quarantines the scene).

**Checksum / provenance.** Each file gets a `provenance` record (kind
`scene`, source `local-file`, input `file://<path>`, input SHA-256) with all
the metadata above, a `local_sources` row, and an audit entry
(`local-ingest`). `scenes.file_sha256` is the SHA-256 of the raster the
pipeline reads (the original, or the VRT); the original's SHA-256 is in
`local_sources` and the provenance record.

**Duplicates and incremental ingestion.** Files are identified by SHA-256:
the same file again prints `ALREADY INGESTED` with its scene id and changes
nothing. A new file adds one scene; then the existing incremental steps
process only what is new: `semantic-index <aoi>` embeds only the new
chips, `discovery-update` assigns them to the current clusters, `analyse
<aoi>` reuses cached tile observations, and `ml-change-run` takes any pair
on the same grid. `--index` runs the first two after ingestion.

**AOIs.** `--aoi` names an existing AOI (the file must have exactly its
pixel grid) or a new id. A new id creates a **local AOI** whose box and
pixel grid come from the raster's own georeferencing (table `local_aois`);
later files for that AOI must match the grid. Local AOIs appear wherever
configured AOIs do (Imagery drawer, map, change engine); the Earth Search
ingest refuses them.

**Offline.** The whole path reads only local files and the local database.
Tested with network sockets blocked (`tests/test_local_ingest.py`), and in
`verify-offline` (a real staged scene registered on the temporary
database copy).

**Limitations.**
- The imagery pipelines are built for Sentinel-2 L2A at 10 m (B02–B11 + SCL,
  uint16). Other sensors register but are NOT COMPATIBLE with them; there is
  no cloud mask without an SCL band, and nothing is resampled.
- Each scene in an AOI must share one pixel grid; files on another grid
  need their own AOI (no reprojection or resampling is done).
- The pipelines read whole scenes into memory and check the scene file's
  SHA-256 on each index run; very large organiser files (e.g. a full
  110 km tile) will be slow and memory-hungry. Not measured.
- A date given as `YYYY-MM-DD` is stored at 00:00Z with its source recorded
  as "operator (date only)".
- Folder ingestion uses each file's own tags; per-file operator dates need
  one command per file.

### Adding an AOI

Add an entry to `config/aois.json`: an id, name, small bbox (a few km) and
imagery settings. Then run `ingest-imagery` and `analyse`. For local or
organiser-supplied files, no config entry is needed: `ingest-local <file>
--aoi <new id>` creates the AOI from the raster itself. Keep AOIs small:
about 1.7 MB per scene for 20 km² at 10 m.

## Jobs

`server/lumon/worker.py` processes the `jobs` table: `source-refresh`,
`refresh-all`, `imagery-ingest`, `change-analysis`, `evaluation`. It runs
as a thread in the API by default. Set `LUMON_EMBEDDED_WORKER=0` and run
`npm run lumon -- worker` to run it as a separate process.
