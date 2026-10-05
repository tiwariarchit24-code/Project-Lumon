"""
Local GeoTIFF / COG ingestion (lumon/imagery/local_ingest.py).

All rasters written here are SYNTHETIC TEST FIXTURES (small generated
GeoTIFFs). One test, test_real_archive_scene_registers_in_place, reads a REAL
Sentinel-2 scene already staged on this machine (skipped if absent); it is
registered in the isolated test database at its original path, not copied.
The embedding "model" in the pipeline tests is a FAKE (mean colour).
"""

import hashlib
import json
import socket

import numpy as np
import pytest
import rasterio
import rasterio.shutil
from rasterio.transform import from_origin

from lumon import db, settings
from lumon.change_ml import learned
from lumon.imagery import aoi as aoi_module
from lumon.imagery import archive, discovery, local_ingest, observations, semantic
from lumon.imagery.local_ingest import LocalIngestError, ingest_file

SIZE = 120
ORIGIN = (300000.0, 2110000.0)  # UTM 43N (TEST FIXTURE)
CANONICAL = ["B02", "B03", "B04", "B08", "B11", "SCL"]


def write_raster(folder, name, *, order=CANONICAL, named=True, tags=None, seed=0, origin=ORIGIN, crs="EPSG:32643",
                 res=10.0, dtype="uint16", nodata=0, count=None, transform=None, driver="GTiff"):
    """A small multi-band raster. Bands follow `order`; values are textured so the dark-pixel test has dark pixels."""
    rng = np.random.default_rng(seed)
    bands = {
        "B02": rng.integers(300, 1500, (SIZE, SIZE)), "B03": rng.integers(400, 1600, (SIZE, SIZE)),
        "B04": rng.integers(400, 1800, (SIZE, SIZE)), "B08": rng.integers(1500, 3000, (SIZE, SIZE)),
        "B11": rng.integers(1000, 2500, (SIZE, SIZE)), "SCL": np.full((SIZE, SIZE), 4),
    }
    data = np.stack([bands[b] for b in order]).astype(dtype) if count is None else rng.integers(0, 255, (count, SIZE, SIZE)).astype(dtype)
    path = folder / name
    profile = {"driver": driver, "width": SIZE, "height": SIZE, "count": data.shape[0], "dtype": dtype}
    if crs:
        profile["crs"] = crs
    profile["transform"] = transform if transform is not None else from_origin(origin[0], origin[1], res, res)
    if nodata is not None:
        profile["nodata"] = nodata
    with rasterio.open(path, "w", **profile) as target:
        target.write(data)
        if named and count is None:
            target.descriptions = tuple(order)
        if tags:
            target.update_tags(**tags)
    return path


S2_TAGS = {"datetime": "2024-01-12T05:53:41Z", "processing_baseline": "05.10", "boa_offset_applied": "True"}


class FakeModel:
    """TEST FIXTURE: 'embeds' a chip as its normalised mean colour."""
    key = "fake-model:1"
    version = {"model": "fake"}

    def __init__(self):
        self.images_encoded = 0

    def encode_images(self, chips):
        self.images_encoded += len(chips)
        v = np.zeros((len(chips), 512), dtype="float32")  # same length as RemoteCLIP embeddings
        v[:, :4] = [[c[..., 0].mean(), c[..., 1].mean(), c[..., 2].mean(), 1.0] for c in chips]
        return v / np.linalg.norm(v, axis=1, keepdims=True)


def scene_row(scene_id):
    connection = db.connect()
    row = connection.execute("SELECT * FROM scenes WHERE id = ?", (scene_id,)).fetchone()
    connection.close()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Metadata, formats, preservation
# ---------------------------------------------------------------------------

def test_metadata_is_read_not_guessed(isolated):
    path = write_raster(isolated, "a.tif", tags=S2_TAGS)
    meta = local_ingest.inspect_file(str(path))
    assert meta["crs"] == "EPSG:32643" and meta["width"] == SIZE and meta["height"] == SIZE and meta["band_count"] == 6
    assert meta["transform"] == [10.0, 0.0, ORIGIN[0], 0.0, -10.0, ORIGIN[1]] and meta["resolution"] == [10.0, 10.0]
    assert meta["bounds"] == [ORIGIN[0], ORIGIN[1] - SIZE * 10, ORIGIN[0] + SIZE * 10, ORIGIN[1]]
    assert meta["nodata"] == 0 and meta["band_descriptions"] == CANONICAL and meta["dtypes"] == ["uint16"] * 6
    assert meta["format"].startswith("GeoTIFF")


