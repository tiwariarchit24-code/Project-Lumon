"""
Small, dependency-free geometry helpers for GeoJSON.

We only need a handful of operations (bounding boxes, point-in-polygon,
line simplification, distances), so we write them in plain Python instead
of installing a large GIS library. Coordinates are always [longitude,
latitude] in degrees (WGS84), as in GeoJSON.
"""

import math

EARTH_RADIUS_M = 6_371_008.8  # mean Earth radius in metres


# ---------------------------------------------------------------------------
# Bounding boxes
# ---------------------------------------------------------------------------

def iter_positions(geometry: dict):
    """
    Yield every [lon, lat] position in a GeoJSON geometry, whatever its type.
    This lets other helpers (like bbox) work on points, lines and polygons alike.
    """
    geometry_type = geometry["type"]
    coordinates = geometry.get("coordinates")
    if geometry_type == "Point":
        yield coordinates
    elif geometry_type in ("MultiPoint", "LineString"):
        yield from coordinates
    elif geometry_type in ("MultiLineString", "Polygon"):
        for part in coordinates:
            yield from part
    elif geometry_type == "MultiPolygon":
        for polygon in coordinates:
            for ring in polygon:
                yield from ring
    elif geometry_type == "GeometryCollection":
        for child in geometry["geometries"]:
            yield from iter_positions(child)


def bbox(geometry: dict) -> list[float]:
    """Return [min_lon, min_lat, max_lon, max_lat] for a GeoJSON geometry."""
    lons, lats = [], []
    for position in iter_positions(geometry):
        lons.append(position[0])
        lats.append(position[1])
    return [min(lons), min(lats), max(lons), max(lats)]


def bbox_contains(box: list[float], lon: float, lat: float) -> bool:
    """True if the point lies inside (or on the edge of) the bounding box."""
    return box[0] <= lon <= box[2] and box[1] <= lat <= box[3]


def bbox_intersects(a: list[float], b: list[float]) -> bool:
    """True if two bounding boxes overlap."""
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


# ---------------------------------------------------------------------------
# Point in polygon
# ---------------------------------------------------------------------------

def _point_in_ring(lon: float, lat: float, ring: list) -> bool:
    """
    Ray-casting test: imagine a line going east from the point and count how
    many times it crosses the ring's edges. An odd count means "inside".
    """
    inside = False
    count = len(ring)
    j = count - 1
    for i in range(count):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        crosses = (yi > lat) != (yj > lat)
        if crosses and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def point_in_polygon(lon: float, lat: float, polygon: list) -> bool:
    """
    `polygon` is a GeoJSON Polygon coordinate list: the first ring is the
    outer boundary, the following rings are holes. A point inside a hole is
    outside the polygon.
    """
    if not _point_in_ring(lon, lat, polygon[0]):
        return False
    for hole in polygon[1:]:
        if _point_in_ring(lon, lat, hole):
            return False
    return True


def point_in_geometry(lon: float, lat: float, geometry: dict) -> bool:
    """True if the point is inside a Polygon or MultiPolygon geometry."""
    if geometry["type"] == "Polygon":
        return point_in_polygon(lon, lat, geometry["coordinates"])
    if geometry["type"] == "MultiPolygon":
        return any(point_in_polygon(lon, lat, polygon) for polygon in geometry["coordinates"])
    return False


# ---------------------------------------------------------------------------
# Simplification (to keep boundary files small)
# ---------------------------------------------------------------------------

def _perpendicular_distance(point, start, end) -> float:
    """Distance from `point` to the straight line through start and end (in degrees)."""
    if start == end:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    dx, dy = end[0] - start[0], end[1] - start[1]
    return abs(dy * point[0] - dx * point[1] + end[0] * start[1] - end[1] * start[0]) / math.hypot(dx, dy)


def simplify_line(points: list, tolerance: float) -> list:
    """
    Douglas-Peucker line simplification.

    Keep the two end points; find the point furthest from the straight line
    between them; if it is further than `tolerance`, keep it and repeat on
    both halves, otherwise drop everything in between. Written with an
    explicit stack instead of recursion so very long coastlines are safe.
    """
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        max_distance, index = 0.0, None
        for i in range(first + 1, last):
            distance = _perpendicular_distance(points[i], points[first], points[last])
            if distance > max_distance:
                max_distance, index = distance, i
        if index is not None and max_distance > tolerance:
            keep[index] = True
            stack.append((first, index))
            stack.append((index, last))
    return [point for point, kept in zip(points, keep) if kept]


def simplify_geometry(geometry: dict, tolerance: float, decimals: int = 4) -> dict | None:
    """
    Simplify a Polygon/MultiPolygon/LineString and round coordinates.

    - tolerance: in degrees (0.001 deg is roughly 100 m)
    - decimals:  4 decimal places is roughly 11 m, plenty for boundaries
    Rings that collapse to fewer than 4 points are dropped. Returns None if
    nothing is left.
    """
    def clean(points):
        simplified = simplify_line(points, tolerance)
        return [[round(p[0], decimals), round(p[1], decimals)] for p in simplified]

    if geometry["type"] == "Polygon":
        rings = [clean(ring) for ring in geometry["coordinates"]]
        rings = [ring for ring in rings if len(ring) >= 4]
        return {"type": "Polygon", "coordinates": rings} if rings else None
    if geometry["type"] == "MultiPolygon":
        polygons = []
        for polygon in geometry["coordinates"]:
            rings = [clean(ring) for ring in polygon]
            if rings and len(rings[0]) >= 4:
                polygons.append([ring for ring in rings if len(ring) >= 4])
        return {"type": "MultiPolygon", "coordinates": polygons} if polygons else None
    if geometry["type"] == "LineString":
        line = clean(geometry["coordinates"])
        return {"type": "LineString", "coordinates": line} if len(line) >= 2 else None
    if geometry["type"] == "MultiLineString":
        lines = [clean(line) for line in geometry["coordinates"]]
        lines = [line for line in lines if len(line) >= 2]
        return {"type": "MultiLineString", "coordinates": lines} if lines else None
    return geometry


# ---------------------------------------------------------------------------
# Distances and centres
# ---------------------------------------------------------------------------

def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle distance between two points in metres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def representative_point(geometry: dict) -> tuple[float, float]:
    """
    A single point to place a marker for any geometry.
    For points this is the point itself; otherwise the centre of the bbox.
    (Good enough for markers; not a true centroid.)
    """
    if geometry["type"] == "Point":
        return geometry["coordinates"][0], geometry["coordinates"][1]
    box = bbox(geometry)
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def largest_polygon_label_point(geometry: dict) -> tuple[float, float]:
    """
    A label point for a (Multi)Polygon: the bbox centre of its largest part,
    nudged to a vertex average if that centre falls outside the shape.
    Used to place state/district names and gazetteer entries.
    """
    polygons = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
    largest = max(polygons, key=lambda polygon: len(polygon[0]))
    outer = largest[0]
    box = bbox({"type": "Polygon", "coordinates": [outer]})
    lon, lat = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    if point_in_polygon(lon, lat, largest):
        return lon, lat
    return sum(p[0] for p in outer) / len(outer), sum(p[1] for p in outer) / len(outer)
