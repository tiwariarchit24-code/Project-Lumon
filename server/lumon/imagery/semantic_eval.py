"""
A small, documented evaluation of semantic image search.

There is no ground truth for the demo AOI, so NO accuracy number is
computed. Two things are reported instead:

1. A reproducible PROXY check for the queries where a simple spectral index
   gives an independent, checkable answer:
     water queries      -> share of chip pixels with MNDWI > 0  (water)
     vegetation queries -> share of chip pixels with NDVI > 0.4 (green vegetation)
   For each query it compares the mean of that share in the top-10 ranked
   chips with the bottom-10 and with the whole archive. A model that "gets"
   the query should rank chips with more water (or vegetation) higher. The
   indices are rules, not truth (turbid water and mangroves confuse them),
   so this shows AGREEMENT, not accuracy.

2. The ranked chip ids for every query in config/ai/semantic_eval.json, so
   the visual review recorded in docs/SEMANTIC_SEARCH_EVAL.md can be redone.
"""

import json

import numpy as np
import rasterio

from .. import db, settings
from . import semantic, sentinel2

TOP_N = 10


def _proxy_fraction(stack: np.ndarray, offset: float, proxy: str) -> float:
    """Share of valid pixels in a chip that the spectral rule calls water / vegetation."""
    band = {name: stack[i].astype("float32") * 0.0001 + offset for i, name in enumerate(sentinel2.REFLECTANCE_BANDS)}
    valid = ~np.isin(stack[-1], list(sentinel2.SCL_INVALID))
    with np.errstate(divide="ignore", invalid="ignore"):
        if proxy == "water":
            index = (band["B03"] - band["B11"]) / (band["B03"] + band["B11"])
            hit = index > 0
        else:
            index = (band["B08"] - band["B04"]) / (band["B08"] + band["B04"])
            hit = index > 0.4
    return float((hit & valid).sum() / max(valid.sum(), 1))


def load_queries() -> list[dict]:
    return json.loads((settings.CONFIG_DIR / "ai" / "semantic_eval.json").read_text())["queries"]


def run(model=None) -> dict:
    """Run every evaluation query; returns proxy statistics and top chip ids."""
    connection = db.connect()
    scenes = {r["id"]: dict(r) for r in connection.execute("SELECT id, file_path, radiometric_offset FROM scenes")}
    connection.close()
    cache: dict[str, float] = {}

    def fraction(chip_id: str, proxy: str) -> float:
        key = f"{proxy}|{chip_id}"
        if key not in cache:
            scene_id, size, row, col = chip_id.rsplit(":", 3)
            size, row, col = int(size), int(row), int(col)
            scene = scenes[scene_id]
            with rasterio.open(scene["file_path"]) as source:
                stack = source.read()[:, row:row + size, col:col + size]
            cache[key] = _proxy_fraction(stack, scene["radiometric_offset"], proxy)
        return cache[key]

    report = []
    for item in load_queries():
        ranked = semantic.search(item["query"], limit=100000, model=model)
        results = ranked["results"]
        entry = {"query": item["query"], "expect": item["expect"], "proxy": item.get("proxy"),
                 "chips": len(results), "score_top": results[0]["score"] if results else None,
                 "score_median": (ranked["score_distribution"] or {}).get("median"),
                 "top_ids": [r["id"] for r in results[:TOP_N]], "text_encode_ms": ranked["timing_ms"]["text_encode"]}
        if item.get("proxy") and results:
            proxy = item["proxy"]
            sample = results[::max(1, len(results) // 200)]  # every k-th chip, for the archive mean
            entry.update({
                "proxy_top_mean": round(float(np.mean([fraction(r["id"], proxy) for r in results[:TOP_N]])), 3),
                "proxy_bottom_mean": round(float(np.mean([fraction(r["id"], proxy) for r in results[-TOP_N:]])), 3),
                "proxy_archive_mean": round(float(np.mean([fraction(r["id"], proxy) for r in sample])), 3),
            })
            entry["proxy_agrees"] = entry["proxy_top_mean"] > entry["proxy_archive_mean"] > entry["proxy_bottom_mean"]
        report.append(entry)
    return {"model_key": semantic.model_key(), "preprocess_version": semantic.PREPROCESS_VERSION, "top_n": TOP_N, "queries": report}
