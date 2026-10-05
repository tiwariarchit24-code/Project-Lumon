"""
Discovery / embedding-based clustering.

All embeddings here are SYNTHETIC TEST FIXTURES written straight into the
chip-embedding cache: three well-separated groups ("A", "B", "C") seen at
several places and dates, plus deliberately invalid vectors. They test the
clustering, versioning and lookup logic, not RemoteCLIP and not any
real-world grouping.
"""

import hashlib
import json
import re

import numpy as np
import pytest

from lumon import db
from lumon.change_ml import learned
from lumon.imagery import discovery, semantic

KEY = "fake-embedding:1"
DIM = discovery.DIMENSION


def unit(vector):
    vector = np.asarray(vector, dtype="float32")
    return vector / np.linalg.norm(vector)


def group_vector(group: str, jitter: int) -> np.ndarray:
    """A unit vector near one of three orthogonal directions (TEST FIXTURE)."""
    rng = np.random.default_rng(jitter)
    base = np.zeros(DIM, dtype="float32")
    base[{"A": 0, "B": 1, "C": 2}[group]] = 1.0
    return unit(base + 0.01 * rng.standard_normal(DIM).astype("float32"))


def insert_chip(connection, chip_id, vector_bytes, place_col, date, aoi="test-aoi", size=112):
    lon, lat = 73.0 + place_col * 0.01, 19.0
    footprint = {"type": "Polygon", "coordinates": [[[lon, lat], [lon + 0.01, lat], [lon + 0.01, lat + 0.01], [lon, lat + 0.01], [lon, lat]]]}
    scene = f"SCENE_{date.replace('-', '')}"
    connection.execute(
        """INSERT OR IGNORE INTO scenes (id, aoi_id, sensor, acquired_at, quality_status, created_at)
           VALUES (?, ?, 'sentinel-2a', ?, 'usable', ?)""", (scene, aoi, f"{date}T05:00:00Z", db.now_iso()))
    connection.execute(
        """INSERT INTO semantic_chips (id, model_key, preprocess_version, scene_id, aoi_id, acquired_at, chip_px, row_off, col_off,
               footprint, lon, lat, valid_fraction, file_sha256, embedding, provenance_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, 1.0, NULL, ?, NULL, ?)""",
        (chip_id, KEY, semantic.PREPROCESS_VERSION, scene, aoi, f"{date}T05:00:00Z", size, place_col * 112,
         json.dumps(footprint), lon + 0.005, lat + 0.005, vector_bytes, db.now_iso()))


# Places 0-2 look like A, 3-5 like B, 6-8 like C, on 4 dates each (36 chips).
PLACES = {0: "A", 1: "A", 2: "A", 3: "B", 4: "B", 5: "B", 6: "C", 7: "C", 8: "C"}
DATES = ["2023-01-10", "2023-02-10", "2024-01-10", "2024-02-10"]


def chip_id(place, date):
    return f"SCENE_{date.replace('-', '')}:112:0:{place * 112}"


@pytest.fixture
def archive(isolated, monkeypatch):
    monkeypatch.setattr(semantic, "model_problems", lambda: [])
    monkeypatch.setattr(semantic, "model_key", lambda: KEY)
    connection = db.connect()
    for place, group in PLACES.items():
        for d, date in enumerate(DATES):
            insert_chip(connection, chip_id(place, date), group_vector(group, place * 10 + d).tobytes(), place, date)
    connection.commit()
    connection.close()
    return isolated


def members(version_id=None):
    connection = discovery._connect()
    version_id = version_id or discovery._current_version_id(connection)
    rows = {r["chip_id"]: dict(r) for r in connection.execute("SELECT * FROM discovery_members WHERE version_id = ?", (version_id,))}
    connection.close()
    return rows


def cache_hash():
    connection = db.connect()
    digest = hashlib.sha256()
    for row in connection.execute("SELECT id, embedding, footprint, acquired_at FROM semantic_chips ORDER BY id"):
        digest.update(repr(tuple(row)).encode())
    connection.close()
    return digest.hexdigest()


