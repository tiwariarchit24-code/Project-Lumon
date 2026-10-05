"""
Major ports from Natural Earth (public domain, small-scale dataset).

The file is global; lumon.ingest keeps only ports inside the India
operating area. This is a generalised list of major ports, not a complete
port register.
"""

from .. import net
from ..geo import shapefile_zip
from .common import make_entity


def fetch(context) -> bytes:
    """Download the zipped ports shapefile."""
    return net.fetch_bytes(context["source"]["endpoint"], timeout=120)


def normalize(data: bytes, context) -> list[dict]:
    """One port entity per point in the file."""
    records = []
    for index, feature in enumerate(shapefile_zip.read_features(data)):
        properties = feature["properties"]
        name = properties.get("name") or f"Unnamed port {index}"
        records.append(make_entity(
            record_id=f"{name}-{index}",
            entity_type="port",
            category="MARITIME",
            name=name,
            geometry=feature["geometry"],
            attributes={"website": properties.get("website") or None, "scale_rank": properties.get("scalerank")},
            raw=properties,
        ))
    return records
