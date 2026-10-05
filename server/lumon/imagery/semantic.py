"""
Semantic satellite image search: text -> ranked Sentinel-2 image chips,
and image -> similar image chips, using the RemoteCLIP ViT-B-32
image/text embedding model.

This is Lumon's first MACHINE-LEARNING capability. What is model-powered and
what is not:
  - model-powered: the 512-number embedding of each image chip and of the
    analyst's text (RemoteCLIP), and therefore the ranking
  - rule-based:    which scenes/chips are eligible (quality status, checksum,
                   cloud mask), how chips are cut and coloured, the time/AOI
                   filters

WHAT A SCORE MEANS: the cosine similarity between the text embedding and the
chip embedding (between -1 and 1; CLIP-style models typically give 0.1-0.35).
It is NOT a probability, a confidence or a detection. A chip ranking first
for "airport runway" means only that, of the staged chips, its embedding is
the closest to that text. It does not prove a runway is there.

THE PIPELINE (index once, search many times):
  1. eligible scenes: usable/degraded quality, file present, file checksum
     equal to the one recorded at ingest, radiometric offset known, the
     expected band layout. Anything else is EXCLUDED with a reason.
  2. chips: square windows of the stored GeoTIFF at two sizes (224 px =
     2.24 km and 112 px = 1.12 km at 10 m), covering the scene edge to edge.
     The stored file is only read, never changed.
  3. chip validity: from the scene's own Scene Classification Layer (SCL);
     a chip with more than 10 % cloud/shadow/no-data pixels is excluded.
  4. colour: true colour (B04, B03, B02), reflectance = DN x 0.0001 + the
     scene's offset, mapped 0-0.3 reflectance -> 0-255 for every scene (the
     same fixed stretch as Lumon's evidence chips).
  5. embed with RemoteCLIP and cache the vector in SQLite (semantic_chips),
     keyed by the model weights checksum and the preprocessing version, so
     a repeated index run embeds only chips it has not seen.
  6. search: embed the text, cosine similarity against the cached vectors,
     highest first. Every result carries its scene id, acquisition date,
     chip footprint (from the raster's own geotransform), score and
     provenance.
  7. similar(): the same cached vectors queried with an example chip's own
     embedding instead of a text (see the section further down).

THE MODEL IS OPTIONAL. If torch/open_clip are not installed or the weights
file is missing, status() is NOT STAGED and search() refuses with that
reason. Nothing else is silently substituted.
"""

import importlib.util
import json
import logging
import math
import os
import threading
import time
from pathlib import Path

import numpy as np
import rasterio
from rasterio.warp import transform as warp_transform
from rasterio.windows import Window

from .. import audit, db, net, provenance, settings
from ..geo import geometry
from . import sentinel2

MODEL_ID = "remoteclip"
PREPROCESS_VERSION = "s2-rgb-b04b03b02-refl0to0.3-scl10pct-v1"
CHIP_SIZES_PX = (224, 112)       # 2.24 km and 1.12 km at 10 m
MAX_INVALID_FRACTION = 0.10      # more cloud/shadow/no-data than this: excluded
STRETCH_MAX = 0.3                # same fixed stretch as imagery/quicklook.py
EXPECTED_BANDS = sentinel2.REFLECTANCE_BANDS + ["SCL"]
BATCH = 32


# ---------------------------------------------------------------------------
# The model adapter
# ---------------------------------------------------------------------------

def model_config() -> dict:
    """The RemoteCLIP entry of config/ai/models.json."""
    models = json.loads((settings.CONFIG_DIR / "ai" / "models.json").read_text())["models"]
    return next(m for m in models if m["id"] == MODEL_ID)


def weights_path() -> Path:
    return settings.ROOT_DIR / model_config()["weights"]["path"]


def model_key() -> str:
    """Identifies the exact model: cached vectors are only reused for the same weights."""
    return f"{MODEL_ID}:{model_config()['weights']['sha256'][:16]}"


