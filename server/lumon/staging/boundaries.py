"""
Stage the India boundary and reference-geography datasets.

For each dataset listed in config/geo/boundary-datasets.json this:
  1. downloads the file (CONNECTED mode only) into data/raw/ untouched
  2. records its SHA-256 checksum and retrieval time
  3. keeps only the India features (if the dataset is global)
  4. keeps only a few useful properties, renamed to simple names
  5. simplifies the shapes and rounds coordinates to keep files small
  6. writes data/boundaries/<id>.geojson
  7. writes a provenance record and data/boundaries/manifest.json

If a download fails, the previously staged copy (if any) is left in place.
"""

import json

from .. import audit, db, net, provenance, settings
from ..geo import geometry, shapefile_zip

PROCESSING_VERSION = "boundaries-v1"


def load_dataset_definitions() -> list[dict]:
    """Read the list of boundary datasets from the config file."""
    path = settings.CONFIG_DIR / "geo" / "boundary-datasets.json"
    return json.loads(path.read_text())["datasets"]


def _features_from_shapefile_zip(data: bytes, dataset: dict) -> list[dict]:
    """
    Read a zipped shapefile and keep only records matching dataset["filter"]
    (e.g. ADM0_A3 == "IND"), so a global file becomes an India-only file.
    """
    rule = dataset.get("filter")
    if not rule:
        return shapefile_zip.read_features(data)
    return shapefile_zip.read_features(data, keep=lambda properties: properties.get(rule["field"]) == rule["equals"])


def _clean_features(features: list[dict], dataset: dict) -> list[dict]:
    """
    Keep only the configured properties (renamed), simplify geometries and
    drop features whose geometry is empty after simplification.
    """
    rename = dataset.get("keep_properties", {})
    tolerance = dataset.get("simplify_tolerance_deg", 0)
    cleaned = []
    for feature in features:
        shape = feature.get("geometry")
        if not shape:
            continue
        if tolerance > 0:
            simplified = geometry.simplify_geometry(shape, tolerance)
            # Very small areas can collapse to nothing when simplified. We
            # never drop a real unit for that reason: keep it at full detail.
            shape = simplified if simplified is not None else geometry.simplify_geometry(shape, 0)
        properties = {new_name: feature["properties"].get(old_name) for old_name, new_name in rename.items()}
        cleaned.append({"type": "Feature", "properties": properties, "geometry": shape})
    return cleaned


def stage_dataset(connection, dataset: dict) -> dict:
    """
    Download and process one dataset. Returns a manifest entry describing
    exactly what was staged (or the error, if it failed).
    """
    retrieved_at = db.now_iso()
    try:
        data = net.fetch_bytes(dataset["url"], timeout=180)
    except Exception as error:  # network/HTTP/offline errors are reported, not fatal
        return {"id": dataset["id"], "status": "failed", "error": str(error), "attempted_at": retrieved_at}

    checksum = net.sha256_of_bytes(data)
    extension = "zip" if dataset["format"] == "shapefile-zip" else "geojson"
    raw_path = settings.RAW_DIR / f"{dataset['id']}.{extension}"
    raw_path.write_bytes(data)  # the untouched original download

    version_note = None
    if dataset["format"] == "shapefile-zip":
        features = _features_from_shapefile_zip(data, dataset)
        version_note = shapefile_zip.version_note(data)
    else:
        features = json.loads(data)["features"]

    features = _clean_features(features, dataset)
    if not features:
        return {"id": dataset["id"], "status": "failed", "error": "no features left after filtering", "attempted_at": retrieved_at}

    output = {"type": "FeatureCollection", "features": features}
    output_path = settings.BOUNDARY_DIR / f"{dataset['id']}.geojson"
    output_path.write_text(json.dumps(output, separators=(",", ":")))

    provenance_id = provenance.create(
        connection,
        kind="boundary",
        source_id=dataset["id"],
        input_ref=dataset["url"],
        input_sha256=checksum,
        processing="filter to India, keep selected properties, Douglas-Peucker simplify, round to 4 decimals",
        processing_version=PROCESSING_VERSION,
        parameters={"simplify_tolerance_deg": dataset.get("simplify_tolerance_deg", 0), "filter": dataset.get("filter")},
        source_version=version_note,
        retrieved_at=retrieved_at,
    )
    connection.commit()

    return {
        "id": dataset["id"],
        "status": "staged",
        "name": dataset["name"],
        "provider": dataset["provider"],
        "dataset": dataset["dataset"],
        "source_url": dataset["url"],
        "source_version": version_note,
        "license": dataset["license"],
        "attribution": dataset["attribution"],
        "retrieved_at": retrieved_at,
        "raw_sha256": checksum,
        "raw_bytes": len(data),
        "output_file": str(output_path.relative_to(settings.ROOT_DIR)),
        "output_bytes": output_path.stat().st_size,
        "feature_count": len(features),
        "processing_version": PROCESSING_VERSION,
        "provenance_id": provenance_id,
    }


def stage_all() -> list[dict]:
    """
    Stage every dataset and write data/boundaries/manifest.json.

    Entries from earlier successful runs are kept when a dataset fails this
    time, so a temporary outage never removes working data.
    """
    settings.ensure_data_folders()
    connection = db.connect()
    manifest_path = settings.BOUNDARY_DIR / "manifest.json"
    previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}

    results = []
    for dataset in load_dataset_definitions():
        result = stage_dataset(connection, dataset)
        results.append(result)
        if result["status"] == "staged":
            previous[dataset["id"]] = result
        else:
            print(f"  ! {dataset['id']}: {result['error']}")
    manifest_path.write_text(json.dumps(previous, indent=2))

    audit.record(connection, "system", "stage-boundaries", None, {
        "staged": [r["id"] for r in results if r["status"] == "staged"],
        "failed": [r["id"] for r in results if r["status"] != "staged"],
    })
    connection.close()
    return results