def test_spherical_kmeans_and_silhouette_on_separated_groups():
    X = np.stack([group_vector(g, i) for i, g in enumerate("AAAAABBBBBCCCCC")])
    labels, centroids, _ = discovery.spherical_kmeans(X, 3, seed=0)
    assert len(set(labels[:5])) == len(set(labels[5:10])) == len(set(labels[10:])) == 1 and len(set(labels)) == 3
    assert np.allclose(np.linalg.norm(centroids, axis=1), 1, atol=1e-5)
    assert discovery.silhouette(X, labels) > 0.8
    assert discovery.candidate_ks(560) == [4, 6, 8, 12, 17] and discovery.candidate_ks(3) == [2]


def test_build_groups_the_archive_with_neutral_labels(archive):
    version = discovery.build(actor="test", log=lambda _: None)
    assert version["n_embeddings"] == 36 and version["n_excluded"] == 0 and version["status"] == "current"
    assert version["parameters"]["families"]["112"]["k"] == 3
    clusters = discovery.list_clusters()
    assert len(clusters) == 3 and all(c["n_places"] == 3 and c["n_scenes"] == 4 and c["size"] == 12 for c in clusters)
    rows = members()
    for place, group in PLACES.items():  # every place, on every date, in the cluster of its group
        assert len({rows[chip_id(p, d)]["cluster_id"] for p, g in PLACES.items() if g == group for d in DATES}) == 1
    for cluster in clusters:  # no fabricated semantic names
        assert re.fullmatch(r"CLUSTER \d{2} · \d\.\d\d km", cluster["label"])
        assert len(cluster["representative_ids"]) == 3
    assert version["provenance"]["kind"] == "discovery-clustering"


def test_same_input_gives_the_same_clusters(archive):
    first = discovery.build(log=lambda _: None)
    second = discovery.build(log=lambda _: None)
    assert first["input_fingerprint"] == second["input_fingerprint"] and first["id"] != second["id"]
    a, b = members(first["id"]), members(second["id"])
    assert {k: v["cluster_id"] for k, v in a.items()} == {k: v["cluster_id"] for k, v in b.items()}
    assert discovery.get_version(first["id"])["status"] == "superseded" and discovery.get_version()["id"] == second["id"]


def test_reference_to_cluster_and_ranked_places(archive):
    discovery.build(log=lambda _: None)
    reference = chip_id(1, "2024-01-10")
    result = discovery.discover(reference)
    assert result["membership"] == "member" and result["reference_chip_id"] == reference
    places = result["places"]
    assert {p["place"].split("/")[-1] for p in places} == {"0", "112", "224"}  # places 0-2 (group A)
    assert places[-1]["is_reference_place"] and not any(p["is_reference_place"] for p in places[:-1])
    scores = [p["score"] for p in places[:-1]]
    assert scores == sorted(scores, reverse=True) and all(p["dates"] == 4 for p in places)
    assert "not a semantic class" in result["note"]


def test_members_keep_metadata(archive):
    discovery.build(log=lambda _: None)
    row = members()[chip_id(4, "2023-02-10")]
    connection = db.connect()
    source = dict(connection.execute("SELECT * FROM semantic_chips WHERE id = ?", (chip_id(4, "2023-02-10"),)).fetchone())
    connection.close()
    for field in ("aoi_id", "scene_id", "acquired_at", "chip_px", "row_off", "col_off", "footprint", "lon", "lat", "model_key", "preprocess_version"):
        assert row[field] == source[field], field
    assert row["assigned_by"] == "clustering" and 0 < row["similarity"] <= 1


