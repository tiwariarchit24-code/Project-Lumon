"""
Read a zipped ESRI shapefile (as published by Natural Earth and others)
straight from memory and return GeoJSON-like features.
"""

import io
import zipfile

import shapefile  # pyshp: small pure-Python shapefile reader


def read_features(data: bytes, keep=None) -> list[dict]:
    """
    Return [{"type": "Feature", "properties": {...}, "geometry": {...}}, ...].

    - data: the bytes of the .zip file
    - keep: optional function(properties) -> bool; records for which it
            returns False are skipped (e.g. "only India")
    """
    archive = zipfile.ZipFile(io.BytesIO(data))
    base = next(name[:-4] for name in archive.namelist() if name.endswith(".shp"))
    reader = shapefile.Reader(
        shp=io.BytesIO(archive.read(base + ".shp")),
        shx=io.BytesIO(archive.read(base + ".shx")),
        dbf=io.BytesIO(archive.read(base + ".dbf")),
        encoding="utf-8",
        encodingErrors="replace",
    )
    field_names = [field[0] for field in reader.fields[1:]]  # skip the deletion-flag field
    features = []
    for shape_record in reader.iterShapeRecords():
        properties = dict(zip(field_names, shape_record.record))
        if keep is not None and not keep(properties):
            continue
        if shape_record.shape.shapeType == shapefile.NULL:
            continue
        features.append({"type": "Feature", "properties": properties, "geometry": shape_record.shape.__geo_interface__})
    return features


def version_note(data: bytes) -> str | None:
    """Natural Earth zips include a small VERSION text file; return its contents if present."""
    archive = zipfile.ZipFile(io.BytesIO(data))
    for name in archive.namelist():
        if "VERSION" in name.upper():
            return archive.read(name).decode("utf-8", errors="ignore").strip()
    return None
