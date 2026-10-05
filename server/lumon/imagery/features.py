"""
Turn one staged scene into per-tile land-cover observations and features.

STEP BY STEP:
  1. Convert stored numbers to reflectance using the scene's processing
     baseline (see sentinel2.py - this is the radiometric harmonisation).
  2. Mark pixels as invalid if the Scene Classification Layer says cloud,
     shadow, cirrus, snow, saturated or no data.
  3. Compute three standard spectral indices per pixel:
        NDVI  = (NIR - Red)   / (NIR + Red)     vegetation greenness
        MNDWI = (Green - SWIR)/ (Green + SWIR)  open water
        NDBI  = (SWIR - NIR)  / (SWIR + NIR)    built-up / bare surfaces
  4. Label each valid pixel: water, vegetation, or open (neither).
  5. Summarise each 100 m x 100 m tile (10 x 10 pixels): share of valid
     pixels, share of each class, average indices, and texture.
  6. Give the tile one land class: water, vegetation, built or bare -
     or no class when fewer than 80 % of its pixels are clear.

HONEST LIMITS: these are simple index thresholds, not a trained model.
"Built" versus "bare" in particular is separated by a texture/brightness
heuristic that has NOT been validated against ground truth. Every change
reported from these classes is therefore labelled with an uncalibrated
score, and the indices behind it are shown to the analyst.
"""

import numpy as np
import rasterio

from . import sentinel2

TILE_PIXELS = 10  # 10 pixels x 10 m = 100 m tiles
MIN_VALID_FRACTION = 0.8  # tiles with less clear sky are not classified

# Pixel thresholds (documented starting points from common practice for
# Sentinel-2 indices; they are not tuned to this AOI).
WATER_MNDWI = 0.1  # MNDWI above this and NDVI below WATER_MAX_NDVI -> water
WATER_MAX_NDVI = 0.2
VEGETATION_NDVI = 0.35  # NDVI at or above this -> vegetation

# Tile thresholds.
CLASS_MAJORITY = 0.5  # a class must cover at least half of the valid pixels
# Built surfaces (roofs, pavement, airfield) are brighter and more varied
# within 100 m than ploughed or cleared soil. Tiles that are mostly "open"
# are called built when their visible brightness is high OR its spread
# between pixels is high; otherwise bare. Heuristic, unvalidated.
BUILT_MIN_BRIGHTNESS = 0.16
BUILT_MIN_TEXTURE = 0.035


def load_reflectance(path: str, offset: float) -> tuple[dict, np.ndarray]:
    """
    Read a staged scene file. Returns ({band: reflectance array}, valid mask).
    Reflectance = DN * 0.0001 + offset, where `offset` is the scene's
    radiometric offset decided and checked at ingest (scenes.radiometric_offset).
    """
    with rasterio.open(path) as source:
        stack = source.read()
    bands = {}
    for index, name in enumerate(sentinel2.REFLECTANCE_BANDS):
        dn = stack[index].astype("float32")
        bands[name] = dn * 0.0001 + offset
    scl = stack[-1]
    valid = ~np.isin(scl, list(sentinel2.SCL_INVALID))
    for name in sentinel2.REFLECTANCE_BANDS:
        valid &= stack[sentinel2.REFLECTANCE_BANDS.index(name)] > 0  # DN 0 = no data
    return bands, valid


def _ratio(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """(a - b) / (a + b), returning 0 where the denominator is 0."""
    total = a + b
    with np.errstate(divide="ignore", invalid="ignore"):
        result = np.where(total != 0, (a - b) / total, 0.0)
    return result.astype("float32")


def pixel_classes(bands: dict) -> dict:
    """Per-pixel indices and the three basic pixel classes."""
    ndvi = _ratio(bands["B08"], bands["B04"])
    mndwi = _ratio(bands["B03"], bands["B11"])
    ndbi = _ratio(bands["B11"], bands["B08"])
    brightness = (bands["B02"] + bands["B03"] + bands["B04"]) / 3
    water = (mndwi > WATER_MNDWI) & (ndvi < WATER_MAX_NDVI)
    vegetation = (ndvi >= VEGETATION_NDVI) & ~water
    open_land = ~water & ~vegetation
    return {"ndvi": ndvi, "mndwi": mndwi, "ndbi": ndbi, "brightness": brightness,
            "water": water, "vegetation": vegetation, "open": open_land}


def tile_class(valid_fraction: float, fractions: dict, open_brightness: float, open_texture: float) -> str | None:
    """
    Decide one land class for a tile from its pixel shares.
    Returns None when the tile is too cloudy to judge.
    """
    if valid_fraction < MIN_VALID_FRACTION:
        return None
    if fractions["water"] >= CLASS_MAJORITY:
        return "water"
    if fractions["vegetation"] >= CLASS_MAJORITY:
        return "vegetation"
    if fractions["open"] >= CLASS_MAJORITY:
        if open_brightness >= BUILT_MIN_BRIGHTNESS or open_texture >= BUILT_MIN_TEXTURE:
            return "built"
        return "bare"
    return "mixed"


def summarise_tiles(bands: dict, valid: np.ndarray) -> list[dict]:
    """
    Summarise the scene into tiles. Returns a list of dicts with keys
    row, col, valid_fraction, land_class, class_fractions, features.
    Partial tiles at the right/bottom edge are ignored.
    """
    classes = pixel_classes(bands)
    height, width = valid.shape
    rows, cols = height // TILE_PIXELS, width // TILE_PIXELS
    tiles = []
    for row in range(rows):
        for col in range(cols):
            window = (slice(row * TILE_PIXELS, (row + 1) * TILE_PIXELS), slice(col * TILE_PIXELS, (col + 1) * TILE_PIXELS))
            tile_valid = valid[window]
            valid_count = int(tile_valid.sum())
            valid_fraction = valid_count / tile_valid.size
            if valid_count == 0:
                tiles.append({"row": row, "col": col, "valid_fraction": 0.0, "land_class": None,
                              "class_fractions": None, "features": None})
                continue
            fractions = {name: float(classes[name][window][tile_valid].mean()) for name in ("water", "vegetation", "open")}
            open_pixels = classes["open"][window] & tile_valid
            brightness = classes["brightness"][window]
            open_brightness = float(brightness[open_pixels].mean()) if open_pixels.any() else 0.0
            open_texture = float(brightness[open_pixels].std()) if open_pixels.sum() > 1 else 0.0
            # The feature vector used for similarity search (see similarity.py).
            features = {
                **{f"{band}_mean": float(bands[band][window][tile_valid].mean()) for band in sentinel2.REFLECTANCE_BANDS},
                "ndvi_mean": float(classes["ndvi"][window][tile_valid].mean()),
                "mndwi_mean": float(classes["mndwi"][window][tile_valid].mean()),
                "ndbi_mean": float(classes["ndbi"][window][tile_valid].mean()),
                "brightness_std": float(brightness[tile_valid].std()),
                "open_brightness": open_brightness,
                "open_texture": open_texture,
            }
            tiles.append({
                "row": row, "col": col, "valid_fraction": round(valid_fraction, 3),
                "land_class": tile_class(valid_fraction, fractions, open_brightness, open_texture),
                "class_fractions": {k: round(v, 3) for k, v in fractions.items()},
                "features": {k: round(v, 4) for k, v in features.items()},
            })
    return tiles
