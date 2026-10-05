"""
Discovery and embedding-based clustering (PS 26227 §2.2.4).

IMAGE SIMILARITY (lumon.imagery.semantic.similar):
    one reference chip -> a ranked list of its nearest chips.
DISCOVERY (this module):
    the whole archive -> groups of chips whose RemoteCLIP embeddings lie
    close together -> the analyst picks any chip and explores the group it
    belongs to: which other places, which dates, how similar to the reference.

A cluster is an EMBEDDING-BASED GROUPING, not a semantic label. Clusters get
neutral names ("CLUSTER 07 · 1.12 km") and never a class name such as
"airport" or "construction".

INPUT: the cached RemoteCLIP chip embeddings (table semantic_chips). Nothing
is re-embedded and no model is loaded: 512 float32 numbers per chip,
L2-normalised, ~4 MB for the current 2,118 chips. Embeddings that are not
512-long, not finite or not unit length are excluded and reported.

METHOD: spherical k-means (k-means on the unit sphere, i.e. with cosine
similarity), in plain numpy:
  - run separately per chip size ("family": 224 px = 2.24 km, 112 px =
    1.12 km), because chips of different sizes show different fields of
    view (the same rule as image similarity)
  - all AOIs of a family are clustered together, so future AOIs can form
    cross-AOI groups without any change here
  - k is chosen per family from a size-scaled list of candidates by the
    highest mean silhouette (cosine distance); the table of candidates and
    their silhouettes is stored with the version
  - k-means++ seeding with fixed seeds (5 restarts, the best objective is
    kept), chips in id order: the same input always gives the same clusters
Why this method: deterministic, no new dependency (scipy/sklearn are not
installed), fast at this size (well under a second per family), and its
centroids allow new chips to be assigned without re-clustering.

VERSIONS AND INCREMENTAL UPDATES (exactly what is recomputed):
  build()   new version: clusters ALL current embeddings (k-means from
            scratch, embeddings only read). The previous version is kept,
            marked superseded.
  update()  same version, centroids FROZEN: only chips embedded since the
            version was built are compared with the centroids of their
            family (one dot product per centroid). A new chip joins the
            nearest cluster if it is within that cluster's radius (its least
            similar original member, or mean − 3 SD if lower); otherwise it
            stays UNASSIGNED.
            Cluster counts (size, places, dates, extent) are recounted from
            the members. No old assignment changes, no centroid moves.
  This is incremental ASSIGNMENT, not incremental re-clustering: new kinds
  of imagery cannot create new clusters until the next build(), and the
  status turns PARTIAL while chips are unassigned or not yet processed.
"""

import hashlib
import json
import math
import time
from collections import Counter, defaultdict

import numpy as np

from .. import audit, db, provenance
from . import semantic

METHOD = "spherical-kmeans"
METHOD_VERSION = "discovery-skmeans-v1"
SEEDS = (0, 1, 2, 3, 4)          # k-means++ restarts; best objective kept
MAX_ITERATIONS = 100
CANDIDATE_FACTORS = (0.25, 0.35, 0.5, 0.7, 1.0)  # x sqrt(n/2): the k values tried per family
REPRESENTATIVES = 3              # chips closest to the centroid, from distinct places
DIMENSION = 512
NOTE = ("A cluster is a group of image chips whose RemoteCLIP embeddings are close together. "
        "It is not a semantic class: membership does not mean the same objects or activity are present.")