def test_map_geojson_has_one_footprint_per_place(archive):
    discovery.build(log=lambda _: None)
    cluster_id = members()[chip_id(6, "2023-01-10")]["cluster_id"]
    geo = discovery.places_geojson(cluster_id, chip_id(6, "2023-01-10"))
    assert len(geo["features"]) == 3 and geo["cluster_id"] == cluster_id
    for feature in geo["features"]:
        assert feature["geometry"]["type"] == "Polygon" and feature["properties"]["id"] in members()
        assert feature["properties"]["title"].startswith("CLUSTER ")
    with pytest.raises(KeyError):
        discovery.cluster_places("112-99")


def test_incremental_update_assigns_without_reclustering(archive):
    version = discovery.build(log=lambda _: None)
    before = members()
    connection = discovery._connect()
    centroids = {r["cluster_id"]: bytes(r["centroid"]) for r in connection.execute("SELECT cluster_id, centroid FROM discovery_clusters")}
    connection.close()
    connection = db.connect()
    insert_chip(connection, chip_id(0, "2025-01-10"), group_vector("A", 999).tobytes(), 0, "2025-01-10")   # fits group A
    outlier = np.zeros(DIM, dtype="float32")
    outlier[400] = 1.0
    insert_chip(connection, chip_id(9, "2025-01-10"), outlier.tobytes(), 9, "2025-01-10")                    # fits nothing
    connection.commit()
    connection.close()
    assert discovery.status()["state"] == "PARTIAL" and discovery.status()["pending"] == 2
    result = discovery.update(log=lambda _: None)
    assert result["new_chips"] == 2 and result["assigned"] == 1 and result["unassigned"] == 1 and result["version_id"] == version["id"]
    after = members()
    assert after[chip_id(0, "2025-01-10")]["assigned_by"] == "incremental"
    assert after[chip_id(0, "2025-01-10")]["cluster_id"] == before[chip_id(0, "2023-01-10")]["cluster_id"]
    assert after[chip_id(9, "2025-01-10")]["cluster_id"] is None
    assert all(after[k]["cluster_id"] == v["cluster_id"] for k, v in before.items())  # old assignments unchanged
    connection = discovery._connect()
    assert {r["cluster_id"]: bytes(r["centroid"]) for r in connection.execute("SELECT cluster_id, centroid FROM discovery_clusters")} == centroids
    connection.close()
    grown = next(c for c in discovery.list_clusters() if c["cluster_id"] == after[chip_id(0, "2025-01-10")]["cluster_id"])
    assert grown["size"] == 13 and grown["n_scenes"] == 5
    status = discovery.status()
    assert status["state"] == "PARTIAL" and any("fit no existing cluster" in r for r in status["reasons"])
    assert discovery.update(log=lambda _: None)["new_chips"] == 0  # nothing new: nothing recomputed
    # An unassigned chip still finds its nearest cluster for discovery, and says so.
    assert discovery.discover(chip_id(9, "2025-01-10"))["membership"].startswith("nearest cluster")
    rebuilt = discovery.build(log=lambda _: None)  # a re-cluster includes every chip in a new version
    assert rebuilt["n_embeddings"] == 38 and discovery.status()["state"] == "READY"


def test_embedding_version_change_requires_a_new_build(archive, monkeypatch):
    discovery.build(log=lambda _: None)
    monkeypatch.setattr(semantic, "model_key", lambda: "another-model:2")
    assert discovery.status()["state"] == "UNAVAILABLE"
    with pytest.raises(discovery.DiscoveryUnavailable):
        discovery.update(log=lambda _: None)


def test_invalid_embeddings_are_excluded(archive):
    connection = db.connect()
    insert_chip(connection, "BAD_SHORT:112:0:0", np.ones(10, dtype="float32").tobytes(), 20, "2023-01-10")
    nan = group_vector("A", 1)
    nan[3] = np.nan
    insert_chip(connection, "BAD_NAN:112:0:0", nan.tobytes(), 21, "2023-01-10")
    insert_chip(connection, "BAD_NORM:112:0:0", (group_vector("A", 2) * 3).tobytes(), 22, "2023-01-10")
    connection.commit()
    connection.close()
    version = discovery.build(log=lambda _: None)
    reasons = {e["chip_id"]: e["reason"] for e in version["excluded"]}
    assert version["n_embeddings"] == 36 and version["n_excluded"] == 3
    assert "expected 512" in reasons["BAD_SHORT:112:0:0"] and "NaN" in reasons["BAD_NAN:112:0:0"] and "unit length" in reasons["BAD_NORM:112:0:0"]
    assert not {"BAD_SHORT:112:0:0", "BAD_NAN:112:0:0", "BAD_NORM:112:0:0"} & set(members())