def test_cog_is_recognised_and_registered_without_conversion(isolated):
    plain = write_raster(isolated, "plain.tif", tags=S2_TAGS)
    cog = isolated / "scene_cog.tif"
    rasterio.shutil.copy(plain, cog, driver="COG")
    plain.unlink()
    assert local_ingest.inspect_file(str(cog))["format"] == "COG"
    report = ingest_file(str(cog), "cog-aoi")
    assert report["outcome"] == "INGESTED" and report["view_path"] == str(cog.resolve())  # read in place, no copy
    assert report["metadata"]["format"] == "COG"


def test_scene_preserves_grid_nodata_and_metadata(isolated):
    path = write_raster(isolated, "a.tif", tags=S2_TAGS)
    report = ingest_file(str(path), "local-a", sensor="sentinel-2b")
    row = scene_row(report["scene_id"])
    assert row["crs"] == "EPSG:32643" and row["acquired_at"] == "2024-01-12T05:53:41Z" and row["sensor"] == "sentinel-2b"
    assert row["radiometric_offset"] == 0.0 and row["quality_status"] == "usable" and row["cloud_fraction"] == 0.0
    with rasterio.open(row["file_path"]) as view, rasterio.open(path) as source:
        assert view.crs == source.crs and view.transform == source.transform and view.bounds == source.bounds
        assert view.res == source.res and view.shape == source.shape and view.nodata == source.nodata
    aoi = aoi_module.get_aoi("local-a")
    assert aoi["source"] == "local" and aoi["grid"]["bounds"] == list(rasterio.open(path).bounds)
    assert archive.grid_of(aoi) == ("EPSG:32643", tuple(rasterio.open(path).bounds))
    assert report["acquired_source"] == "file tag datetime" and report["radiometric_offset_source"].startswith("file tags")


