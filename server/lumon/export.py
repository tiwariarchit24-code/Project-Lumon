"""
Export analysis results as a GeoPackage (.gpkg) for GIS software (QGIS,
ArcGIS...).

A GeoPackage is an SQLite database with a few standard metadata tables
(OGC GeoPackage 1.3). We write it directly with Python's sqlite3, so no GIS
library is needed:

  gpkg_spatial_ref_sys   coordinate systems (we use EPSG:4326, lon/lat)
  gpkg_contents          one row per layer
  gpkg_geometry_columns  which column of each layer holds geometry

Each geometry is stored as a "GeoPackage binary": a small header ('GP',
version, flags, SRS id) followed by standard WKB (well-known binary).

Layers written:
  change_events  (polygons) every change candidate, accepted and suppressed
  osint_events   (points)   OSINT events in the operating area
  scenes         (table)    satellite observations used
  decisions      (table)    analyst decisions
  provenance     (table)    provenance records
"""

import sqlite3
import struct
from datetime import datetime, timezone

from . import audit, db, settings

SRS_ID = 4326


# ---------------------------------------------------------------------------
# WKB encoding (little-endian)
# ---------------------------------------------------------------------------

def _wkb_point(x: float, y: float) -> bytes:
    return struct.pack("<BIdd", 1, 1, x, y)


def _ring(points: list) -> bytes:
    return struct.pack("<I", len(points)) + b"".join(struct.pack("<dd", p[0], p[1]) for p in points)


def _wkb_polygon(rings: list) -> bytes:
    return struct.pack("<BII", 1, 3, len(rings)) + b"".join(_ring(r) for r in rings)


def _wkb_multipolygon(polygons: list) -> bytes:
    return struct.pack("<BII", 1, 6, len(polygons)) + b"".join(_wkb_polygon(p) for p in polygons)


def geometry_blob(geometry: dict) -> bytes | None:
    """GeoJSON geometry -> GeoPackage binary (Point, Polygon, MultiPolygon)."""
    if geometry is None:
        return None
    kind = geometry["type"]
    if kind == "Point":
        wkb = _wkb_point(*geometry["coordinates"][:2])
    elif kind == "Polygon":
        wkb = _wkb_polygon(geometry["coordinates"])
    elif kind == "MultiPolygon":
        wkb = _wkb_multipolygon(geometry["coordinates"])
    else:
        return None
    # Header: magic 'GP', version 0, flags 0b00000001 (little-endian, no envelope), srs_id.
    return b"GP" + struct.pack("<BBi", 0, 1, SRS_ID) + wkb


# ---------------------------------------------------------------------------
# File structure
# ---------------------------------------------------------------------------

def _create_metadata_tables(gpkg: sqlite3.Connection) -> None:
    gpkg.execute("PRAGMA application_id = 1196444487")  # 'GPKG'
    gpkg.execute("PRAGMA user_version = 10300")  # GeoPackage 1.3
    gpkg.executescript("""
        CREATE TABLE gpkg_spatial_ref_sys (srs_name TEXT NOT NULL, srs_id INTEGER PRIMARY KEY, organization TEXT NOT NULL,
            organization_coordsys_id INTEGER NOT NULL, definition TEXT NOT NULL, description TEXT);
        CREATE TABLE gpkg_contents (table_name TEXT PRIMARY KEY, data_type TEXT NOT NULL, identifier TEXT UNIQUE,
            description TEXT DEFAULT '', last_change DATETIME NOT NULL, min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE,
            srs_id INTEGER);
        CREATE TABLE gpkg_geometry_columns (table_name TEXT NOT NULL, column_name TEXT NOT NULL, geometry_type_name TEXT NOT NULL,
            srs_id INTEGER NOT NULL, z TINYINT NOT NULL, m TINYINT NOT NULL, PRIMARY KEY (table_name, column_name));
    """)
    wgs84 = ('GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],'
             'UNIT["degree",0.0174532925199433],AUTHORITY["EPSG","4326"]]')
    gpkg.executemany("INSERT INTO gpkg_spatial_ref_sys VALUES (?, ?, ?, ?, ?, ?)", [
        ("Undefined cartesian SRS", -1, "NONE", -1, "undefined", None),
        ("Undefined geographic SRS", 0, "NONE", 0, "undefined", None),
        ("WGS 84 geodetic", 4326, "EPSG", 4326, wgs84, "longitude/latitude coordinates in decimal degrees"),
    ])


def _add_layer(gpkg, name: str, columns: dict, rows: list[dict], geometry_type: str | None, description: str) -> None:
    """
    Create one layer table and fill it.
    - columns: {column name: SQL type}; geometry (if any) goes in column 'geom'
    - geometry_type: 'POINT' / 'MULTIPOLYGON' / None for a plain table
    """
    column_sql = ", ".join(f'"{name_}" {kind}' for name_, kind in columns.items())
    geom_sql = ", geom BLOB" if geometry_type else ""
    gpkg.execute(f'CREATE TABLE "{name}" (fid INTEGER PRIMARY KEY AUTOINCREMENT, {column_sql}{geom_sql})')
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    boxes = [r["_bbox"] for r in rows if r.get("_bbox")]
    extent = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)) if boxes else (None,) * 4
    gpkg.execute("INSERT INTO gpkg_contents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (name, "features" if geometry_type else "attributes", name, description, now, *extent,
                  SRS_ID if geometry_type else None))
    if geometry_type:
        gpkg.execute("INSERT INTO gpkg_geometry_columns VALUES (?, 'geom', ?, ?, 0, 0)", (name, geometry_type, SRS_ID))
    names = list(columns) + (["geom"] if geometry_type else [])
    placeholders = ", ".join("?" * len(names))
    quoted = ", ".join(f'"{n}"' for n in names)
    for row in rows:
        gpkg.execute(f'INSERT INTO "{name}" ({quoted}) VALUES ({placeholders})', [row.get(n) for n in names])


