"""
Shared test setup.

Every test runs against a temporary data folder and database, so tests
never touch the real staged data in data/ and never use the network
(the mode is forced to air-gapped).

All geometry and records used in tests are SYNTHETIC TEST FIXTURES.
They are not real places, events or observations.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lumon import settings  # noqa: E402
from lumon.geo import boundary  # noqa: E402

# TEST FIXTURE: a 10 x 10 degree square "operating area" (not India).
SQUARE = {"type": "Polygon", "coordinates": [[[70, 10], [80, 10], [80, 20], [70, 20], [70, 10]]]}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Point all data paths at a temporary folder and force air-gapped mode."""
    monkeypatch.setenv("LUMON_MODE", "airgapped")
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(settings, "BOUNDARY_DIR", tmp_path / "boundaries")
    monkeypatch.setattr(settings, "REFERENCE_DIR", tmp_path / "reference")
    monkeypatch.setattr(settings, "SNAPSHOT_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(settings, "IMAGERY_DIR", tmp_path / "imagery")
    monkeypatch.setattr(settings, "EXPORT_DIR", tmp_path / "exports")
    monkeypatch.setattr(settings, "DATABASE_PATH", tmp_path / "test.db")
    yield tmp_path


@pytest.fixture
def square_area(monkeypatch):
    """Use the synthetic square as the operating area."""
    from lumon.geo import geometry
    monkeypatch.setattr(boundary, "_operating_area_parts", lambda: ((geometry.bbox(SQUARE), SQUARE),))
    return SQUARE
