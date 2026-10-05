"""
Learned change detection: MODEL-GENERATED CANDIDATE CHANGES between two
Sentinel-2 scenes, using the BTC-B network with its OSCD checkpoint
(lumon/change_ml/btc_model.py).

This is separate from Lumon's rule-based change engine (lumon/change), which
stays the baseline. The two never replace each other: if this model is not
staged or fails, the status says NOT STAGED / UNAVAILABLE and nothing else
runs in its place. Outputs live in their own tables (ml_change_runs,
ml_change_regions), files (data/change_ml/<run>/) and map layer.

WHAT THE MODEL SAYS, AND WHAT IT DOES NOT
The network outputs, per 10 m pixel, a score in 0..1 that the pair differs in
the way OSCD's labelled URBAN changes differ (new buildings, roads). A score
above 0.5 (BTC's own evaluation threshold) marks a CANDIDATE change pixel.
It is not a verified change, not a probability calibrated for this archive,
and never by itself evidence that something was built or demolished.

PIPELINE FOR ONE PAIR (before, after)
  1. pair check (rule-based, nothing is resampled or invented):
       - both scenes usable/degraded, same AOI, files present and unchanged
         (SHA-256 vs ingest), radiometric offset known, expected bands
       - IDENTICAL pixel grid (CRS, geotransform, size); otherwise refused
       - before acquired earlier than after
       - measured misregistration (phase correlation of near-infrared edges)
         <= 1.0 px, else refused; > 0.5 px gives a warning
       - pixels clear in BOTH images (SCL) >= 50 %, else refused
       - season gap reported (vegetation and tides change with the season)
  2. colour: the same true-colour rendering as Lumon's evidence chips
     (B04/B03/B02, reflectance = DN x 0.0001 + offset, fixed 0-0.3 stretch)
  3. tiles: 96 x 96 px windows (OSCD's tile size), 64 px apart, covering the
     scene edge to edge; each resized to 256 x 256 (bilinear), ImageNet
     normalised, run through the model (before first), sigmoid, resized back
     to 96 x 96; overlapping scores are averaged
  4. outputs on the scenes' own grid: probability GeoTIFF (float32) and
     candidate-mask GeoTIFF (1 = candidate, 0 = no, 255 = not clear in both)
  5. regions: 8-connected candidate pixels -> polygons. Regions smaller than
     MIN_REGION_PX (9 px = 0.09 ha) are counted but not reported: below what
     10 m imagery resolves reliably.
  6. evidence per region (rule-based checks, labelled as such): spectral
     before/after (NDVI, MNDWI, brightness), flags for possible seasonal
     vegetation or water change, nearby cloud/no-data, thin shapes under
     misregistration, near-resolution size, persistence in later scenes,
     and overlap with the rule-based baseline (which is NOT ground truth).
"""

import importlib.util
import json
import math
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import rasterio
from rasterio import features
from rasterio.warp import transform_geom

from .. import audit, db, net, provenance, settings
from ..imagery import semantic, sentinel2