def test_missing_embeddings_or_model(isolated, monkeypatch):
    monkeypatch.setattr(semantic, "model_problems", lambda: [])
    monkeypatch.setattr(semantic, "model_key", lambda: KEY)
    assert discovery.status()["state"] == "NOT STAGED"
    with pytest.raises(discovery.DiscoveryUnavailable):
        discovery.build(log=lambda _: None)
    with pytest.raises(discovery.DiscoveryUnavailable):
        discovery.discover("ANY:112:0:0")
    monkeypatch.setattr(semantic, "model_problems", lambda: ["Weights not staged: test"])
    assert discovery.status()["state"] == "NOT STAGED"
    with pytest.raises(discovery.DiscoveryUnavailable):
        discovery.build(log=lambda _: None)


def test_clustering_leaves_similarity_and_btc_untouched(archive):
    before_cache = cache_hash()
    before_similar = semantic.similar([chip_id(0, "2023-01-10")], scope="all", limit=50)["results"]
    connection = db.connect()
    connection.executescript(learned.SCHEMA)
    ml_before = [connection.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("ml_change_runs", "ml_change_regions", "change_candidates")]
    connection.close()
    discovery.build(log=lambda _: None)
    discovery.update(log=lambda _: None)
    assert cache_hash() == before_cache  # embeddings are only read
    after_similar = semantic.similar([chip_id(0, "2023-01-10")], scope="all", limit=50)["results"]
    assert [(r["id"], r["score"]) for r in after_similar] == [(r["id"], r["score"]) for r in before_similar]
    connection = db.connect()
    assert [connection.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("ml_change_runs", "ml_change_regions", "change_candidates")] == ml_before
    connection.close()


def test_api(archive, monkeypatch):
    from fastapi.testclient import TestClient
    from lumon import main
    monkeypatch.setenv("LUMON_EMBEDDED_WORKER", "0")
    with TestClient(main.app) as client:
        assert client.get("/api/discovery/status").json()["state"] == "NOT STAGED"
        assert client.post("/api/discovery/discover", json={"chip_id": chip_id(0, "2023-01-10")}).status_code == 409
        assert client.post("/api/discovery/build").json()["job_id"]
        discovery.build(log=lambda _: None)
        assert len(client.get("/api/discovery/clusters").json()) == 3
        found = client.post("/api/discovery/discover", json={"chip_id": chip_id(3, "2023-01-10")}).json()
        cluster_id = found["cluster"]["cluster_id"]
        assert len(found["places"]) == 3
        assert client.get(f"/api/discovery/clusters/{cluster_id}").json()["cluster"]["cluster_id"] == cluster_id
        assert len(client.get(f"/api/discovery/clusters/{cluster_id}/places.geojson").json()["features"]) == 3
        by_point = client.post("/api/discovery/discover", json={"at": {"lon": 73.035, "lat": 19.005}}).json()
        assert by_point["reference_chip_id"].endswith(":112:0:336")  # place 3, latest date
        assert client.post("/api/discovery/discover", json={"chip_id": "NOT_A_CHIP:1:2:3"}).status_code == 404
        assert client.get("/api/discovery/evaluation").json()["families"]["112"]["clusters"] == 3
        monkeypatch.setattr(semantic, "model_problems", lambda: ["Weights not staged: test"])
        assert client.post("/api/discovery/build").status_code == 409