def test_checksum_is_sha256_of_the_file(isolated):
    path = write_raster(isolated, "a.tif", tags=S2_TAGS)
    report = ingest_file(str(path), "local-a")
    assert report["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert scene_row(report["scene_id"])["file_sha256"] == report["sha256"]  # canonical file: the original is the view


def test_explicit_band_mapping_builds_a_vrt_with_the_same_pixels(isolated):
    order = ["SCL", "B11", "B08", "B04", "B03", "B02"]
    path = write_raster(isolated, "reordered.tif", order=order, named=False)
    with pytest.raises(LocalIngestError):
        ingest_file(str(path), "local-r", acquired="2024-02-01", bands="B02=6,B03=5,B04=4,B08=3,B11=2,SCL=9")  # band 9 does not exist
    report = ingest_file(str(path), "local-r", acquired="2024-02-01", reflectance_offset=0,
                         bands="B02=6,B03=5,B04=4,B08=3,B11=2,SCL=1")
    assert report["band_mapping_source"] == "operator" and report["view_path"].endswith(".vrt")
    assert (isolated / "reordered.tif").stat().st_size > 10 * (report and len(open(report["view_path"]).read()))  # VRT is tiny
    with rasterio.open(report["view_path"]) as view, rasterio.open(path) as source:
        assert list(view.descriptions) == CANONICAL and view.transform == source.transform and view.crs == source.crs
        for canonical_index, name in enumerate(CANONICAL, start=1):
            assert np.array_equal(view.read(canonical_index), source.read(order.index(name) + 1))
    assert report["quality_status"] == "usable" and report["compatibility"]["semantic-index"] == "SUPPORTED"


@pytest.mark.parametrize("mapping, message", [
    ("B02=1,B02=2", "given twice"), ("B05=1", "unknown canonical band"), ("B02=1,B03=1", "same source band"),
    ("B02", "expected NAME=INDEX"), ("B02=0", "not a band"),
])
def test_inconsistent_band_mapping_is_rejected(isolated, mapping, message):
    path = write_raster(isolated, "a.tif", named=False)
    with pytest.raises(LocalIngestError, match=message):
        ingest_file(str(path), "local-a", acquired="2024-01-01", bands=mapping)


# ---------------------------------------------------------------------------
# Invalid input and missing metadata
# ---------------------------------------------------------------------------

def test_invalid_files_are_rejected_clearly(isolated):
    with pytest.raises(LocalIngestError, match="file not found"):
        ingest_file(str(isolated / "missing.tif"), "x")
    (isolated / "notes.tif").write_text("not a raster")
    with pytest.raises(LocalIngestError, match="not a readable raster"):
        ingest_file(str(isolated / "notes.tif"), "x")
    with pytest.raises(LocalIngestError, match="local files only"):
        ingest_file("https://example.org/scene.tif", "x")
    no_crs = write_raster(isolated, "nocrs.tif", crs=None)
    with pytest.raises(LocalIngestError, match="no coordinate reference system"):
        ingest_file(str(no_crs), "x")
    rotated = write_raster(isolated, "rot.tif", transform=rasterio.Affine(10, 2, ORIGIN[0], 0, -10, ORIGIN[1]))
    with pytest.raises(LocalIngestError, match="rotated"):
        ingest_file(str(rotated), "x")
    png = write_raster(isolated, "img.png", count=3, dtype="uint8", driver="PNG", crs=None, nodata=None)
    with pytest.raises(LocalIngestError):
        ingest_file(str(png), "x")
    with pytest.raises(LocalIngestError, match="--aoi is required"):
        ingest_file(str(write_raster(isolated, "a.tif", tags=S2_TAGS)), "")


def test_unknown_date_is_never_invented(isolated):
    path = write_raster(isolated, "nodate.tif", tags={"processing_baseline": "05.10", "boa_offset_applied": "True"})
    pending = ingest_file(str(path), "local-d")
    assert pending["outcome"] == "PENDING METADATA" and "UNKNOWN" in pending["reason"]
    assert pending["compatibility"]["registration"].startswith("NOT ENOUGH METADATA")
    assert scene_row(pending["scene_id"]) is None and aoi_module.get_aoi("local-d") is None
    done = ingest_file(str(path), "local-d", acquired="2024-03-01")
    assert done["outcome"] == "INGESTED" and scene_row(done["scene_id"])["acquired_at"] == "2024-03-01T00:00:00Z"
    assert done["acquired_source"].startswith("operator (date only")


def test_conflicting_or_ambiguous_dates_are_rejected(isolated):
    path = write_raster(isolated, "a.tif", tags=S2_TAGS)
    with pytest.raises(LocalIngestError, match="file says acquired"):
        ingest_file(str(path), "local-a", acquired="2020-01-01")
    no_zone = write_raster(isolated, "nozone.tif", tags={"datetime": "2024-01-12T05:53:41"})
    with pytest.raises(LocalIngestError, match="no time zone"):
        ingest_file(str(no_zone), "local-z")


def test_incompatible_file_is_registered_but_kept_out_of_pipelines(isolated):
    rgb = write_raster(isolated, "rgb.tif", count=3, dtype="uint8")
    report = ingest_file(str(rgb), "local-rgb", acquired="2024-05-01")
    assert report["outcome"] == "INGESTED" and report["quality_status"] == "quarantined"
    assert report["compatibility"]["registration"] == "SUPPORTED"
    for capability in ("semantic-index", "image-similarity", "discovery", "rule-based-change", "learned-change"):
        assert report["compatibility"][capability].startswith("NOT COMPATIBLE") and "give --bands" in report["compatibility"][capability]
    assert report["sensor"] == "unknown" and scene_row(report["scene_id"])["processing_level"] is None  # nothing fabricated
    assert semantic.scene_problem(scene_row(report["scene_id"])).startswith("quality status quarantined")


def test_missing_offset_can_be_supplied_later(isolated):
    path = write_raster(isolated, "a.tif", tags={"datetime": "2024-01-12T05:53:41Z"})  # no radiometric tags
    first = ingest_file(str(path), "local-o")
    assert first["quality_status"] == "quarantined" and "radiometric offset UNKNOWN" in first["compatibility"]["semantic-index"]
    assert ingest_file(str(path), "local-o")["outcome"] == "ALREADY INGESTED"  # nothing new supplied: unchanged
    fixed = ingest_file(str(path), "local-o", reflectance_offset=0)
    assert fixed["outcome"] == "INGESTED" and fixed["scene_id"] == first["scene_id"] and fixed["quality_status"] == "usable"


def test_pipeline_requirements_are_reported_not_forced(isolated):
    coarse = write_raster(isolated, "coarse.tif", tags=S2_TAGS, res=30.0)
    report = ingest_file(str(coarse), "local-30")
    assert "needs 10 m pixels" in report["compatibility"]["semantic-index"] and report["quality_status"] == "quarantined"
    geographic = write_raster(isolated, "geo.tif", tags=S2_TAGS, crs="EPSG:4326", origin=(73.0, 19.0), res=0.0001)
    assert "projected CRS" in ingest_file(str(geographic), "local-geo")["compatibility"]["semantic-index"]


def test_grid_mismatch_with_an_existing_aoi_is_refused(isolated):
    ingest_file(str(write_raster(isolated, "a.tif", tags=S2_TAGS)), "local-a")
    shifted = write_raster(isolated, "b.tif", tags=S2_TAGS | {"datetime": "2025-01-10T05:53:41Z"}, origin=(ORIGIN[0] + 50, ORIGIN[1]), seed=2)
    with pytest.raises(LocalIngestError, match="does not resample"):
        ingest_file(str(shifted), "local-a")
    # Earth Search AOIs keep their own grid: a local file on another grid cannot join demo-01.
    with pytest.raises(LocalIngestError, match="pixel grid differs"):
        ingest_file(str(shifted), "demo-01")


# ---------------------------------------------------------------------------
# Duplicate / incremental behaviour and the existing pipeline
# ---------------------------------------------------------------------------

def test_duplicates_are_idempotent_and_new_files_are_incremental(isolated, monkeypatch):
    monkeypatch.setattr(semantic, "model_problems", lambda: [])
    monkeypatch.setattr(semantic, "model_key", lambda: FakeModel.key)
    monkeypatch.setattr(semantic, "_model_error", None)
    model = FakeModel()
    first = ingest_file(str(write_raster(isolated, "a.tif", tags=S2_TAGS)), "local-a")
    index1 = semantic.index("local-a", model=model, log=lambda _: None)
    assert index1["chips_embedded"] > 0 and index1["scenes_indexed"] == 1
    again = ingest_file(str(isolated / "a.tif"), "local-a")
    assert again["outcome"] == "ALREADY INGESTED" and again["scene_id"] == first["scene_id"]
    connection = db.connect()
    assert connection.execute("SELECT COUNT(*) FROM scenes").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM local_sources").fetchone()[0] == 1
    connection.close()
    assert semantic.index("local-a", model=model, log=lambda _: None)["chips_embedded"] == 0  # nothing re-embedded
    second = ingest_file(str(write_raster(isolated, "b.tif", tags=S2_TAGS | {"datetime": "2025-01-10T05:53:41Z"}, seed=5)), "local-a")
    assert second["outcome"] == "INGESTED" and second["aoi"].startswith("existing AOI")
    encoded_before = model.images_encoded
    index3 = semantic.index("local-a", model=model, log=lambda _: None)
    assert index3["chips_embedded"] == index1["chips_embedded"] and index3["chips_cached"] == index1["chips_embedded"]
    assert model.images_encoded - encoded_before == index1["chips_embedded"]  # only the new scene's chips
    # Image similarity and discovery work on the local chips.
    chips = semantic.similar([f"{first['scene_id']}:112:0:0"], scope="all", limit=5)
    assert chips["results"] and all(r["aoi_id"] == "local-a" for r in chips["results"])
    version = discovery.build(log=lambda _: None)
    assert version["n_embeddings"] == 2 * index1["chips_embedded"]
    third = ingest_file(str(write_raster(isolated, "c.tif", tags=S2_TAGS | {"datetime": "2026-01-10T05:53:41Z"}, seed=9)), "local-a")
    semantic.index("local-a", model=model, log=lambda _: None)
    update = discovery.update(log=lambda _: None)
    assert update["new_chips"] == index1["chips_embedded"] and update["version_id"] == version["id"]
    found = discovery.discover(f"{third['scene_id']}:112:0:0")
    assert found["reference_chip_id"].startswith(third["scene_id"]) and found["places"]


def test_temporal_pairing_tiles_and_map_corners_use_the_raster_grid(isolated, monkeypatch):
    monkeypatch.setattr(learned, "model_problems", lambda: [])
    a = ingest_file(str(write_raster(isolated, "a.tif", tags=S2_TAGS)), "local-a")
    # Same ground a year later (same texture): a registered pair, as from the same sensor grid.
    b = ingest_file(str(write_raster(isolated, "b.tif", tags=S2_TAGS | {"datetime": "2025-01-10T05:53:41Z"})), "local-a")
    check = learned.check_pair(a["scene_id"], b["scene_id"])
    assert check["ok"], check["problems"]
    observations.process_aoi("local-a", log=lambda _: None)
    connection = db.connect()
    tile = json.loads(connection.execute("SELECT geometry FROM tiles WHERE aoi_id = 'local-a' AND row = 0 AND col = 0").fetchone()[0])
    connection.close()
    # The raster's own top-left corner (UTM is slightly rotated against north, so not the WGS84 envelope).
    from rasterio.warp import transform as warp_transform
    (lon,), (lat,) = warp_transform("EPSG:32643", "EPSG:4326", [ORIGIN[0]], [ORIGIN[1]])
    assert abs(tile["coordinates"][0][3][0] - lon) < 1e-5 and abs(tile["coordinates"][0][3][1] - lat) < 1e-5
    from lumon.routes.imagery import image_corners
    corners = image_corners(aoi_module.get_aoi("local-a"))
    assert abs(corners[0][0] - lon) < 1e-5 and abs(corners[0][1] - lat) < 1e-5


def test_existing_earth_search_aoi_grid_is_unchanged(isolated, monkeypatch):
    demo = aoi_module.get_aoi("demo-01")
    assert archive.grid_of(demo) == (f"EPSG:{archive.EXPECTED_EPSG}", archive.aoi_utm_bounds(demo))
    assert demo.get("source") is None and "grid" not in demo
    # A local AOI refuses the Earth Search path (checked before any catalogue request).
    ingest_file(str(write_raster(isolated, "a.tif", tags=S2_TAGS)), "local-a")
    monkeypatch.setenv("LUMON_MODE", "connected")
    with pytest.raises(ValueError, match="local AOI"):
        archive.ingest_aoi("local-a", log=lambda _: None)


def test_provenance_audit_and_export(isolated):
    report = ingest_file(str(write_raster(isolated, "a.tif", tags=S2_TAGS)), "local-a", sensor="sentinel-2b")
    from lumon import provenance
    connection = db.connect()
    record = provenance.get(connection, report["provenance_id"])
    audit_rows = connection.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'local-ingest'").fetchone()[0]
    connection.close()
    assert record["kind"] == "scene" and record["source_id"] == "local-file" and record["input_sha256"] == report["sha256"]
    params = record["parameters"]
    for key in ("filename", "crs", "width", "height", "resolution", "bounds", "transform", "nodata", "band_mapping",
                "acquired_at", "acquired_source", "sensor", "radiometric_offset_source", "compatibility"):
        assert key in params, key
    assert record["input_ref"].startswith("file://") and audit_rows == 1
    described = local_ingest.describe(report["scene_id"])
    assert described["original_sha256"] == report["sha256"] and described["status"] == "registered"
    from lumon import export
    layers = export.export_geopackage(isolated / "out.gpkg")["layers"]
    assert layers["scenes"] == 1


def test_offline_ingestion_with_sockets_blocked(isolated, monkeypatch):
    monkeypatch.setenv("LUMON_MODE", "airgapped")

    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted during local ingestion")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(semantic, "model_problems", lambda: [])
    monkeypatch.setattr(semantic, "model_key", lambda: FakeModel.key)
    report = ingest_file(str(write_raster(isolated, "a.tif", tags=S2_TAGS)), "offline-aoi")
    assert report["outcome"] == "INGESTED"
    assert semantic.index("offline-aoi", model=FakeModel(), log=lambda _: None)["chips_embedded"] > 0


def test_cli(isolated, capsys):
    from lumon import cli
    path = write_raster(isolated, "a.tif", tags=S2_TAGS)
    cli.cmd_ingest_local([str(path), "--aoi", "cli-aoi", "--sensor", "sentinel-2b"])
    assert "INGESTED" in capsys.readouterr().out
    cli.cmd_ingest_local([str(path), "--aoi", "cli-aoi"])
    assert "ALREADY INGESTED" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.cmd_ingest_local([str(isolated / "missing.tif"), "--aoi", "cli-aoi"])
    assert "LOCAL INGEST FAILED" in capsys.readouterr().out


real_scenes = sorted((settings.ROOT_DIR / "data" / "imagery" / "demo-01").glob("*.tif"))


@pytest.mark.skipif(not real_scenes, reason="no real staged Sentinel-2 scene on this machine")
def test_real_archive_scene_registers_in_place(isolated):
    """REAL DATA: a staged Earth Search Sentinel-2 L2A window, ingested as a local file (no copy)."""
    real = real_scenes[-1]
    report = ingest_file(str(real), "real-local")
    assert report["outcome"] == "INGESTED" and report["view_path"] == str(real.resolve())
    assert report["acquired_source"] == "file tag datetime" and report["band_mapping_source"] == "file band descriptions"
    assert all(v.startswith("SUPPORTED") for v in report["compatibility"].values())
    assert report["quality_status"] in ("usable", "degraded")
    with rasterio.open(real) as source:
        assert scene_row(report["scene_id"])["crs"] == source.crs.to_string()
        assert aoi_module.get_aoi("real-local")["grid"]["bounds"] == list(source.bounds)