SCHEMA = """
CREATE TABLE IF NOT EXISTS discovery_versions (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,              -- current | superseded
    model_key TEXT NOT NULL,           -- embedding index version (model)
    preprocess_version TEXT NOT NULL,  -- embedding index version (chips)
    method TEXT NOT NULL,
    method_version TEXT NOT NULL,
    parameters TEXT NOT NULL,          -- JSON: seeds, candidates, chosen k and silhouettes per family
    input_fingerprint TEXT NOT NULL,   -- SHA-256 of the clustered chip ids + embeddings
    n_embeddings INTEGER NOT NULL,
    n_excluded INTEGER NOT NULL,
    n_clusters INTEGER NOT NULL,
    incremental_added INTEGER NOT NULL DEFAULT 0,
    incremental_unassigned INTEGER NOT NULL DEFAULT 0,
    seconds REAL,
    provenance_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS discovery_clusters (
    version_id TEXT NOT NULL,
    cluster_id TEXT NOT NULL,          -- e.g. "112-07"
    label TEXT NOT NULL,               -- neutral: "CLUSTER 07 · 1.12 km"
    family_px INTEGER NOT NULL,
    number INTEGER NOT NULL,
    centroid BLOB NOT NULL,            -- float32, unit length (frozen for the version)
    radius REAL NOT NULL,              -- assignment threshold for new chips (see assignment_radius)
    size INTEGER NOT NULL,
    n_places INTEGER NOT NULL,
    n_scenes INTEGER NOT NULL,
    n_aois INTEGER NOT NULL,
    bbox TEXT NOT NULL,                -- JSON [w, s, e, n] of member footprints
    first_date TEXT,
    last_date TEXT,
    representative_ids TEXT NOT NULL,  -- JSON list of chip ids
    mean_similarity REAL NOT NULL,     -- mean member-to-centroid cosine (compactness)
    PRIMARY KEY (version_id, cluster_id)
);
CREATE TABLE IF NOT EXISTS discovery_members (
    version_id TEXT NOT NULL,
    chip_id TEXT NOT NULL,
    cluster_id TEXT,                   -- NULL = unassigned (incremental chip outside every radius)
    similarity REAL,                   -- cosine to its cluster centroid (or to the nearest one if unassigned)
    assigned_by TEXT NOT NULL,         -- clustering | incremental
    assigned_at TEXT NOT NULL,
    aoi_id TEXT NOT NULL,
    scene_id TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    chip_px INTEGER NOT NULL,
    row_off INTEGER NOT NULL,
    col_off INTEGER NOT NULL,
    footprint TEXT NOT NULL,
    lon REAL NOT NULL,
    lat REAL NOT NULL,
    model_key TEXT NOT NULL,
    preprocess_version TEXT NOT NULL,
    PRIMARY KEY (version_id, chip_id)
);
CREATE INDEX IF NOT EXISTS discovery_members_cluster ON discovery_members (version_id, cluster_id);
"""


class DiscoveryUnavailable(RuntimeError):
    """No usable embeddings, model not staged, or no clustering version built yet."""


def _connect():
    connection = db.connect()
    connection.executescript(SCHEMA)
    return connection


# ---------------------------------------------------------------------------
# Embeddings (read only)
# ---------------------------------------------------------------------------

def load_embeddings(connection, chip_ids: list[str] | None = None) -> tuple[list[dict], np.ndarray, list[dict]]:
    """
    Cached chips of the current embedding version, in chip-id order, as
    (rows, matrix, excluded). Invalid vectors are excluded with a reason.
    """
    sql = """SELECT id, aoi_id, scene_id, acquired_at, chip_px, row_off, col_off, footprint, lon, lat, embedding
             FROM semantic_chips WHERE model_key = ? AND preprocess_version = ?"""
    params = [semantic.model_key(), semantic.PREPROCESS_VERSION]
    if chip_ids is not None:
        sql += f" AND id IN ({','.join('?' * len(chip_ids))})"
        params += chip_ids
    rows, vectors, excluded = [], [], []
    for row in connection.execute(sql + " ORDER BY id", params):
        blob = row["embedding"]
        if blob is None or len(blob) != DIMENSION * 4:
            excluded.append({"chip_id": row["id"], "reason": f"embedding has {0 if blob is None else len(blob) // 4} values, expected {DIMENSION}"})
            continue
        vector = np.frombuffer(blob, dtype="float32")
        if not np.isfinite(vector).all():
            excluded.append({"chip_id": row["id"], "reason": "embedding contains NaN or infinity"})
            continue
        if abs(float(np.linalg.norm(vector)) - 1.0) > 1e-3:
            excluded.append({"chip_id": row["id"], "reason": "embedding is not unit length"})
            continue
        record = dict(row)
        record.pop("embedding")
        rows.append(record)
        vectors.append(vector)
    matrix = np.stack(vectors) if vectors else np.zeros((0, DIMENSION), dtype="float32")
    return rows, matrix, excluded


def _place(row) -> tuple:
    """A place = the same chip window of the same AOI (all its dates)."""
    return (row["aoi_id"], row["chip_px"], row["row_off"], row["col_off"])


# ---------------------------------------------------------------------------
# Clustering (numpy only)
# ---------------------------------------------------------------------------

def spherical_kmeans(X: np.ndarray, k: int, seed: int) -> tuple[np.ndarray, np.ndarray, float]:
    """
    k-means with cosine similarity on unit vectors. k-means++ seeding from
    `seed`; returns (labels, unit centroids, objective = sum of member-to-
    centroid cosines). Deterministic for the same X, k and seed.
    """
    rng = np.random.default_rng(seed)
    n = len(X)
    centres = [X[int(rng.integers(n))]]
    for _ in range(1, k):
        distance = np.clip(1 - np.max(X @ np.array(centres).T, axis=1), 0, None) ** 2
        total = distance.sum()
        index = int(rng.choice(n, p=distance / total)) if total > 0 else int(rng.integers(n))
        centres.append(X[index])
    C = np.array(centres, dtype="float64")
    labels = None
    for _ in range(MAX_ITERATIONS):
        new = np.argmax(X @ C.T, axis=1)
        if labels is not None and np.array_equal(new, labels):
            break
        labels = new
        for j in range(k):
            members = X[labels == j]
            if len(members):
                mean = members.mean(axis=0)
                C[j] = mean / np.linalg.norm(mean)
    objective = float(np.sum(np.max(X @ C.T, axis=1)))
    return labels, C.astype("float32"), objective


