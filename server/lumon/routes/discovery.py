"""
Discovery / embedding-based clustering routes (PS 26227 §2.2.4).

Clusters are embedding-based groupings with neutral names, never semantic
labels. Requests are refused (409) when RemoteCLIP is not staged or no
clustering version exists; nothing is substituted.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import worker
from ..imagery import discovery, semantic

router = APIRouter(prefix="/api/discovery")


class DiscoverAt(BaseModel):
    lon: float
    lat: float
    scene_id: str | None = None


class DiscoverRequest(BaseModel):
    chip_id: str | None = None
    at: DiscoverAt | None = None  # or: the chip under a map point (latest indexed scene)


@router.get("/status")
def status():
    """NOT STAGED / INDEXING / PARTIAL / READY / UNAVAILABLE with the current version."""
    return discovery.status()


@router.get("/clusters")
def clusters(family_px: int | None = None):
    """Clusters of the current version (neutral labels)."""
    return discovery.list_clusters(family_px=family_px)


@router.get("/clusters/{cluster_id}/places.geojson")
def cluster_places_geojson(cluster_id: str, reference: str | None = None):
    """One footprint per member place, for the map."""
    try:
        return discovery.places_geojson(cluster_id, reference)
    except KeyError:
        raise HTTPException(404, "cluster not found in the current version")


@router.get("/clusters/{cluster_id}")
def cluster_detail(cluster_id: str, reference: str | None = None):
    """Member places, ranked by similarity to a reference chip (or to the centroid)."""
    try:
        return discovery.cluster_places(cluster_id, reference)
    except KeyError:
        raise HTTPException(404, "cluster not found in the current version")


@router.post("/discover")
def discover(request: DiscoverRequest):
    """Reference chip (or map point) -> its cluster -> other member places ranked by similarity."""
    chip_id = request.chip_id
    try:
        if chip_id is None and request.at is not None:
            found = semantic.chips_at(request.at.lon, request.at.lat, request.at.scene_id)
            if not found:
                raise HTTPException(404, "no indexed image chip at this location")
            chip_id = found[0]["id"]
        if chip_id is None:
            raise HTTPException(400, "give chip_id or at")
        return discovery.discover(chip_id)
    except discovery.DiscoveryUnavailable as error:
        raise HTTPException(409, {"state": "NOT STAGED", "reasons": [str(error)]})
    except KeyError:
        raise HTTPException(404, "chip not indexed with the current embedding version")


@router.post("/build")
def build():
    """Queue a full re-clustering (a new version; embeddings are only read)."""
    if semantic.model_problems():
        raise HTTPException(409, {"state": "NOT STAGED", "reasons": semantic.model_problems()})
    return {"job_id": worker.enqueue("discovery-build", {"actor": "analyst"})}


@router.post("/update")
def update():
    """Queue an incremental assignment of newly embedded chips (centroids frozen)."""
    if semantic.model_problems():
        raise HTTPException(409, {"state": "NOT STAGED", "reasons": semantic.model_problems()})
    return {"job_id": worker.enqueue("discovery-update", {"actor": "analyst"})}


@router.get("/evaluation")
def evaluation():
    """Structural metrics of the current version (no labels, no accuracy)."""
    if discovery.get_version() is None:
        raise HTTPException(409, {"state": "NOT STAGED", "reasons": ["no clustering version yet"]})
    return discovery.evaluate()