def model_problems() -> list[str]:
    """Why the model cannot be used (empty list = files and packages are present)."""
    problems = []
    for package in ("torch", "open_clip"):
        if importlib.util.find_spec(package) is None:
            problems.append(f"Python package '{package}' is not installed (pip install torch open_clip_torch)")
    weights = model_config()["weights"]
    path = weights_path()
    if not path.exists():
        problems.append(f"Weights not staged: {weights['path']} (download {weights['url']})")
    elif path.stat().st_size != weights["size_bytes"]:
        problems.append(f"Weights file has the wrong size ({path.stat().st_size} bytes, expected {weights['size_bytes']}): incomplete or different file")
    return problems


class RemoteClipModel:
    """
    RemoteCLIP ViT-B-32 on the CPU. Loads the weights once, after checking
    their SHA-256 against the registry. No network access: open_clip builds
    the architecture locally and the weights come from data/models/.
    """

    def __init__(self):
        problems = model_problems()
        if problems:
            raise ModelNotStaged("; ".join(problems))
        config = model_config()
        checksum = net.sha256_of_bytes(weights_path().read_bytes())
        if checksum != config["weights"]["sha256"]:
            raise ModelUnavailable(f"Weights checksum mismatch ({checksum[:12]}…, expected {config['weights']['sha256'][:12]}…)")
        os.environ.setdefault("HF_HUB_OFFLINE", "1")  # never reach out to a model hub
        import open_clip
        import torch

        self._torch = torch
        architecture = config["weights"]["architecture"]
        # pretrained=None: random initialisation, immediately replaced by
        # the RemoteCLIP weights below (every key must match).
        # open_clip warns "initialized randomly" here; that is expected and
        # misleading in a log, so it is silenced for this one call.
        logging.disable(logging.WARNING)
        try:
            self.model, _, self.preprocess = open_clip.create_model_and_transforms(architecture, pretrained=None)
        finally:
            logging.disable(logging.NOTSET)
        state = torch.load(weights_path(), map_location="cpu", weights_only=True)
        self.model.load_state_dict(state, strict=True)
        self.model.eval()
        self.tokenizer = open_clip.get_tokenizer(architecture)
        self.key = model_key()
        self.version = {"model": config["name"], "architecture": architecture, "weights_sha256": checksum,
                        "torch": torch.__version__, "open_clip": open_clip.__version__}

    def encode_images(self, chips: list[np.ndarray]) -> np.ndarray:
        """(n, 512) float32, L2-normalised, for uint8 RGB chips."""
        from PIL import Image
        torch = self._torch
        batch = torch.stack([self.preprocess(Image.fromarray(chip)) for chip in chips])
        with torch.no_grad():
            vectors = self.model.encode_image(batch).float()
        vectors = vectors / vectors.norm(dim=-1, keepdim=True)
        return vectors.numpy().astype("float32")

    def encode_text(self, text: str) -> np.ndarray:
        """(512,) float32, L2-normalised."""
        torch = self._torch
        with torch.no_grad():
            vector = self.model.encode_text(self.tokenizer([text]))[0].float()
        vector = vector / vector.norm()
        return vector.numpy().astype("float32")


class ModelNotStaged(RuntimeError):
    """Weights or packages are missing."""


class ModelUnavailable(RuntimeError):
    """Files are present but the model cannot be used (checksum, load error)."""


_model = None
_model_error: str | None = None
_model_lock = threading.Lock()


def get_model():
    """The loaded model (loaded once per process). Raises ModelNotStaged / ModelUnavailable."""
    global _model, _model_error
    with _model_lock:
        if _model is None:
            try:
                _model = RemoteClipModel()
                _model_error = None
            except ModelNotStaged:
                raise
            except Exception as error:
                _model_error = f"{type(error).__name__}: {error}"
                raise ModelUnavailable(_model_error) from error
        return _model


# ---------------------------------------------------------------------------
# Scenes and chips
# ---------------------------------------------------------------------------