def silhouette(X: np.ndarray, labels: np.ndarray) -> float:
    """Mean silhouette with cosine distance (1 = tight, separate clusters; ~0 = overlapping)."""
    distance = 1 - X @ X.T
    clusters = sorted(set(labels.tolist()))
    if len(clusters) < 2:
        return 0.0
    mean_to = np.stack([distance[:, labels == c].mean(axis=1) for c in clusters], axis=1)
    sizes = np.array([(labels == c).sum() for c in clusters])
    own = np.array([clusters.index(l) for l in labels])
    a = mean_to[np.arange(len(X)), own] * sizes[own] / np.maximum(sizes[own] - 1, 1)  # exclude self (distance 0)
    others = mean_to.copy()
    others[np.arange(len(X)), own] = np.inf
    b = others.min(axis=1)
    s = np.where(sizes[own] > 1, (b - a) / np.maximum(np.maximum(a, b), 1e-12), 0.0)
    return float(s.mean())


def candidate_ks(n: int) -> list[int]:
    """k values tried for a family of n chips (scaled with sqrt(n/2), at least 2, below n)."""
    base = math.sqrt(n / 2)
    return sorted({k for k in (max(2, round(base * f)) for f in CANDIDATE_FACTORS) if k < n})


def cluster_family(X: np.ndarray) -> dict:
    """Choose k by silhouette and cluster; returns labels, centroids, k, and the candidate table."""
    table, best = [], None
    for k in candidate_ks(len(X)):
        labels, centroids, objective = max((spherical_kmeans(X, k, seed) for seed in SEEDS), key=lambda r: r[2])
        score = silhouette(X, labels)
        table.append({"k": k, "silhouette": round(score, 4)})
        if best is None or score > best[0] + 1e-9:
            best = (score, k, labels, centroids)
    return {"k": best[1], "silhouette": round(best[0], 4), "labels": best[2], "centroids": best[3], "candidates": table}


# ---------------------------------------------------------------------------
# Building and updating versions
# ---------------------------------------------------------------------------

def _require_staged():
    problems = semantic.model_problems()
    if problems:
        raise DiscoveryUnavailable("RemoteCLIP is not staged: " + "; ".join(problems))


def _fingerprint(rows: list[dict], X: np.ndarray) -> str:
    digest = hashlib.sha256()
    for row, vector in zip(rows, X):
        digest.update(row["id"].encode())
        digest.update(vector.tobytes())
    return digest.hexdigest()


def _cluster_stats(connection, version_id: str, cluster_id: str) -> dict:
    """Size, places, dates, AOIs and extent of a cluster, counted from its members."""
    members = [dict(r) for r in connection.execute(
        "SELECT * FROM discovery_members WHERE version_id = ? AND cluster_id = ?", (version_id, cluster_id))]
    ring_points = [p for m in members for p in json.loads(m["footprint"])["coordinates"][0]]
    return {
        "size": len(members), "n_places": len({_place(m) for m in members}), "n_scenes": len({m["scene_id"] for m in members}),
        "n_aois": len({m["aoi_id"] for m in members}),
        "bbox": json.dumps([round(min(p[0] for p in ring_points), 6), round(min(p[1] for p in ring_points), 6),
                            round(max(p[0] for p in ring_points), 6), round(max(p[1] for p in ring_points), 6)]),
        "first_date": min(m["acquired_at"] for m in members), "last_date": max(m["acquired_at"] for m in members),
    }


def assignment_radius(similarity: np.ndarray) -> float:
    """
    Lowest centroid similarity a NEW chip may have to join a cluster: the
    least similar original member, widened to mean − 3 standard deviations
    when that is lower. (Members are slightly closer to a centroid computed
    from themselves than new chips of the same kind are, so the strict
    minimum would wrongly turn such chips away.)
    """
    return float(min(similarity.min(), similarity.mean() - 3 * similarity.std()))


