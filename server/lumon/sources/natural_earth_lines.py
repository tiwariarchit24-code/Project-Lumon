"""
Generic adapter for Natural Earth line/polygon reference layers
(rivers, lakes, railways, roads).

These files are global. To keep processing fast we first drop features
whose bounding box does not even overlap the operating-area bounding box;
lumon.ingest then applies the real India boundary test.
"""

from .. import net
from ..geo import geometry, shapefile_zip
from .common import make_reference

# Properties worth keeping for labels and styling. Natural Earth uses
# slightly different field names in different files, so we try several.
KEEP_FIELDS = ["name", "name_en", "featurecla", "scalerank", "type", "category", "label"]


def fetch(context) -> bytes:
    """Download the zipped shapefile named in the registry entry."""
    return net.fetch_bytes(context["source"]["endpoint"], timeout=180)


def normalize(data: bytes, context) -> list[dict]:
    """One reference feature per line/polygon overlapping the operating-area bbox."""
    area_box = context["bbox"]
    records = []
    for feature in shapefile_zip.read_features(data):
        if not geometry.bbox_intersects(geometry.bbox(feature["geometry"]), area_box):
            continue
        properties = {key.lower(): value for key, value in feature["properties"].items()}
        kept = {field: properties.get(field) for field in KEEP_FIELDS if properties.get(field) not in (None, "")}
        simplified = geometry.simplify_geometry(feature["geometry"], 0.002) or feature["geometry"]
        records.append(make_reference(simplified, kept))
    return records