def scene_problem(scene: dict) -> str | None:
    """Why a scene cannot be indexed, or None if it is suitable."""
    if scene["quality_status"] not in ("usable", "degraded"):
        reason = scene.get("quarantine_reason")
        return f"quality status {scene['quality_status']}" + (f": {reason}" if reason else "")
    if not scene.get("file_path") or not Path(scene["file_path"]).exists():
        return "scene file not staged"
    if scene.get("radiometric_offset") is None:
        return "radiometric offset unknown (reflectance cannot be computed)"
    if scene.get("file_sha256") and net.sha256_of_bytes(Path(scene["file_path"]).read_bytes()) != scene["file_sha256"]:
        return "file checksum differs from the one recorded at ingest (corrupt or modified file)"
    try:
        with rasterio.open(scene["file_path"]) as source:
            if list(source.descriptions) != EXPECTED_BANDS:
                return f"unexpected band layout {list(source.descriptions)} (expected {EXPECTED_BANDS})"
            if source.crs is None:
                return "no coordinate reference system"
    except rasterio.errors.RasterioIOError as error:
        return f"unreadable raster: {error}"
    return None


def chip_windows(height: int, width: int) -> list[tuple[int, int, int]]:
    """
    (size, row_off, col_off) for every chip: for each chip size, evenly
    spaced square windows from edge to edge (they overlap a little when the
    scene is not an exact multiple of the size). Sizes larger than the scene
    are skipped.
    """
    windows = []
    for size in CHIP_SIZES_PX:
        if size > height or size > width:
            continue
        rows = np.linspace(0, height - size, max(1, math.ceil((height - size) / size) + 1)).round().astype(int)
        cols = np.linspace(0, width - size, max(1, math.ceil((width - size) / size) + 1)).round().astype(int)
        windows += [(size, int(r), int(c)) for r in rows for c in cols]
    return windows


def to_rgb(stack: np.ndarray, offset: float) -> np.ndarray:
    """True-colour uint8 (H, W, 3) from a (bands, H, W) DN stack, fixed 0-0.3 stretch."""
    channels = []
    for band in ("B04", "B03", "B02"):
        reflectance = stack[sentinel2.REFLECTANCE_BANDS.index(band)].astype("float32") * 0.0001 + offset
        channels.append(np.clip(reflectance / STRETCH_MAX * 255, 0, 255).astype("uint8"))
    return np.dstack(channels)


def valid_fraction(stack: np.ndarray) -> float:
    """Share of pixels that are not cloud/shadow/no-data (SCL) and have data in every band."""
    valid = ~np.isin(stack[-1], list(sentinel2.SCL_INVALID))
    for index in range(len(sentinel2.REFLECTANCE_BANDS)):
        valid &= stack[index] > 0
    return float(valid.mean())


def footprint(source, size: int, row_off: int, col_off: int) -> tuple[dict, float, float]:
    """WGS84 polygon of a chip from the raster's own geotransform, plus its centre."""
    transform = source.window_transform(Window(col_off, row_off, size, size))
    corners = [transform @ (0, 0), transform @ (size, 0), transform @ (size, size), transform @ (0, size)]
    lons, lats = warp_transform(source.crs, "EPSG:4326", [c[0] for c in corners], [c[1] for c in corners])
    ring = [[round(x, 6), round(y, 6)] for x, y in zip(lons, lats)]
    centre_x, centre_y = transform @ (size / 2, size / 2)
    (lon,), (lat,) = warp_transform(source.crs, "EPSG:4326", [centre_x], [centre_y])
    return {"type": "Polygon", "coordinates": [ring + [ring[0]]]}, round(lon, 6), round(lat, 6)


def read_chips(scene: dict) -> tuple[list[dict], dict]:
    """
    Cut a scene into chips. Returns (valid chips with rgb + metadata,
    {exclusion reason: count}).
    """
    chips, excluded = [], {}
    with rasterio.open(scene["file_path"]) as source:
        stack = source.read()
        for size, row, col in chip_windows(source.height, source.width):
            window = stack[:, row:row + size, col:col + size]
            fraction = valid_fraction(window)
            if fraction < 1 - MAX_INVALID_FRACTION:
                reason = f"more than {int(MAX_INVALID_FRACTION * 100)} % cloud/shadow/no-data (SCL)"
                excluded[reason] = excluded.get(reason, 0) + 1
                continue
            polygon, lon, lat = footprint(source, size, row, col)
            chips.append({"id": f"{scene['id']}:{size}:{row}:{col}", "chip_px": size, "row_off": row, "col_off": col,
                          "footprint": polygon, "lon": lon, "lat": lat, "valid_fraction": round(fraction, 4),
                          "rgb": to_rgb(window, scene["radiometric_offset"])})
    return chips, excluded


