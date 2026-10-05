"""
Pilot routes: the NIT Raipur pilot summary, its study-area geometry for the
map, and the AI/ML capability and model registries.
"""

from fastapi import APIRouter, HTTPException

from .. import pilot

router = APIRouter(prefix="/api")


def _check(pilot_id: str) -> None:
    if pilot_id != pilot.load_pilot()["id"]:
        raise HTTPException(404, "unknown pilot")


@router.get("/pilots/{pilot_id}")
def pilot_summary(pilot_id: str):
    """Study area, data registry, capability and model registries with counts by state."""
    _check(pilot_id)
    return pilot.summary()


@router.get("/pilots/{pilot_id}/aoi.geojson")
def pilot_aoi(pilot_id: str):
    """The study area (PROVISIONAL until a verified campus boundary is supplied)."""
    _check(pilot_id)
    return pilot.aoi_feature_collection()


@router.get("/ai/capabilities")
def capabilities():
    """The AI/ML capability registry with its validation result."""
    return {**pilot.load_capabilities(), "registry_problems": pilot.validate_registries()}


@router.get("/ai/models")
def models():
    """Candidate models (none deployed)."""
    return pilot.load_models()