MODEL_ID = "btc-b-oscd96"
PREPROCESS_VERSION = "btc-s2rgb-refl0to0.3-tile96s64-bilinear256-imagenet-v1"
TILE_PX = 96
STRIDE_PX = 64
MODEL_SIZE = 256
THRESHOLD = 0.5
MIN_REGION_PX = 9            # 0.09 ha; smaller candidate regions are not reported
SMALL_REGION_PX = 25         # below 0.25 ha: flagged as near the resolution limit
MAX_SHIFT_PX = 1.0           # pairs misregistered by more than this are refused
WARN_SHIFT_PX = 0.5
MIN_JOINT_CLEAR = 0.5
BATCH = 8
IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)
LABEL = "MODEL-GENERATED CANDIDATE CHANGE"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ml_change_runs (
    id TEXT PRIMARY KEY,               -- mlrun:<before>:<after>:<model key>
    model_key TEXT NOT NULL,
    preprocess_version TEXT NOT NULL,
    aoi_id TEXT NOT NULL,
    before_scene_id TEXT NOT NULL,
    after_scene_id TEXT NOT NULL,
    before_date TEXT NOT NULL,
    after_date TEXT NOT NULL,
    checks TEXT NOT NULL,              -- JSON: pair check results and warnings
    parameters TEXT NOT NULL,          -- JSON: tile, stride, threshold, min region
    probability_path TEXT,
    probability_sha256 TEXT,
    mask_path TEXT,
    mask_sha256 TEXT,
    candidate_pixels INTEGER,
    clear_pixels INTEGER,
    regions_reported INTEGER,
    regions_too_small INTEGER,
    seconds REAL,
    provenance_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ml_change_regions (
    id TEXT PRIMARY KEY,               -- <run id>:<n>
    run_id TEXT NOT NULL,
    geometry TEXT NOT NULL,            -- GeoJSON polygon, WGS84
    lon REAL NOT NULL,
    lat REAL NOT NULL,
    area_m2 REAL NOT NULL,
    pixels INTEGER NOT NULL,
    mean_score REAL NOT NULL,
    max_score REAL NOT NULL,
    evidence TEXT NOT NULL,            -- JSON: spectral context, flags, persistence, baseline overlap
    review_status TEXT NOT NULL DEFAULT 'unreviewed',  -- unreviewed | rejected | plausible
    reviewed_by TEXT,
    reviewed_at TEXT,
    review_note TEXT
);
"""
REVIEW_STATES = ("unreviewed", "rejected", "plausible")


class ModelNotStaged(RuntimeError):
    """Weights, configuration or packages are missing."""


class ModelUnavailable(RuntimeError):
    """Files are present but the model cannot be used (checksum, load error)."""


class PairRejected(ValueError):
    """The two scenes cannot be compared honestly (reasons in .problems)."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


# ---------------------------------------------------------------------------
# Model files and loading
# ---------------------------------------------------------------------------

def model_config() -> dict:
    models = json.loads((settings.CONFIG_DIR / "ai" / "models.json").read_text())["models"]
    return next(m for m in models if m["id"] == MODEL_ID)


def model_key() -> str:
    return f"{MODEL_ID}:{model_config()['weights']['sha256'][:16]}"


def model_problems() -> list[str]:
    """Why the model cannot be used (empty = files and packages present)."""
    problems = []
    for package in ("torch", "transformers", "safetensors"):
        if importlib.util.find_spec(package) is None:
            problems.append(f"Python package '{package}' is not installed (see server/requirements-ml.txt)")
    weights = model_config()["weights"]
    path = settings.ROOT_DIR / weights["path"]
    if not path.exists():
        problems.append(f"Weights not staged: {weights['path']} (download {weights['url']})")
    elif path.stat().st_size != weights["size_bytes"]:
        problems.append(f"Weights file has the wrong size ({path.stat().st_size} bytes, expected {weights['size_bytes']})")
    if not (settings.ROOT_DIR / weights["backbone_config"]).exists():
        problems.append(f"Backbone architecture file missing: {weights['backbone_config']}")
    return problems


_model = None
_model_error: str | None = None
_model_lock = threading.Lock()


def get_model():
    """The loaded network (once per process), after checking the weights' SHA-256."""
    global _model, _model_error
    with _model_lock:
        if _model is None:
            problems = model_problems()
            if problems:
                raise ModelNotStaged("; ".join(problems))
            try:
                from .btc_model import load_btc
                weights = model_config()["weights"]
                path = settings.ROOT_DIR / weights["path"]
                checksum = net.sha256_of_bytes(path.read_bytes())
                if checksum != weights["sha256"]:
                    raise ModelUnavailable(f"Weights checksum mismatch ({checksum[:12]}…)")
                _model = load_btc(path, settings.ROOT_DIR / weights["backbone_config"])
                _model_error = None
            except Exception as error:
                _model_error = f"{type(error).__name__}: {error}"
                raise ModelUnavailable(_model_error) from error
        return _model


def predict_tiles(model, before: np.ndarray, after: np.ndarray) -> np.ndarray:
    """
    Change scores for a batch of uint8 RGB tile pairs (N, 96, 96, 3) ->
    (N, 96, 96) float32 in 0..1, exactly as BTC was evaluated: resize to
    256 (bilinear), /255, ImageNet normalisation, model, sigmoid; then resized
    back to the tile's own pixels.
    """
    import torch

    mean = torch.tensor(IMAGENET_MEAN).view(1, 3, 1, 1)
    std = torch.tensor(IMAGENET_STD).view(1, 3, 1, 1)

    def prepare(tiles):
        x = torch.from_numpy(tiles).permute(0, 3, 1, 2).float() / 255
        x = torch.nn.functional.interpolate(x, size=MODEL_SIZE, mode="bilinear", align_corners=False)
        return (x - mean) / std

    with torch.no_grad():
        scores = torch.sigmoid(model(prepare(before), prepare(after)))
        scores = torch.nn.functional.interpolate(scores, size=before.shape[1:3], mode="bilinear",
                                                 align_corners=False, antialias=True)
    return scores[:, 0].numpy().astype("float32")


# ---------------------------------------------------------------------------
# Pair checks
# ---------------------------------------------------------------------------

def tile_offsets(length: int) -> list[int]:
    """Tile start positions covering 0..length edge to edge, about STRIDE_PX apart."""
    if length <= TILE_PX:
        return [0]
    count = math.ceil((length - TILE_PX) / STRIDE_PX) + 1
    return sorted({int(round(v)) for v in np.linspace(0, length - TILE_PX, count)})


def estimate_shift(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """
    Sub-pixel (rows, cols) shift between two images by phase correlation of
    their edge strength, with a parabola fit around the peak. Used only to
    MEASURE misregistration; images are never resampled.
    """
    def edges(img):
        gy, gx = np.gradient(img.astype("float64"))
        e = np.hypot(gx, gy)
        return (e - e.mean()) * np.outer(np.hanning(e.shape[0]), np.hanning(e.shape[1]))

    spectrum = np.fft.fft2(edges(a)) * np.conj(np.fft.fft2(edges(b)))
    spectrum /= np.abs(spectrum) + 1e-12
    surface = np.fft.fftshift(np.real(np.fft.ifft2(spectrum)))
    y, x = np.unravel_index(surface.argmax(), surface.shape)

    def refine(minus, centre, plus):
        denominator = minus - 2 * centre + plus
        return 0.5 * (minus - plus) / denominator if denominator else 0.0

    h, w = surface.shape
    dy = y - h // 2 + refine(surface[(y - 1) % h, x], surface[y, x], surface[(y + 1) % h, x])
    dx = x - w // 2 + refine(surface[y, (x - 1) % w], surface[y, x], surface[y, (x + 1) % w])
    return float(dy), float(dx)


def _scene(connection, scene_id: str) -> dict:
    row = connection.execute("SELECT * FROM scenes WHERE id = ?", (scene_id,)).fetchone()
    if row is None:
        raise PairRejected([f"unknown scene {scene_id}"])
    return dict(row)


def _read(scene: dict) -> tuple[np.ndarray, dict]:
    with rasterio.open(scene["file_path"]) as source:
        return source.read(), {"crs": source.crs, "transform": source.transform, "height": source.height,
                               "width": source.width, "bands": list(source.descriptions)}


def _clear(stack: np.ndarray) -> np.ndarray:
    valid = ~np.isin(stack[-1], list(sentinel2.SCL_INVALID))
    return valid & (stack[: len(sentinel2.REFLECTANCE_BANDS)] > 0).all(axis=0)


def _month_gap(a: str, b: str) -> int:
    gap = abs(int(a[5:7]) - int(b[5:7]))
    return min(gap, 12 - gap)


def check_pair(before_id: str, after_id: str) -> dict:
    """
    Decide whether two scenes can be compared. Returns
    {"ok", "problems", "warnings", "shift_px", "joint_clear", "month_gap", "days_apart", ...}
    and, when ok, the loaded arrays under "_data" (not for JSON).
    """
    connection = db.connect()
    try:
        before, after = _scene(connection, before_id), _scene(connection, after_id)
    finally:
        connection.close()
    problems, warnings = [], []
    for scene in (before, after):
        problem = semantic.scene_problem(scene)  # quality, file, checksum, offset, bands
        if problem:
            problems.append(f"{scene['id']}: {problem}")
    if before["aoi_id"] != after["aoi_id"]:
        problems.append("scenes belong to different AOIs")
    if before["acquired_at"] >= after["acquired_at"]:
        problems.append("the 'before' scene must be acquired earlier than the 'after' scene")
    result = {"before_scene_id": before_id, "after_scene_id": after_id, "aoi_id": before["aoi_id"],
              "before_date": before["acquired_at"], "after_date": after["acquired_at"]}
    if problems:
        return {**result, "ok": False, "problems": problems, "warnings": warnings}

    stack_a, grid_a = _read(before)
    stack_b, grid_b = _read(after)
    if (grid_a["crs"], grid_a["transform"], grid_a["height"], grid_a["width"]) != \
            (grid_b["crs"], grid_b["transform"], grid_b["height"], grid_b["width"]):
        problems.append("the scenes are not on the same pixel grid (CRS, geotransform or size differ); "
                        "Lumon does not resample to force a comparison")
        return {**result, "ok": False, "problems": problems, "warnings": warnings}

    nir = sentinel2.REFLECTANCE_BANDS.index("B08")
    dy, dx = estimate_shift(stack_a[nir], stack_b[nir])
    shift = math.hypot(dy, dx)
    joint = _clear(stack_a) & _clear(stack_b)
    joint_clear = float(joint.mean())
    month_gap = _month_gap(before["acquired_at"], after["acquired_at"])
    days = (datetime.fromisoformat(after["acquired_at"][:10]) - datetime.fromisoformat(before["acquired_at"][:10])).days
    if shift > MAX_SHIFT_PX:
        problems.append(f"measured misregistration {shift:.2f} px exceeds {MAX_SHIFT_PX} px: edges would appear as change")
    elif shift > WARN_SHIFT_PX:
        warnings.append(f"measured misregistration {shift:.2f} px: thin candidates along edges may be misregistration")
    if joint_clear < MIN_JOINT_CLEAR:
        problems.append(f"only {joint_clear:.0%} of pixels are clear in both scenes (minimum {MIN_JOINT_CLEAR:.0%})")
    elif joint_clear < 0.95:
        warnings.append(f"{1 - joint_clear:.0%} of pixels are cloud/shadow/no-data in one scene: masked, not scored")
    if month_gap > 1:
        warnings.append(f"the scenes are {month_gap} months apart in the seasonal cycle: vegetation and tidal water "
                        "differences can appear as candidate change")
    result.update(ok=not problems, problems=problems, warnings=warnings, shift_px=round(shift, 3),
                  shift_rows_cols=[round(dy, 3), round(dx, 3)], joint_clear=round(joint_clear, 4),
                  month_gap=month_gap, days_apart=days, grid={"crs": str(grid_a["crs"]), "width": grid_a["width"],
                                                              "height": grid_a["height"], "transform": list(grid_a["transform"])[:6]})
    if not problems:
        result["_data"] = {"before": before, "after": after, "stack_a": stack_a, "stack_b": stack_b,
                           "joint": joint, "grid": grid_a}
    return result


def suggest_pairs(aoi_id: str | None = None, limit: int = 30) -> list[dict]:
    """
    Pairs most suitable for an honest comparison: different years, at most
    one calendar month apart in the season (controls vegetation and monsoon
    effects), both scenes >= 95 % clear and registered within 0.5 px of a
    common reference (approximation: each scene is measured once against
    the latest clear scene). Longest time span first.
    """
    connection = db.connect()
    sql = "SELECT * FROM scenes WHERE quality_status IN ('usable','degraded') AND file_path IS NOT NULL"
    scenes = [dict(r) for r in connection.execute(sql + (" AND aoi_id = ?" if aoi_id else "") + " ORDER BY acquired_at",
                                                    (aoi_id,) if aoi_id else ())]
    connection.close()
    measured = []
    for scene in scenes:
        if semantic.scene_problem(scene):
            continue
        stack, grid = _read(scene)
        measured.append({"scene": scene, "nir": stack[sentinel2.REFLECTANCE_BANDS.index("B08")],
                         "clear": float(_clear(stack).mean()), "grid": (str(grid["crs"]), tuple(grid["transform"]))})
    clear = [m for m in measured if m["clear"] >= 0.95]
    if not clear:
        return []
    reference = clear[-1]
    for m in clear:
        m["shift"] = math.hypot(*estimate_shift(reference["nir"], m["nir"]))
    pairs = []
    for i, a in enumerate(clear):
        for b in clear[i + 1:]:
            sa, sb = a["scene"], b["scene"]
            if sa["aoi_id"] != sb["aoi_id"] or a["grid"] != b["grid"] or sa["acquired_at"][:4] == sb["acquired_at"][:4]:
                continue
            if _month_gap(sa["acquired_at"], sb["acquired_at"]) > 1 or a["shift"] > 0.5 or b["shift"] > 0.5:
                continue
            pairs.append({"before_scene_id": sa["id"], "after_scene_id": sb["id"], "aoi_id": sa["aoi_id"],
                          "before_date": sa["acquired_at"], "after_date": sb["acquired_at"],
                          "years_apart": int(sb["acquired_at"][:4]) - int(sa["acquired_at"][:4])})
    pairs.sort(key=lambda p: (-p["years_apart"], p["before_date"]))
    return pairs[:limit]


# ---------------------------------------------------------------------------
# Inference for one pair
# ---------------------------------------------------------------------------

def _spectral(stack: np.ndarray, offset: float, mask: np.ndarray) -> dict:
    """Mean NDVI, MNDWI and visible brightness inside a mask (reflectance)."""
    bands = {name: stack[i].astype("float32") * 0.0001 + offset for i, name in enumerate(sentinel2.REFLECTANCE_BANDS)}
    with np.errstate(divide="ignore", invalid="ignore"):
        ndvi = (bands["B08"] - bands["B04"]) / (bands["B08"] + bands["B04"])
        mndwi = (bands["B03"] - bands["B11"]) / (bands["B03"] + bands["B11"])
    brightness = (bands["B02"] + bands["B03"] + bands["B04"]) / 3
    pick = lambda array: round(float(np.nanmean(array[mask])), 4)
    return {"ndvi": pick(ndvi), "mndwi": pick(mndwi), "brightness": pick(brightness)}


def _signature(stack: np.ndarray, offset: float, mask: np.ndarray) -> np.ndarray:
    return np.array([float((stack[i][mask].astype("float32") * 0.0001 + offset).mean())
                     for i in range(len(sentinel2.REFLECTANCE_BANDS))])


def _polygon_area(ring: list) -> float:
    return 0.5 * abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:])))