def export_geopackage(path=None) -> dict:
    """Write the GeoPackage and return {path, layers: {name: row count}}."""
    from .geo import geometry as geo  # local import keeps module start-up light

    settings.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = path or settings.EXPORT_DIR / f"lumon-export-{stamp}.gpkg"
    connection = db.connect()
    gpkg = sqlite3.connect(path)
    _create_metadata_tables(gpkg)

    changes = []
    for row in connection.execute("SELECT * FROM change_candidates ORDER BY earliest_supported_after"):
        shape = db.from_json(row["geometry"])
        changes.append({
            "candidate_id": row["id"], "aoi_id": row["aoi_id"], "change_class": row["change_class"], "direction": row["direction"],
            "from_class": row["from_class"], "to_class": row["to_class"], "status": row["status"], "review_state": row["review_state"],
            "last_clean_before": row["last_clean_before"], "earliest_supported_after": row["earliest_supported_after"],
            "area_m2": row["area_m2"], "area_min_m2": row["area_min_m2"], "area_max_m2": row["area_max_m2"],
            "score": row["score"], "score_kind": row["score_kind"], "gate_results": row["gate_results"],
            "provenance_id": row["provenance_id"], "geom": geometry_blob(shape), "_bbox": geo.bbox(shape),
        })
    _add_layer(gpkg, "change_events", {
        "candidate_id": "TEXT", "aoi_id": "TEXT", "change_class": "TEXT", "direction": "TEXT", "from_class": "TEXT",
        "to_class": "TEXT", "status": "TEXT", "review_state": "TEXT", "last_clean_before": "TEXT",
        "earliest_supported_after": "TEXT", "area_m2": "REAL", "area_min_m2": "REAL", "area_max_m2": "REAL",
        "score": "REAL", "score_kind": "TEXT", "gate_results": "TEXT", "provenance_id": "TEXT",
    }, changes, "MULTIPOLYGON", "Change candidates (accepted and suppressed) with gate results; score is uncalibrated")

    events = []
    for row in connection.execute("SELECT * FROM events WHERE lon IS NOT NULL"):
        events.append({
            "event_id": row["id"], "source_id": row["source_id"], "type": row["type"], "category": row["category"],
            "title": row["title"], "start_time": row["start_time"], "end_time": row["end_time"], "status": row["status"],
            "evidence_type": row["evidence_type"], "provenance_id": row["provenance_id"],
            "geom": geometry_blob({"type": "Point", "coordinates": [row["lon"], row["lat"]]}),
            "_bbox": [row["lon"], row["lat"], row["lon"], row["lat"]],
        })
    _add_layer(gpkg, "osint_events", {
        "event_id": "TEXT", "source_id": "TEXT", "type": "TEXT", "category": "TEXT", "title": "TEXT", "start_time": "TEXT",
        "end_time": "TEXT", "status": "TEXT", "evidence_type": "TEXT", "provenance_id": "TEXT",
    }, events, "POINT", "OSINT events (representative points) inside the India operating area")

    scenes = [dict(r) for r in connection.execute(
        "SELECT id AS scene_id, aoi_id, sensor, acquired_at, crs, processing_level, file_sha256, cloud_fraction, quality_status, quarantine_reason, provenance_id FROM scenes")]
    _add_layer(gpkg, "scenes", {k: "TEXT" if k != "cloud_fraction" else "REAL" for k in
                                ["scene_id", "aoi_id", "sensor", "acquired_at", "crs", "processing_level", "file_sha256", "cloud_fraction", "quality_status", "quarantine_reason", "provenance_id"]},
               scenes, None, "Satellite observations registered in the archive")

    decisions = [dict(r) for r in connection.execute("SELECT target_kind, target_id, decision, new_label, analyst, reason, decided_at FROM decisions")]
    _add_layer(gpkg, "decisions", {k: "TEXT" for k in ["target_kind", "target_id", "decision", "new_label", "analyst", "reason", "decided_at"]},
               decisions, None, "Analyst decisions")

    records = [dict(r) for r in connection.execute("SELECT * FROM provenance")]
    _add_layer(gpkg, "provenance", {k: "TEXT" for k in ["id", "kind", "source_id", "source_version", "retrieved_at", "input_ref", "input_sha256", "processing", "processing_version", "parameters", "created_at"]},
               records, None, "Provenance records")

    gpkg.commit()
    gpkg.close()
    layers = {"change_events": len(changes), "osint_events": len(events), "scenes": len(scenes), "decisions": len(decisions), "provenance": len(records)}
    audit.record(connection, "analyst", "export", str(path), layers)
    connection.close()
    return {"path": str(path), "layers": layers}