# ---------------------------------------------------------------------------
# Indexing (cached)
# ---------------------------------------------------------------------------

def index(aoi_id: str | None = None, model=None, actor: str = "system", log=print) -> dict:
    """
    Embed every eligible chip that is not cached yet for this model and
    preprocessing version. Returns counts, including scenes and chips that
    were excluded and why. `model` can be passed in (tests); otherwise the
    RemoteCLIP model is loaded.
    """
    # The model is loaded only if some chip actually needs embedding, so a
    # fully cached archive is re-checked in seconds.
    if model is None and model_problems():
        raise ModelNotStaged("; ".join(model_problems()))
    key = model.key if model else model_key()
    connection = db.connect()
    sql = "SELECT * FROM scenes" + (" WHERE aoi_id = ?" if aoi_id else "") + " ORDER BY acquired_at"
    scenes = [dict(r) for r in connection.execute(sql, (aoi_id,) if aoi_id else ())]
    cached = {row[0] for row in connection.execute(
        "SELECT id FROM semantic_chips WHERE model_key = ? AND preprocess_version = ?", (key, PREPROCESS_VERSION))}
    started = time.perf_counter()
    summary = {"model_key": key, "preprocess_version": PREPROCESS_VERSION, "scenes_indexed": 0, "scenes_excluded": 0,
               "chips_embedded": 0, "chips_cached": 0, "chips_excluded": 0, "excluded_scenes": []}
    prov_id = provenance.create(
        connection, kind="semantic-index", source_id=MODEL_ID, input_ref=f"scenes{'/' + aoi_id if aoi_id else ''}",
        input_sha256=None, processing="RemoteCLIP image embedding of Sentinel-2 true-colour chips",
        processing_version=PREPROCESS_VERSION,
        parameters={"model_key": key, "model": model_config()["name"], "chip_sizes_px": list(CHIP_SIZES_PX),
                    "max_invalid_fraction": MAX_INVALID_FRACTION, "stretch_max_reflectance": STRETCH_MAX},
    )
    for scene in scenes:
        problem = scene_problem(scene)
        if problem:
            summary["scenes_excluded"] += 1
            summary["excluded_scenes"].append({"scene_id": scene["id"], "reason": problem})
            _record_scene(connection, scene["id"], key, "excluded", problem, 0, 0, {})
            continue
        chips, excluded = read_chips(scene)
        new = [c for c in chips if c["id"] not in cached]
        if new and model is None:
            model = get_model()
        for start in range(0, len(new), BATCH):
            batch = new[start:start + BATCH]
            vectors = model.encode_images([c["rgb"] for c in batch])
            for chip, vector in zip(batch, vectors):
                connection.execute(
                    """INSERT OR REPLACE INTO semantic_chips (id, model_key, preprocess_version, scene_id, aoi_id, acquired_at,
                           chip_px, row_off, col_off, footprint, lon, lat, valid_fraction, file_sha256, embedding, provenance_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (chip["id"], key, PREPROCESS_VERSION, scene["id"], scene["aoi_id"], scene["acquired_at"], chip["chip_px"],
                     chip["row_off"], chip["col_off"], db.to_json(chip["footprint"]), chip["lon"], chip["lat"],
                     chip["valid_fraction"], scene.get("file_sha256"), np.asarray(vector, dtype="float32").tobytes(), prov_id, db.now_iso()))
        summary["chips_embedded"] += len(new)
        summary["chips_cached"] += len(chips) - len(new)
        summary["chips_excluded"] += sum(excluded.values())
        summary["scenes_indexed"] += 1
        status = "indexed" if chips else "excluded"
        _record_scene(connection, scene["id"], key, status, None if chips else "every chip was cloudy or had no data",
                      len(chips), sum(excluded.values()), excluded)
        connection.commit()
    summary["seconds"] = round(time.perf_counter() - started, 2)
    connection.commit()
    audit.record(connection, actor, "semantic-index", aoi_id or "all", {k: v for k, v in summary.items() if k != "excluded_scenes"})
    connection.close()
    log(f"semantic index: {summary['scenes_indexed']} scenes, {summary['chips_embedded']} embedded, "
        f"{summary['chips_cached']} cached, {summary['chips_excluded']} chips and {summary['scenes_excluded']} scenes excluded")
    return summary


def _record_scene(connection, scene_id, key, status, reason, chips_indexed, chips_excluded, reasons):
    connection.execute(
        """INSERT OR REPLACE INTO semantic_scenes (scene_id, model_key, preprocess_version, status, reason, chips_indexed,
               chips_excluded, exclusion_reasons, indexed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (scene_id, key, PREPROCESS_VERSION, status, reason, chips_indexed, chips_excluded, db.to_json(reasons), db.now_iso()))


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

SCORE_KIND = "cosine similarity (model similarity, not a probability or detection)"


def search(text: str, limit: int = 20, start: str | None = None, end: str | None = None,
           aoi_id: str | None = None, model=None) -> dict:
    """
    Rank cached chips by similarity to `text`. Returns results plus timing,
    the number of chips/scenes searched and the model version. Raises
    ModelNotStaged / ModelUnavailable when the model cannot be used; it
    never falls back to keyword or rule-based matching.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("empty query")
    t0 = time.perf_counter()
    model = model or get_model()
    t1 = time.perf_counter()
    query_vector = model.encode_text(text)
    t2 = time.perf_counter()
    connection = db.connect()
    sql = """SELECT c.*, s.sensor, s.cloud_fraction, s.quality_status, s.provenance_id AS scene_provenance_id
             FROM semantic_chips c JOIN scenes s ON s.id = c.scene_id
             WHERE c.model_key = ? AND c.preprocess_version = ?"""
    params = [model.key, PREPROCESS_VERSION]
    if start:
        sql += " AND c.acquired_at >= ?"
        params.append(start)
    if end:
        sql += " AND c.acquired_at <= ?"
        params.append(end)
    if aoi_id:
        sql += " AND c.aoi_id = ?"
        params.append(aoi_id)
    rows = connection.execute(sql, params).fetchall()
    connection.close()
    results = []
    if rows:
        matrix = np.frombuffer(b"".join(r["embedding"] for r in rows), dtype="float32").reshape(len(rows), -1)
        scores = matrix @ query_vector
        order = np.argsort(-scores)
        for rank, index in enumerate(order[:limit], start=1):
            row = rows[int(index)]
            polygon = json.loads(row["footprint"])
            ring = polygon["coordinates"][0]
            results.append({
                "kind": "chip", "id": row["id"], "query": text, "rank": rank, "score": round(float(scores[index]), 4), "score_kind": SCORE_KIND,
                "scene_id": row["scene_id"], "aoi_id": row["aoi_id"], "acquired_at": row["acquired_at"], "sensor": row["sensor"],
                "chip_px": row["chip_px"], "chip_km": row["chip_px"] * 10 / 1000, "footprint": polygon,
                "bbox": [min(p[0] for p in ring), min(p[1] for p in ring), max(p[0] for p in ring), max(p[1] for p in ring)],
                "lon": row["lon"], "lat": row["lat"], "valid_fraction": row["valid_fraction"],
                "provenance_id": row["provenance_id"], "scene_provenance_id": row["scene_provenance_id"],
            })
        distribution = {"median": round(float(np.median(scores)), 4), "max": round(float(scores.max()), 4),
                        "min": round(float(scores.min()), 4)}
    else:
        distribution = None
    t3 = time.perf_counter()
    return {
        "query": text, "model": getattr(model, "version", {"model_key": model.key}), "model_key": model.key,
        "score_kind": SCORE_KIND, "chips_searched": len(rows), "scenes_searched": len({r["scene_id"] for r in rows}),
        "score_distribution": distribution, "results": results,
        "timing_ms": {"model_load": round((t1 - t0) * 1000, 1), "text_encode": round((t2 - t1) * 1000, 1),
                      "rank": round((t3 - t2) * 1000, 1), "total": round((t3 - t0) * 1000, 1)},
        "note": "Scores rank chips by model similarity only. A high rank is not a detection and does not prove the described object is present.",
    }


# ---------------------------------------------------------------------------
# Image-to-image similarity ("find image chips that look like this one")
#
# Reuses the cached chip embeddings: the query is the embedding of one or
# more already-indexed chips, so no model inference runs at query time.
# The model must still be staged (same rule as text search), so a result
# is never shown for a model whose files are missing.
#
# Scopes (all chips of one AOI share the same pixel grid, so "the same
# place" is decided from pixel windows, not from coordinates):
#   other-places  chips that do NOT overlap an example chip by more than
#                 half (look-alike places elsewhere); best date per place
#   same-place    only chips at an example's window, on every date (how
#                 much each date looks like the example)
#   all           everything except the example chips themselves
# Only chips of the same size as the examples are compared: a 2.24 km and a
# 1.12 km chip show different fields of view, so their embeddings are not
# like for like.
# ---------------------------------------------------------------------------

SIMILAR_SCORE_KIND = "cosine similarity of image embeddings (model similarity, not a probability or a confirmed match)"
SIMILAR_SCOPES = ("other-places", "same-place", "all")
SAME_PLACE_OVERLAP = 0.5  # share of the smaller window: above this, two chips show the same place


def _require_model_files():
    """Refuse when the model is not staged or failed to load (never substitute anything)."""
    problems = model_problems()
    if problems:
        raise ModelNotStaged("; ".join(problems))
    if _model_error:
        raise ModelUnavailable(_model_error)


def _window(row) -> tuple[str, int, int, int]:
    return row["aoi_id"], row["row_off"], row["col_off"], row["chip_px"]


def window_overlap(a: tuple, b: tuple) -> float:
    """Overlap of two chip windows (aoi, row, col, size) as a share of the smaller one; 0 across AOIs."""
    if a[0] != b[0]:
        return 0.0
    rows = max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    cols = max(0, min(a[2] + a[3], b[2] + b[3]) - max(a[2], b[2]))
    return rows * cols / min(a[3], b[3]) ** 2


def _chip_rows(connection, where: str = "", params: tuple = ()) -> list:
    return connection.execute(
        f"""SELECT c.*, s.sensor, s.provenance_id AS scene_provenance_id
            FROM semantic_chips c JOIN scenes s ON s.id = c.scene_id
            WHERE c.model_key = ? AND c.preprocess_version = ? {where}""",
        (model_key(), PREPROCESS_VERSION, *params)).fetchall()


def _result(row, rank: int, score: float, score_kind: str, **extra) -> dict:
    """One ranked chip with its scene metadata, footprint and provenance."""
    polygon = json.loads(row["footprint"])
    ring = polygon["coordinates"][0]
    return {
        "kind": "chip", "id": row["id"], "rank": rank, "score": round(float(score), 4), "score_kind": score_kind,
        "scene_id": row["scene_id"], "aoi_id": row["aoi_id"], "acquired_at": row["acquired_at"], "sensor": row["sensor"],
        "chip_px": row["chip_px"], "chip_km": row["chip_px"] * 10 / 1000, "footprint": polygon,
        "bbox": [min(p[0] for p in ring), min(p[1] for p in ring), max(p[0] for p in ring), max(p[1] for p in ring)],
        "lon": row["lon"], "lat": row["lat"], "valid_fraction": row["valid_fraction"],
        "provenance_id": row["provenance_id"], "scene_provenance_id": row["scene_provenance_id"], **extra,
    }


def chips_at(lon: float, lat: float, scene_id: str | None = None) -> list[dict]:
    """
    Indexed chips whose footprint contains the point, from `scene_id` or
    (by default) the latest indexed scene there. Smallest chips first, then
    the chip whose centre is nearest the point.
    """
    connection = db.connect()
    rows = [r for r in _chip_rows(connection, *(("AND c.scene_id = ?", (scene_id,)) if scene_id else ("", ())))
            if geometry.point_in_geometry(lon, lat, json.loads(r["footprint"]))]
    connection.close()
    if not rows:
        return []
    if not scene_id:
        latest = max(r["acquired_at"] for r in rows)
        rows = [r for r in rows if r["acquired_at"] == latest]
    rows.sort(key=lambda r: (r["chip_px"], (r["lon"] - lon) ** 2 + (r["lat"] - lat) ** 2))
    return [_result(r, rank, 1.0, "location match (not a similarity score)") for rank, r in enumerate(rows, start=1)]


def similar(chip_ids: list[str], negative_ids: list[str] | None = None, scope: str = "other-places",
            limit: int = 20, start: str | None = None, end: str | None = None) -> dict:
    """
    Rank indexed chips by how close their RemoteCLIP image embedding is to
    the example chips (mean of the examples, minus half the mean of any
    "not like this" chips). Raises ModelNotStaged / ModelUnavailable /
    ValueError (unknown scope, or example chips not indexed).
    """
    _require_model_files()
    if scope not in SIMILAR_SCOPES:
        raise ValueError(f"scope must be one of {', '.join(SIMILAR_SCOPES)}")
    negative_ids = list(negative_ids or [])
    if not chip_ids:
        raise ValueError("at least one example chip is needed")
    t0 = time.perf_counter()
    connection = db.connect()
    marks = ",".join("?" * len(chip_ids + negative_ids))
    examples = {r["id"]: r for r in _chip_rows(connection, f"AND c.id IN ({marks})", tuple(chip_ids + negative_ids))}
    missing = [c for c in chip_ids + negative_ids if c not in examples]
    if missing:
        connection.close()
        raise ValueError(f"not indexed with the current model: {', '.join(missing)}")
    where, params = "", []
    if start:
        where += " AND c.acquired_at >= ?"
        params.append(start)
    if end:
        where += " AND c.acquired_at <= ?"
        params.append(end)
    rows = _chip_rows(connection, where, tuple(params))
    connection.close()

    def vector(row):
        return np.frombuffer(row["embedding"], dtype="float32")

    query = np.mean([vector(examples[c]) for c in chip_ids], axis=0)
    if negative_ids:
        query = query - 0.5 * np.mean([vector(examples[c]) for c in negative_ids], axis=0)
    query = query / (np.linalg.norm(query) or 1.0)

    example_windows = [_window(examples[c]) for c in chip_ids]
    sizes = {examples[c]["chip_px"] for c in chip_ids}
    skip = set(chip_ids) | set(negative_ids)
    candidates = []
    for row in rows:
        if row["id"] in skip or row["chip_px"] not in sizes:
            continue
        same_place = any(window_overlap(_window(row), w) > SAME_PLACE_OVERLAP for w in example_windows)
        if (scope == "other-places" and same_place) or (scope == "same-place" and not same_place):
            continue
        candidates.append((row, same_place))
    results = []
    if candidates:
        matrix = np.stack([vector(r) for r, _ in candidates])
        scores = matrix @ query
        seen_places = set()
        for index in np.argsort(-scores):
            row, same_place = candidates[int(index)]
            place = _window(row)
            # other-places / all: one result per place (its best-matching date),
            # so one look-alike site does not fill the list with its dates.
            if scope != "same-place":
                if place in seen_places:
                    continue
                seen_places.add(place)
            results.append(_result(row, len(results) + 1, scores[index], SIMILAR_SCORE_KIND, same_place=same_place))
            if len(results) >= limit:
                break
        distribution = {"median": round(float(np.median(scores)), 4), "max": round(float(scores.max()), 4),
                        "min": round(float(scores.min()), 4)}
    else:
        distribution = None
    return {
        "examples": [_result(examples[c], i + 1, 1.0, "example") for i, c in enumerate(chip_ids)],
        "negatives": [_result(examples[c], i + 1, 1.0, "example (not like this)") for i, c in enumerate(negative_ids)],
        "scope": scope, "model_key": model_key(), "model": {"model": model_config()["name"], "architecture": model_config()["weights"]["architecture"]},
        "score_kind": SIMILAR_SCORE_KIND, "chips_searched": len(candidates), "score_distribution": distribution,
        "results": results, "timing_ms": {"total": round((time.perf_counter() - t0) * 1000, 1), "model_load": 0.0},
        "note": "Ranked by similarity of RemoteCLIP image embeddings to the example. Looking alike is not evidence that "
                "the same objects or activity are present; check the images.",
    }


def chip_record(chip_id: str) -> dict | None:
    """One indexed chip with its scene metadata and provenance (for the detail panel)."""
    connection = db.connect()
    row = connection.execute(
        """SELECT c.id, c.scene_id, c.aoi_id, c.acquired_at, c.chip_px, c.row_off, c.col_off, c.footprint, c.lon, c.lat,
                  c.valid_fraction, c.file_sha256, c.model_key, c.preprocess_version, c.provenance_id, c.created_at,
                  s.sensor, s.processing_level, s.crs, s.cloud_fraction, s.quality_status, s.source_url, s.provenance_id AS scene_provenance_id
           FROM semantic_chips c JOIN scenes s ON s.id = c.scene_id WHERE c.id = ? ORDER BY c.created_at DESC LIMIT 1""",
        (chip_id,)).fetchone()
    if row is None:
        connection.close()
        return None
    record = dict(row)
    record["footprint"] = json.loads(record["footprint"])
    ring = record["footprint"]["coordinates"][0]
    record["bbox"] = [min(p[0] for p in ring), min(p[1] for p in ring), max(p[0] for p in ring), max(p[1] for p in ring)]
    record["provenance"] = provenance.get(connection, record["provenance_id"]) if record["provenance_id"] else None
    record["scene_provenance"] = provenance.get(connection, record["scene_provenance_id"]) if record["scene_provenance_id"] else None
    connection.close()
    return record


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def status() -> dict:
    """
    Retrieval status for the UI and the AI/ML registry:
      NOT STAGED   weights or packages missing, or nothing indexed yet
      INDEXING     an index job is queued or running
      PARTIAL      some eligible scenes are indexed, others not yet
      READY        every eligible scene has been processed with this model
      UNAVAILABLE  files present but the model failed to load (checksum, error)
    Cheap: does not load the model or hash the weights.
    """
    config = model_config()
    problems = model_problems()
    connection = db.connect()
    key = model_key()
    scenes = [dict(r) for r in connection.execute("SELECT id, quality_status FROM scenes")]
    eligible = [s for s in scenes if s["quality_status"] in ("usable", "degraded")]
    processed = {r["scene_id"]: dict(r) for r in connection.execute(
        "SELECT * FROM semantic_scenes WHERE model_key = ? AND preprocess_version = ?", (key, PREPROCESS_VERSION))}
    chips = connection.execute("SELECT COUNT(*), COUNT(DISTINCT scene_id) FROM semantic_chips WHERE model_key = ? AND preprocess_version = ?",
                               (key, PREPROCESS_VERSION)).fetchone()
    job = connection.execute("SELECT status FROM jobs WHERE kind = 'semantic-index' AND status IN ('queued', 'running') LIMIT 1").fetchone()
    connection.close()
    done = [s for s in eligible if s["id"] in processed]
    eligible_ids = {s["id"] for s in eligible}
    excluded = [{"scene_id": sid, "reason": p["reason"]} for sid, p in processed.items()
                if p["status"] == "excluded" and sid in eligible_ids]
    not_eligible = len(scenes) - len(eligible)
    chips_excluded = sum(p["chips_excluded"] for p in processed.values())
    if problems:
        state, reasons = "NOT STAGED", problems
    elif _model_error:
        state, reasons = "UNAVAILABLE", [_model_error]
    elif job:
        state, reasons = "INDEXING", [f"index job {job['status']}"]
    elif chips[0] == 0:
        state, reasons = "NOT STAGED", ["Model staged but the archive is not indexed yet (run INDEX ARCHIVE or `lumon semantic-index`)."]
    elif len(done) < len(eligible):
        state, reasons = "PARTIAL", [f"{len(eligible) - len(done)} eligible scenes not indexed yet"]
    else:
        state, reasons = "READY", []
    return {
        "state": state, "reasons": reasons, "model_id": MODEL_ID, "model_name": config["name"],
        "architecture": config["weights"]["architecture"], "weights_sha256": config["weights"]["sha256"],
        "framework": config["weights"]["framework"], "license": config.get("license"), "model_key": key,
        "preprocess_version": PREPROCESS_VERSION, "model_loaded": _model is not None,
        "scenes_total": len(scenes), "scenes_eligible": len(eligible), "scenes_processed": len(done),
        "scenes_indexed": chips[1], "scenes_excluded_by_quality": not_eligible, "scenes_excluded_by_indexer": excluded,
        "chips_indexed": chips[0], "chips_excluded": chips_excluded, "chip_sizes_px": list(CHIP_SIZES_PX),
        "max_invalid_fraction": MAX_INVALID_FRACTION, "score_kind": SCORE_KIND,
    }