def _baseline_index(connection, aoi_id: str, grid: dict) -> tuple[np.ndarray, dict]:
    """Rule-based change candidates of the AOI rasterised onto the scene grid (0 = none)."""
    rows = [dict(r) for r in connection.execute(
        "SELECT id, status, change_class, last_clean_before, earliest_supported_after, geometry FROM change_candidates WHERE aoi_id = ?",
        (aoi_id,))]
    shapes, info = [], {}
    for number, row in enumerate(rows, start=1):
        geometry = json.loads(row["geometry"]) if row["geometry"] else None
        if not geometry:
            continue
        shapes.append((transform_geom("EPSG:4326", grid["crs"], geometry), number))
        info[number] = {k: row[k] for k in ("id", "status", "change_class", "last_clean_before", "earliest_supported_after")}
    if not shapes:
        return np.zeros((grid["height"], grid["width"]), dtype="int32"), info
    raster = features.rasterize(shapes, out_shape=(grid["height"], grid["width"]), transform=grid["transform"], fill=0, dtype="int32")
    return raster, info


def run_pair(before_id: str, after_id: str, model=None, actor: str = "system", force: bool = False) -> dict:
    """
    Run the model on one pair and store the run, its GeoTIFFs and its
    candidate regions. Returns the run record. Raises PairRejected,
    ModelNotStaged or ModelUnavailable; never falls back to the rule-based engine.
    """
    if model is None and model_problems():
        raise ModelNotStaged("; ".join(model_problems()))
    key = getattr(model, "key", None) or model_key()
    run_id = f"mlrun:{before_id}:{after_id}:{key}"
    connection = db.connect()
    connection.executescript(SCHEMA)
    existing = connection.execute("SELECT id FROM ml_change_runs WHERE id = ? AND preprocess_version = ?",
                                  (run_id, PREPROCESS_VERSION)).fetchone()
    connection.close()
    if existing and not force:
        return get_run(run_id)

    check = check_pair(before_id, after_id)
    if not check["ok"]:
        raise PairRejected(check["problems"])
    data = check.pop("_data")
    model = model or get_model()
    started = time.perf_counter()

    # 1-3. colour, tiles, model, averaged scores on the native grid
    before, after, grid, joint = data["before"], data["after"], data["grid"], data["joint"]
    rgb_a = semantic.to_rgb(data["stack_a"], before["radiometric_offset"])
    rgb_b = semantic.to_rgb(data["stack_b"], after["radiometric_offset"])
    height, width = joint.shape
    total = np.zeros((height, width), dtype="float32")
    counts = np.zeros((height, width), dtype="float32")
    windows = [(r, c) for r in tile_offsets(height) for c in tile_offsets(width)]
    for start in range(0, len(windows), BATCH):
        batch = windows[start:start + BATCH]
        tiles_a = np.stack([rgb_a[r:r + TILE_PX, c:c + TILE_PX] for r, c in batch])
        tiles_b = np.stack([rgb_b[r:r + TILE_PX, c:c + TILE_PX] for r, c in batch])
        scores = predict_tiles(model, tiles_a, tiles_b)
        for (r, c), score in zip(batch, scores):
            total[r:r + TILE_PX, c:c + TILE_PX] += score
            counts[r:r + TILE_PX, c:c + TILE_PX] += 1
    probability = total / np.maximum(counts, 1)
    probability[~joint] = np.nan
    candidates = (probability > THRESHOLD) & joint

    # 4. georeferenced outputs on the scenes' own grid
    folder = settings.DATA_DIR / "change_ml" / f"{before_id}__{after_id}"
    folder.mkdir(parents=True, exist_ok=True)
    profile = {"driver": "GTiff", "width": width, "height": height, "count": 1, "crs": grid["crs"],
               "transform": grid["transform"], "compress": "deflate"}
    tags = {"label": LABEL, "model_key": key, "preprocess_version": PREPROCESS_VERSION, "before_scene_id": before_id,
            "after_scene_id": after_id, "before_date": before["acquired_at"], "after_date": after["acquired_at"],
            "threshold": str(THRESHOLD)}
    probability_path, mask_path = folder / "probability.tif", folder / "candidates.tif"
    with rasterio.open(probability_path, "w", dtype="float32", nodata=float("nan"), **profile) as target:
        target.write(probability, 1)
        target.update_tags(**tags, meaning="model change score 0..1 (not a calibrated probability); NaN = not clear in both scenes")
    mask_raster = np.where(joint, candidates.astype("uint8"), 255).astype("uint8")
    with rasterio.open(mask_path, "w", dtype="uint8", nodata=255, **profile) as target:
        target.write(mask_raster, 1)
        target.update_tags(**tags, meaning="1 = model-generated candidate change, 0 = not flagged, 255 = not clear in both scenes")

    # 5-6. regions with evidence
    connection = db.connect()
    connection.executescript(SCHEMA)
    baseline, baseline_info = _baseline_index(connection, before["aoi_id"], grid)
    later = [dict(r) for r in connection.execute(
        """SELECT * FROM scenes WHERE aoi_id = ? AND quality_status IN ('usable','degraded') AND acquired_at > ?
           ORDER BY acquired_at""", (before["aoi_id"], after["acquired_at"]))]
    later = [s for s in later if _month_gap(s["acquired_at"], after["acquired_at"]) <= 1][:3]
    later_stacks = [(s, _read(s)[0]) for s in later if not semantic.scene_problem(s)]
    invalid_near = ~joint
    dilated = invalid_near.copy()
    for shift_r, shift_c in ((1, 0), (-1, 0), (0, 1), (0, -1), (2, 0), (-2, 0), (0, 2), (0, -2)):
        dilated |= np.roll(np.roll(invalid_near, shift_r, axis=0), shift_c, axis=1)

    regions, too_small = [], 0
    for geometry, value in features.shapes(candidates.astype("uint8"), mask=candidates, connectivity=8, transform=grid["transform"]):
        ring = geometry["coordinates"][0]
        area = _polygon_area(ring) - sum(_polygon_area(hole) for hole in geometry["coordinates"][1:])
        pixels = int(round(area / 100.0))
        if pixels < MIN_REGION_PX:
            too_small += 1
            continue
        inside = features.geometry_mask([geometry], out_shape=(height, width), transform=grid["transform"], invert=True)
        scores = probability[inside]
        spectral_before = _spectral(data["stack_a"], before["radiometric_offset"], inside)
        spectral_after = _spectral(data["stack_b"], after["radiometric_offset"], inside)
        flags = []
        if check["month_gap"] > 1 and abs(spectral_after["ndvi"] - spectral_before["ndvi"]) > 0.2:
            flags.append("possible seasonal vegetation change (NDVI shift across seasons)")
        if (spectral_before["mndwi"] > 0) != (spectral_after["mndwi"] > 0):
            flags.append("water appears or disappears (MNDWI crosses 0): tide, flooding or seasonal water possible")
        if (dilated & inside).any():
            flags.append("touches cloud/shadow/no-data within 2 px: possible mask-edge artefact")
        perimeter = sum(math.dist(p, q) for p, q in zip(ring, ring[1:]))
        if check["shift_px"] > WARN_SHIFT_PX and area / max(perimeter, 1) < 10:
            flags.append("thin shape in a pair misregistered by more than 0.5 px: possible edge artefact")
        if pixels < SMALL_REGION_PX:
            flags.append("near Sentinel-2's resolution limit (< 0.25 ha)")
        # Persistence: do later scenes (same season) look more like 'after' than 'before' here?
        sig_a = _signature(data["stack_a"], before["radiometric_offset"], inside)
        sig_b = _signature(data["stack_b"], after["radiometric_offset"], inside)
        persistence = []
        for scene, stack in later_stacks:
            clear_here = inside & _clear(stack)
            if clear_here.sum() < max(1, pixels // 2):
                continue
            sig = _signature(stack, scene["radiometric_offset"], clear_here)
            persistence.append({"scene_id": scene["id"], "date": scene["acquired_at"][:10],
                                "closer_to": "after" if np.linalg.norm(sig - sig_b) < np.linalg.norm(sig - sig_a) else "before"})
        if persistence and all(p["closer_to"] == "before" for p in persistence):
            flags.append("not persistent: later same-season scenes look like the 'before' image again")
        overlapping = sorted({int(v) for v in baseline[inside] if v})
        baseline_overlap = []
        for number in overlapping:
            item = baseline_info[number]
            window_overlaps = bool(item["earliest_supported_after"] and item["last_clean_before"]
                                   and item["last_clean_before"][:10] <= after["acquired_at"][:10]
                                   and item["earliest_supported_after"][:10] >= before["acquired_at"][:10])
            baseline_overlap.append({**item, "pixels_shared": int((baseline[inside] == number).sum()),
                                     "change_window_overlaps_pair": window_overlaps})
        wgs = transform_geom(grid["crs"], "EPSG:4326", geometry)
        lons = [p[0] for p in wgs["coordinates"][0]]
        lats = [p[1] for p in wgs["coordinates"][0]]
        regions.append({
            "geometry": wgs, "lon": round(sum(lons) / len(lons), 6), "lat": round(sum(lats) / len(lats), 6),
            "area_m2": round(area, 1), "pixels": pixels, "mean_score": round(float(np.nanmean(scores)), 4),
            "max_score": round(float(np.nanmax(scores)), 4),
            "evidence": {"label": LABEL, "spectral_before": spectral_before, "spectral_after": spectral_after,
                         "flags": flags, "flags_note": "Rule-based checks on the model output; they do not remove regions.",
                         "persistence": persistence, "baseline_overlap": baseline_overlap,
                         "baseline_note": "The rule-based engine is a separate baseline, not ground truth."},
        })
    regions.sort(key=lambda r: -r["mean_score"] * r["pixels"])

    seconds = round(time.perf_counter() - started, 2)
    parameters = {"tile_px": TILE_PX, "stride_px": STRIDE_PX, "model_input_px": MODEL_SIZE, "threshold": THRESHOLD,
                  "min_region_px": MIN_REGION_PX, "colour": "B04/B03/B02, reflectance 0-0.3 -> 0-255 (fixed)",
                  "normalisation": "ImageNet mean/std", "tiles": len(windows), "order": "before, after"}
    probability_sha = net.sha256_of_bytes(probability_path.read_bytes())
    mask_sha = net.sha256_of_bytes(mask_path.read_bytes())
    provenance_id = provenance.create(
        connection, kind="ml-change-run", source_id=MODEL_ID, input_ref=f"{before_id} -> {after_id}",
        input_sha256=f"{before.get('file_sha256')}+{after.get('file_sha256')}",
        processing="BTC-B (OSCD checkpoint) bi-temporal change inference on Sentinel-2 true colour",
        processing_version=PREPROCESS_VERSION, parameters={**parameters, "model_key": key, "checks": check},
    )
    connection.execute("DELETE FROM ml_change_regions WHERE run_id = ?", (run_id,))
    connection.execute(
        """INSERT OR REPLACE INTO ml_change_runs (id, model_key, preprocess_version, aoi_id, before_scene_id, after_scene_id,
               before_date, after_date, checks, parameters, probability_path, probability_sha256, mask_path, mask_sha256,
               candidate_pixels, clear_pixels, regions_reported, regions_too_small, seconds, provenance_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, key, PREPROCESS_VERSION, before["aoi_id"], before_id, after_id, before["acquired_at"], after["acquired_at"],
         db.to_json(check), db.to_json(parameters), str(probability_path.relative_to(settings.ROOT_DIR)) if probability_path.is_relative_to(settings.ROOT_DIR) else str(probability_path),
         probability_sha, str(mask_path.relative_to(settings.ROOT_DIR)) if mask_path.is_relative_to(settings.ROOT_DIR) else str(mask_path),
         mask_sha, int(candidates.sum()), int(joint.sum()), len(regions), too_small, seconds, provenance_id, db.now_iso()))
    for number, region in enumerate(regions, start=1):
        connection.execute(
            """INSERT INTO ml_change_regions (id, run_id, geometry, lon, lat, area_m2, pixels, mean_score, max_score, evidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (f"{run_id}:{number}", run_id, db.to_json(region["geometry"]), region["lon"], region["lat"], region["area_m2"],
             region["pixels"], region["mean_score"], region["max_score"], db.to_json(region["evidence"])))
    connection.commit()
    audit.record(connection, actor, "ml-change-run", run_id, {"regions": len(regions), "too_small": too_small,
                                                               "candidate_pixels": int(candidates.sum()), "seconds": seconds})
    connection.close()
    return get_run(run_id)


# ---------------------------------------------------------------------------
# Reading, review, status
# ---------------------------------------------------------------------------

def _path(stored: str | None) -> Path | None:
    if not stored:
        return None
    path = Path(stored)
    return path if path.is_absolute() else settings.ROOT_DIR / path


def get_run(run_id: str) -> dict | None:
    connection = db.connect()
    connection.executescript(SCHEMA)
    row = connection.execute("SELECT * FROM ml_change_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        connection.close()
        return None
    run = db.row_to_dict(row, ["checks", "parameters"])
    run["label"] = LABEL
    run["review_counts"] = {r[0]: r[1] for r in connection.execute(
        "SELECT review_status, COUNT(*) FROM ml_change_regions WHERE run_id = ? GROUP BY review_status", (run_id,))}
    run["provenance"] = provenance.get(connection, run["provenance_id"]) if run["provenance_id"] else None
    connection.close()
    return run


def list_runs(aoi_id: str | None = None) -> list[dict]:
    connection = db.connect()
    connection.executescript(SCHEMA)
    rows = connection.execute("SELECT id FROM ml_change_runs" + (" WHERE aoi_id = ?" if aoi_id else "") + " ORDER BY created_at DESC, rowid DESC",
                              (aoi_id,) if aoi_id else ()).fetchall()
    connection.close()
    return [get_run(r[0]) for r in rows]


def regions_geojson(run_id: str | None = None, include_rejected: bool = True, all_runs: bool = False) -> dict:
    """
    Candidate regions as GeoJSON, each labelled as a model candidate. One run
    at a time (the given one, or the latest), because regions of different
    date pairs mean different things; all_runs=True returns every run.
    """
    connection = db.connect()
    connection.executescript(SCHEMA)
    if run_id is None and not all_runs:
        latest = connection.execute("SELECT id FROM ml_change_runs ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
        run_id = latest[0] if latest else "none"
    sql = """SELECT g.*, r.before_date, r.after_date, r.before_scene_id, r.after_scene_id FROM ml_change_regions g
             JOIN ml_change_runs r ON r.id = g.run_id WHERE 1 = 1"""
    params = []
    if run_id:
        sql += " AND g.run_id = ?"
        params.append(run_id)
    if not include_rejected:
        sql += " AND g.review_status != 'rejected'"
    rows = connection.execute(sql, params).fetchall()
    connection.close()
    return {"type": "FeatureCollection", "run_id": None if all_runs else run_id, "features": [{
        "type": "Feature", "geometry": json.loads(r["geometry"]),
        "properties": {"id": r["id"], "run_id": r["run_id"], "label": LABEL, "mean_score": r["mean_score"],
                       "pixels": r["pixels"], "review_status": r["review_status"],
                       "title": f"{LABEL} · {r['before_date'][:10]} → {r['after_date'][:10]} · score {r['mean_score']:.2f}"},
    } for r in rows]}


def get_region(region_id: str) -> dict | None:
    connection = db.connect()
    connection.executescript(SCHEMA)
    row = connection.execute("SELECT * FROM ml_change_regions WHERE id = ?", (region_id,)).fetchone()
    connection.close()
    if row is None:
        return None
    region = db.row_to_dict(row, ["geometry", "evidence"])
    region["label"] = LABEL
    region["run"] = get_run(region["run_id"])
    ring = region["geometry"]["coordinates"][0]
    region["bbox"] = [min(p[0] for p in ring), min(p[1] for p in ring), max(p[0] for p in ring), max(p[1] for p in ring)]
    return region


def review_region(region_id: str, decision: str, analyst: str, note: str | None = None) -> dict:
    """Analyst review of one candidate region: rejected / plausible / back to unreviewed. Audited."""
    if decision not in REVIEW_STATES:
        raise ValueError(f"decision must be one of {', '.join(REVIEW_STATES)}")
    if not (analyst or "").strip():
        raise ValueError("analyst name is required")
    connection = db.connect()
    connection.executescript(SCHEMA)
    if connection.execute("SELECT 1 FROM ml_change_regions WHERE id = ?", (region_id,)).fetchone() is None:
        connection.close()
        raise KeyError(region_id)
    connection.execute("UPDATE ml_change_regions SET review_status = ?, reviewed_by = ?, reviewed_at = ?, review_note = ? WHERE id = ?",
                       (decision, analyst.strip(), db.now_iso(), note, region_id))
    connection.commit()
    audit.record(connection, analyst.strip(), "ml-change-review", region_id, {"decision": decision, "note": note})
    connection.close()
    return get_region(region_id)


def probability_png(run_id: str, bbox: list[float] | None = None, upscale: int = 1) -> bytes | None:
    """
    The run's score map as an RGB PNG: black where not a candidate (or not
    clear), magenta ramp above the threshold. `bbox` (lon/lat) cuts the same
    window as the before/after quicklooks, so the three line up.
    """
    from rasterio.warp import transform as warp_transform
    from rasterio.windows import from_bounds

    from ..imagery.quicklook import _png_bytes

    run = get_run(run_id)
    if run is None or not _path(run["probability_path"]) or not _path(run["probability_path"]).exists():
        return None
    with rasterio.open(_path(run["probability_path"])) as source:
        window = None
        if bbox:
            xs, ys = warp_transform("EPSG:4326", source.crs, [bbox[0], bbox[2]], [bbox[1], bbox[3]])
            window = from_bounds(min(xs), min(ys), max(xs), max(ys), transform=source.transform).round_offsets().round_lengths()
        score = source.read(1, window=window, boundless=True, fill_value=np.nan)
    rgb = np.zeros(score.shape + (3,), dtype="uint8")
    above = np.nan_to_num(score) > THRESHOLD
    strength = np.clip((np.nan_to_num(score) - THRESHOLD) / (1 - THRESHOLD), 0, 1)
    rgb[..., 0] = np.where(above, 120 + 135 * strength, 0).astype("uint8")
    rgb[..., 1] = np.where(above, 20, 0).astype("uint8")
    rgb[..., 2] = np.where(above, 120 + 100 * strength, 0).astype("uint8")
    if upscale > 1:
        rgb = rgb.repeat(upscale, axis=0).repeat(upscale, axis=1)
    return _png_bytes(rgb)  # shown next to the before/after images, not as a map overlay


def status() -> dict:
    """
    NOT STAGED   weights, config or packages missing
    UNAVAILABLE  files present, but loading failed (checksum, mismatch)
    INDEXING     a change run is queued or running
    READY        model staged; runs on demand for a chosen pair
    (PARTIAL is not used by this capability.)
    """
    config = model_config()
    problems = model_problems()
    connection = db.connect()
    connection.executescript(SCHEMA)
    job = connection.execute("SELECT status FROM jobs WHERE kind = 'ml-change-run' AND status IN ('queued','running') LIMIT 1").fetchone()
    runs = connection.execute("SELECT COUNT(*) FROM ml_change_runs").fetchone()[0]
    regions = {r[0]: r[1] for r in connection.execute("SELECT review_status, COUNT(*) FROM ml_change_regions GROUP BY review_status")}
    connection.close()
    if problems:
        state, reasons = "NOT STAGED", problems
    elif _model_error:
        state, reasons = "UNAVAILABLE", [_model_error]
    elif job:
        state, reasons = "INDEXING", [f"change run {job['status']}"]
    else:
        state, reasons = "READY", []
    return {"state": state, "reasons": reasons, "label": LABEL, "model_id": MODEL_ID, "model_name": config["name"],
            "model_key": model_key(), "license": config.get("license"), "verified_here": config.get("verified_here"),
            "training_data": config.get("training_data"), "preprocess_version": PREPROCESS_VERSION,
            "model_loaded": _model is not None, "runs": runs, "regions": regions,
            "parameters": {"tile_px": TILE_PX, "stride_px": STRIDE_PX, "threshold": THRESHOLD, "min_region_px": MIN_REGION_PX,
                           "max_shift_px": MAX_SHIFT_PX, "min_joint_clear": MIN_JOINT_CLEAR}}