def build(actor: str = "system", log=print) -> dict:
    """Cluster every current embedding into a NEW version (the old one is kept as superseded)."""
    _require_staged()
    started = time.perf_counter()
    connection = _connect()
    rows, X, excluded = load_embeddings(connection)
    if not rows:
        connection.close()
        raise DiscoveryUnavailable("no indexed chip embeddings (run the semantic index first)")
    fingerprint = _fingerprint(rows, X)
    # Sequence number + input fingerprint: unique even for two builds in the same second.
    number = connection.execute("SELECT COUNT(*) FROM discovery_versions").fetchone()[0] + 1
    version_id = f"disc-v{number:03d}-{fingerprint[:8]}"
    now = db.now_iso()
    families = sorted({r["chip_px"] for r in rows}, reverse=True)
    parameters = {"seeds": list(SEEDS), "max_iterations": MAX_ITERATIONS, "candidate_factors": list(CANDIDATE_FACTORS),
                  "selection": "highest mean cosine silhouette", "families": {}}
    clusters = []
    for family in families:
        index = [i for i, r in enumerate(rows) if r["chip_px"] == family]
        result = cluster_family(X[index])
        parameters["families"][str(family)] = {"n": len(index), "k": result["k"], "silhouette": result["silhouette"],
                                               "candidates": result["candidates"]}
        # Number clusters by size (largest = 01), ties by first member id: deterministic.
        groups = defaultdict(list)
        for position, label in zip(index, result["labels"]):
            groups[int(label)].append(position)
        order = sorted(groups, key=lambda label: (-len(groups[label]), rows[groups[label][0]]["id"]))
        for number, label in enumerate(order, start=1):
            centroid = result["centroids"][label]
            similarity = X[groups[label]] @ centroid
            clusters.append({"family": family, "number": number, "positions": groups[label], "centroid": centroid,
                             "similarity": similarity, "radius": assignment_radius(similarity)})
    connection.execute("UPDATE discovery_versions SET status = 'superseded' WHERE status = 'current'")
    for cluster in clusters:
        cluster_id = f"{cluster['family']}-{cluster['number']:02d}"
        for position, similarity in zip(cluster["positions"], cluster["similarity"]):
            r = rows[position]
            connection.execute(
                """INSERT INTO discovery_members (version_id, chip_id, cluster_id, similarity, assigned_by, assigned_at, aoi_id, scene_id,
                       acquired_at, chip_px, row_off, col_off, footprint, lon, lat, model_key, preprocess_version)
                   VALUES (?, ?, ?, ?, 'clustering', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (version_id, r["id"], cluster_id, round(float(similarity), 5), now, r["aoi_id"], r["scene_id"], r["acquired_at"],
                 r["chip_px"], r["row_off"], r["col_off"], r["footprint"], r["lon"], r["lat"], semantic.model_key(), semantic.PREPROCESS_VERSION))
        # Representatives: most central members, one per place.
        representatives, seen = [], set()
        for k in np.argsort(-cluster["similarity"]):
            r = rows[cluster["positions"][int(k)]]
            if _place(r) not in seen:
                seen.add(_place(r))
                representatives.append(r["id"])
            if len(representatives) == REPRESENTATIVES:
                break
        stats = _cluster_stats(connection, version_id, cluster_id)
        connection.execute(
            """INSERT INTO discovery_clusters (version_id, cluster_id, label, family_px, number, centroid, radius, size, n_places, n_scenes,
                   n_aois, bbox, first_date, last_date, representative_ids, mean_similarity) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (version_id, cluster_id, f"CLUSTER {cluster['number']:02d} · {cluster['family'] * 10 / 1000:.2f} km", cluster["family"],
             cluster["number"], cluster["centroid"].astype("float32").tobytes(), round(cluster["radius"], 5),
             stats["size"], stats["n_places"], stats["n_scenes"], stats["n_aois"], stats["bbox"], stats["first_date"], stats["last_date"],
             json.dumps(representatives), round(float(cluster["similarity"].mean()), 5)))
    seconds = round(time.perf_counter() - started, 2)
    provenance_id = provenance.create(
        connection, kind="discovery-clustering", source_id=semantic.MODEL_ID, input_ref=f"semantic_chips ({len(rows)} embeddings)",
        input_sha256=fingerprint, processing=f"{METHOD} per chip size on cached RemoteCLIP embeddings",
        processing_version=METHOD_VERSION, parameters=parameters)
    connection.execute(
        """INSERT INTO discovery_versions (id, status, model_key, preprocess_version, method, method_version, parameters, input_fingerprint,
               n_embeddings, n_excluded, n_clusters, seconds, provenance_id, created_at) VALUES (?, 'current', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (version_id, semantic.model_key(), semantic.PREPROCESS_VERSION, METHOD, METHOD_VERSION, json.dumps(parameters), fingerprint,
         len(rows), len(excluded), len(clusters), seconds, provenance_id, now))
    connection.commit()
    audit.record(connection, actor, "discovery-build", version_id, {"embeddings": len(rows), "excluded": len(excluded),
                                                                     "clusters": len(clusters), "seconds": seconds})
    connection.close()
    log(f"discovery: {len(clusters)} clusters from {len(rows)} embeddings ({len(excluded)} excluded) in {seconds} s -> {version_id}")
    return {**get_version(version_id), "excluded": excluded}


def update(actor: str = "system", log=print) -> dict:
    """
    Incremental assignment of chips embedded since the current version was
    built (centroids frozen; see the module notes). Returns counts.
    """
    _require_staged()
    connection = _connect()
    version = connection.execute("SELECT * FROM discovery_versions WHERE status = 'current'").fetchone()
    if version is None:
        connection.close()
        raise DiscoveryUnavailable("no clustering version yet: run build() first")
    version = dict(version)
    if (version["model_key"], version["preprocess_version"]) != (semantic.model_key(), semantic.PREPROCESS_VERSION):
        connection.close()
        raise DiscoveryUnavailable("the embedding index version changed: a new build() is required")
    known = {r[0] for r in connection.execute("SELECT chip_id FROM discovery_members WHERE version_id = ?", (version["id"],))}
    all_ids = [r[0] for r in connection.execute(
        "SELECT id FROM semantic_chips WHERE model_key = ? AND preprocess_version = ?", (semantic.model_key(), semantic.PREPROCESS_VERSION))]
    new_ids = sorted(set(all_ids) - known)
    rows, X, excluded = load_embeddings(connection, new_ids) if new_ids else ([], np.zeros((0, DIMENSION), "float32"), [])
    clusters = [dict(r) for r in connection.execute("SELECT * FROM discovery_clusters WHERE version_id = ?", (version["id"],))]
    by_family = defaultdict(list)
    for c in clusters:
        by_family[c["family_px"]].append(c)
    now = db.now_iso()
    assigned = unassigned = 0
    touched = set()
    for row, vector in zip(rows, X):
        family = by_family.get(row["chip_px"], [])
        cluster_id, similarity = None, None
        if family:
            sims = [float(np.frombuffer(c["centroid"], dtype="float32") @ vector) for c in family]
            best = int(np.argmax(sims))
            similarity = sims[best]
            if similarity >= family[best]["radius"]:
                cluster_id = family[best]["cluster_id"]
        if cluster_id:
            assigned += 1
            touched.add(cluster_id)
        else:
            unassigned += 1
        connection.execute(
            """INSERT INTO discovery_members (version_id, chip_id, cluster_id, similarity, assigned_by, assigned_at, aoi_id, scene_id,
                   acquired_at, chip_px, row_off, col_off, footprint, lon, lat, model_key, preprocess_version)
               VALUES (?, ?, ?, ?, 'incremental', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (version["id"], row["id"], cluster_id, None if similarity is None else round(similarity, 5), now, row["aoi_id"], row["scene_id"],
             row["acquired_at"], row["chip_px"], row["row_off"], row["col_off"], row["footprint"], row["lon"], row["lat"],
             semantic.model_key(), semantic.PREPROCESS_VERSION))
    for cluster_id in touched:
        stats = _cluster_stats(connection, version["id"], cluster_id)
        connection.execute(
            """UPDATE discovery_clusters SET size = ?, n_places = ?, n_scenes = ?, n_aois = ?, bbox = ?, first_date = ?, last_date = ?
               WHERE version_id = ? AND cluster_id = ?""",
            (stats["size"], stats["n_places"], stats["n_scenes"], stats["n_aois"], stats["bbox"], stats["first_date"], stats["last_date"],
             version["id"], cluster_id))
    connection.execute(
        "UPDATE discovery_versions SET incremental_added = incremental_added + ?, incremental_unassigned = incremental_unassigned + ?, updated_at = ? WHERE id = ?",
        (assigned, unassigned, now, version["id"]))
    connection.commit()
    summary = {"version_id": version["id"], "new_chips": len(new_ids), "assigned": assigned, "unassigned": unassigned,
               "excluded": excluded, "recomputed": "cosine of each new chip to the frozen centroids of its family; counts of the touched clusters"}
    audit.record(connection, actor, "discovery-update", version["id"], {k: v for k, v in summary.items() if k != "excluded"} | {"excluded": len(excluded)})
    connection.close()
    log(f"discovery update: {len(new_ids)} new chips, {assigned} assigned, {unassigned} unassigned, {len(excluded)} excluded")
    return summary


# ---------------------------------------------------------------------------
# Reading: versions, clusters, reference-based discovery, map
# ---------------------------------------------------------------------------

def _current_version_id(connection) -> str | None:
    row = connection.execute("SELECT id FROM discovery_versions WHERE status = 'current'").fetchone()
    return row[0] if row else None


def get_version(version_id: str | None = None) -> dict | None:
    connection = _connect()
    version_id = version_id or _current_version_id(connection)
    row = connection.execute("SELECT * FROM discovery_versions WHERE id = ?", (version_id,)).fetchone() if version_id else None
    if row is None:
        connection.close()
        return None
    version = db.row_to_dict(row, ["parameters"])
    version["provenance"] = provenance.get(connection, version["provenance_id"]) if version["provenance_id"] else None
    version["note"] = NOTE
    connection.close()
    return version


def _cluster_record(row) -> dict:
    record = dict(row)
    record.pop("centroid", None)
    record["bbox"] = json.loads(record["bbox"])
    record["representative_ids"] = json.loads(record["representative_ids"])
    return record


def list_clusters(version_id: str | None = None, family_px: int | None = None) -> list[dict]:
    connection = _connect()
    version_id = version_id or _current_version_id(connection)
    sql = "SELECT * FROM discovery_clusters WHERE version_id = ?"
    params = [version_id]
    if family_px:
        sql += " AND family_px = ?"
        params.append(family_px)
    rows = connection.execute(sql + " ORDER BY family_px DESC, number", params).fetchall()
    connection.close()
    return [_cluster_record(r) for r in rows]


def _embedding(connection, chip_id: str) -> np.ndarray | None:
    rows, X, _ = load_embeddings(connection, [chip_id])
    return X[0] if rows else None


def cluster_places(cluster_id: str, reference_chip_id: str | None = None, version_id: str | None = None) -> dict:
    """
    One cluster's members grouped by place (all dates of a place together),
    each place represented by its member most similar to the reference chip
    (or to the centroid without a reference), places ranked by that score.
    """
    connection = _connect()
    version_id = version_id or _current_version_id(connection)
    cluster = connection.execute("SELECT * FROM discovery_clusters WHERE version_id = ? AND cluster_id = ?", (version_id, cluster_id)).fetchone()
    if cluster is None:
        connection.close()
        raise KeyError(cluster_id)
    members = [dict(r) for r in connection.execute(
        "SELECT * FROM discovery_members WHERE version_id = ? AND cluster_id = ? ORDER BY chip_id", (version_id, cluster_id))]
    rows, X, _ = load_embeddings(connection, [m["chip_id"] for m in members])
    reference = _embedding(connection, reference_chip_id) if reference_chip_id else None
    connection.close()
    vectors = {r["id"]: v for r, v in zip(rows, X)}
    query = reference if reference is not None else np.frombuffer(cluster["centroid"], dtype="float32")
    places = defaultdict(list)
    for member in members:
        vector = vectors.get(member["chip_id"])
        member["score"] = round(float(vector @ query), 4) if vector is not None else None
        places[_place(member)].append(member)
    reference_place = None
    if reference_chip_id:
        ref = next((m for m in members if m["chip_id"] == reference_chip_id), None)
        reference_place = _place(ref) if ref else None
    summary = []
    for place, items in places.items():
        best = max(items, key=lambda m: -1 if m["score"] is None else m["score"])
        ring = json.loads(best["footprint"])["coordinates"][0]
        summary.append({
            "place": "/".join(str(v) for v in place), "aoi_id": place[0], "chip_px": place[1],
            "best_chip_id": best["chip_id"], "scene_id": best["scene_id"], "acquired_at": best["acquired_at"],
            "score": best["score"], "dates": len(items), "first_date": min(i["acquired_at"] for i in items),
            "last_date": max(i["acquired_at"] for i in items), "footprint": json.loads(best["footprint"]),
            "bbox": [min(p[0] for p in ring), min(p[1] for p in ring), max(p[0] for p in ring), max(p[1] for p in ring)],
            "lon": best["lon"], "lat": best["lat"], "is_reference_place": place == reference_place,
        })
    summary.sort(key=lambda p: (p["is_reference_place"], -(p["score"] or -1)))
    return {"cluster": _cluster_record(cluster), "version_id": version_id, "reference_chip_id": reference_chip_id,
            "score_kind": "cosine similarity to the reference chip" if reference is not None else "cosine similarity to the cluster centroid",
            "places": summary, "note": NOTE}


def discover(chip_id: str) -> dict:
    """
    Reference chip -> its cluster -> the other member places ranked by
    similarity to the reference. A chip that is not a member (unassigned, or
    newer than the version) is matched to its nearest centroid and marked so.
    """
    _require_staged()
    connection = _connect()
    version_id = _current_version_id(connection)
    if version_id is None:
        connection.close()
        raise DiscoveryUnavailable("no clustering version yet: build discovery clusters first")
    member = connection.execute("SELECT cluster_id, similarity FROM discovery_members WHERE version_id = ? AND chip_id = ?",
                                (version_id, chip_id)).fetchone()
    membership = "member"
    cluster_id = member["cluster_id"] if member else None
    if cluster_id is None:
        vector = _embedding(connection, chip_id)
        if vector is None:
            connection.close()
            raise KeyError(chip_id)
        chip_px = connection.execute("SELECT chip_px FROM semantic_chips WHERE id = ? LIMIT 1", (chip_id,)).fetchone()[0]
        family = connection.execute("SELECT cluster_id, centroid FROM discovery_clusters WHERE version_id = ? AND family_px = ?",
                                    (version_id, chip_px)).fetchall()
        if not family:
            connection.close()
            raise DiscoveryUnavailable(f"no clusters for {chip_px} px chips in this version")
        best = max(family, key=lambda c: float(np.frombuffer(c["centroid"], dtype="float32") @ vector))
        cluster_id = best["cluster_id"]
        membership = "nearest cluster (unassigned chip)" if member else "nearest cluster (chip newer than this version)"
    connection.close()
    result = cluster_places(cluster_id, chip_id, version_id)
    result["membership"] = membership
    return result


def places_geojson(cluster_id: str, reference_chip_id: str | None = None) -> dict:
    """The cluster's places as GeoJSON for the map (one footprint per place)."""
    detail = cluster_places(cluster_id, reference_chip_id)
    label = detail["cluster"]["label"]
    return {"type": "FeatureCollection", "cluster_id": cluster_id, "features": [{
        "type": "Feature", "geometry": place["footprint"],
        "properties": {"id": place["best_chip_id"], "cluster_id": cluster_id, "score": place["score"], "dates": place["dates"],
                       "is_reference_place": place["is_reference_place"],
                       "title": f"{label} member · {place['dates']} dates · best {place['acquired_at'][:10]}"
                                + (f" · similarity {place['score']:.3f}" if place["score"] is not None else "")},
    } for place in detail["places"]]}


def status() -> dict:
    """
    NOT STAGED   RemoteCLIP not staged, no embeddings, or no version built yet
    INDEXING     a discovery job is queued or running
    PARTIAL      chips embedded after the version are not yet processed, or
                 some incremental chips stayed unassigned (re-cluster advised)
    READY        every current embedding is a member of the current version
    UNAVAILABLE  the embedding index version changed since the build
    """
    connection = _connect()
    version = connection.execute("SELECT * FROM discovery_versions WHERE status = 'current'").fetchone()
    embeddings = connection.execute("SELECT COUNT(*) FROM semantic_chips WHERE model_key = ? AND preprocess_version = ?",
                                    (semantic.model_key(), semantic.PREPROCESS_VERSION)).fetchone()[0]
    job = connection.execute("SELECT status FROM jobs WHERE kind IN ('discovery-build', 'discovery-update') AND status IN ('queued','running') LIMIT 1").fetchone()
    members = connection.execute("SELECT COUNT(*) FROM discovery_members WHERE version_id = ?", (version["id"],)).fetchone()[0] if version else 0
    connection.close()
    problems = semantic.model_problems()
    pending = max(embeddings - members, 0)
    if problems:
        state, reasons = "NOT STAGED", ["RemoteCLIP is not staged: " + "; ".join(problems)]
    elif version is not None and (version["model_key"], version["preprocess_version"]) != (semantic.model_key(), semantic.PREPROCESS_VERSION):
        state, reasons = "UNAVAILABLE", ["embedding index version changed since the build: re-index, then BUILD CLUSTERS again"]
    elif embeddings == 0:
        state, reasons = "NOT STAGED", ["no indexed chip embeddings (run the semantic index first)"]
    elif job:
        state, reasons = "INDEXING", [f"discovery job {job['status']}"]
    elif version is None:
        state, reasons = "NOT STAGED", ["no clustering version yet (BUILD CLUSTERS)"]
    elif pending or version["incremental_unassigned"]:
        reasons = []
        if pending:
            reasons.append(f"{pending} newer chip embeddings not processed yet (UPDATE)")
        if version["incremental_unassigned"]:
            reasons.append(f"{version['incremental_unassigned']} new chips fit no existing cluster (re-cluster to include them)")
        state = "PARTIAL"
    else:
        state, reasons = "READY", []
    return {"state": state, "reasons": reasons, "method": METHOD, "method_version": METHOD_VERSION, "embeddings": embeddings,
            "pending": pending, "version": dict(version) | {"parameters": json.loads(version["parameters"])} if version else None,
            "note": NOTE}


# ---------------------------------------------------------------------------
# Structural evaluation (no labels, no accuracy claim)
# ---------------------------------------------------------------------------

def evaluate(version_id: str | None = None, neighbours: int = 10) -> dict:
    """
    Measurable properties of a clustering version, per chip-size family:
      clusters, size distribution, places per cluster, single-place clusters
      (place leakage), cross-date consistency (share of a place's dates in
      its most common cluster), nearest-neighbour consistency (share of each
      chip's k nearest chips in the same cluster, also counting only
      neighbours at OTHER places), date concentration (clusters holding most
      chips of the dates they contain: haze/date-driven), season
      concentration (share of post-monsoon Oct–Dec dates vs the archive),
      compactness (mean member-to-centroid cosine) and silhouette.
    """
    connection = _connect()
    version_id = version_id or _current_version_id(connection)
    members = {r["chip_id"]: dict(r) for r in connection.execute(
        "SELECT * FROM discovery_members WHERE version_id = ? AND cluster_id IS NOT NULL", (version_id,))}
    rows, X, _ = load_embeddings(connection, sorted(members))
    clusters = {r["cluster_id"]: dict(r) for r in connection.execute("SELECT * FROM discovery_clusters WHERE version_id = ?", (version_id,))}
    connection.close()
    report = {"version_id": version_id, "families": {}, "clusters": {}}
    for family in sorted({r["chip_px"] for r in rows}, reverse=True):
        index = [i for i, r in enumerate(rows) if r["chip_px"] == family]
        F = X[index]
        R = [rows[i] for i in index]
        labels = np.array([members[r["id"]]["cluster_id"] for r in R])
        ids = sorted(set(labels.tolist()))
        sizes = [int((labels == c).sum()) for c in ids]
        places = {c: {_place(r) for r, l in zip(R, labels) if l == c} for c in ids}
        by_place = defaultdict(list)
        for r, l in zip(R, labels):
            by_place[_place(r)].append(l)
        consistency = float(np.mean([Counter(v).most_common(1)[0][1] / len(v) for v in by_place.values()]))
        similarity = F @ F.T
        np.fill_diagonal(similarity, -np.inf)
        place_of = [_place(r) for r in R]
        nn_same, nn_other = [], []
        for i in range(len(R)):
            order = np.argsort(-similarity[i])
            nn_same.append(np.mean(labels[order[:neighbours]] == labels[i]))
            others = [j for j in order if place_of[j] != place_of[i]][:neighbours]
            nn_other.append(np.mean(labels[others] == labels[i]))
        scene_total = Counter(r["scene_id"] for r in R)
        archive_post_monsoon = np.mean([r["acquired_at"][5:7] in ("10", "11", "12") for r in R])
        for c in ids:
            in_c = [r for r, l in zip(R, labels) if l == c]
            scene_counts = Counter(r["scene_id"] for r in in_c)
            coverage = float(np.mean([scene_counts[s] / scene_total[s] for s in scene_counts]))
            months = Counter(int(r["acquired_at"][5:7]) for r in in_c)
            report["clusters"][c] = {
                "label": clusters[c]["label"], "size": len(in_c), "places": len(places[c]), "scenes": len(scene_counts),
                "date_coverage": round(coverage, 3),  # high = holds most windows of its dates (date-driven)
                "post_monsoon_share": round(sum(v for m, v in months.items() if m in (10, 11, 12)) / len(in_c), 3),
                "compactness": clusters[c]["mean_similarity"],
                "top_place_share": round(max(Counter(_place(r) for r in in_c).values()) / len(in_c), 3),
            }
        report["families"][str(family)] = {
            "chips": len(R), "places": len(by_place), "clusters": len(ids),
            "size_min_median_max": [min(sizes), int(np.median(sizes)), max(sizes)],
            "places_per_cluster_min_median_max": [min(len(p) for p in places.values()), int(np.median([len(p) for p in places.values()])),
                                                  max(len(p) for p in places.values())],
            "single_place_clusters": sum(1 for p in places.values() if len(p) == 1),
            "cross_date_consistency": round(consistency, 3),
            f"nn{neighbours}_same_cluster": round(float(np.mean(nn_same)), 3),
            f"nn{neighbours}_other_places_same_cluster": round(float(np.mean(nn_other)), 3),
            "chance_same_cluster": round(float(sum((s / len(R)) ** 2 for s in sizes)), 3),
            "silhouette": round(silhouette(F, np.array([ids.index(l) for l in labels])), 4),
            "archive_post_monsoon_share": round(float(archive_post_monsoon), 3),
        }
    return report


# The capability registry (lumon/pilot.py) reads a runtime's model and model
# state from these names; discovery uses RemoteCLIP's cached embeddings.
MODEL_ID = semantic.MODEL_ID


def model_problems() -> list[str]:
    return semantic.model_problems()
