# Discovery & embedding-based clustering (PS 26227 §2.2.4): evaluation (2026-10-05)

Real archive only: the demo AOI (Navi Mumbai), 63 usable Sentinel-2 scenes,
2,118 cached RemoteCLIP chip embeddings. **No accuracy is claimed**: there
are no labels, and a cluster is an embedding-based grouping, not a class.
Clusters have neutral names (`CLUSTER 07 · 1.12 km`).

## 1. What was inspected first (Phase 1)

| Item | Finding |
|---|---|
| Storage | `semantic_chips.embedding`: float32 BLOB, **512 values**, L2-normalised (norm 1.000000 for all), one model key (`remoteclip:60014e395d930a3f`) and one preprocessing version |
| Size in memory | 2,118 × 512 × 4 B = **4.3 MB** (full cosine matrix 18 MB) — safe |
| Chips | 224 px (2.24 km): 560 · 112 px (1.12 km): 1,558 |
| Places | **34** chip windows (9 + 25), 60–63 dates each, one AOI |
| Linkage kept per chip | AOI, scene id, acquisition date, pixel window, footprint (WGS84), centre lon/lat |
| Existing index | primary key only; exact cosine search in numpy (no vector index needed at this size) |
| Libraries | numpy available; scipy / sklearn / hdbscan / faiss **not installed** — none added |
| Structure | raw embeddings: 63–65 % of a chip's 10 nearest neighbours are the same place on other dates, 10–12 % other places on the same date (haze/date effect). Removing each scene's mean ("scene-centring") cut the date effect to 2–4 % but pushed same-place to 76–86 %, so clusters would collapse onto single places; raw embeddings were kept. |

## 2. Method (and why)

**Spherical k-means** (k-means with cosine similarity on unit vectors), plain
numpy, run separately per chip size (as image similarity does). k is chosen
per family from `round(sqrt(n/2) × {0.25, 0.35, 0.5, 0.7, 1.0})` by the
highest mean cosine silhouette; k-means++ seeding with seeds 0–4, best
objective kept; chips in id order → **deterministic**.

Why: no new dependency; under 1 s for the whole archive; centroids allow
new chips to be assigned without re-clustering (Phase 6); a DBSCAN/graph
method would need tuned density thresholds on a weakly structured space and
would not give an assignment rule for new chips.

Result (version `disc-v002-0ebf8f33`): **2,118 embeddings → 18 clusters**
(2.24 km: k = 4, silhouette 0.215; 1.12 km: k = 14, silhouette 0.191), all
34 places represented, 0 embeddings excluded, 0.5 s, ~130 MB peak memory.

| Family | k tried → silhouette |
|---|---|
| 2.24 km | 4 → 0.215 · 6 → 0.185 · 8 → 0.198 · 12 → 0.185 · 17 → 0.184 |
| 1.12 km | 7 → 0.179 · 10 → 0.174 · 14 → 0.191 · 20 → 0.190 · 28 → 0.177 |

All silhouettes are low (≈ 0.2): **the archive has weak cluster structure**.
The k choice is weakly supported (neighbouring k score almost the same).

## 3. Structural metrics

| Metric | 2.24 km | 1.12 km |
|---|---|---|
| Chips / places / clusters | 560 / 9 / 4 | 1,558 / 25 / 14 |
| Cluster size min / median / max | 117 / 143 / 157 | 55 / 81 / 193 |
| Places per cluster min / median / max | 8 / 8 / 9 | 5 / 9 / 16 |
| Single-place clusters (place leakage) | 0 | 0 |
| Cross-date consistency (share of a place's dates in its most common cluster) | 0.48 | 0.57 |
| 10 nearest neighbours in the same cluster | 0.83 (chance 0.25) | 0.79 (chance 0.09) |
| … counting only neighbours at other places | 0.80 | 0.68 |
| Mean member–centroid cosine (compactness) | 0.926–0.944 | 0.907–0.948 |

Clusters agree with nearest neighbours far above chance, including across
places — they do group different locations. A place splits over several
clusters through time (consistency ~0.5), which here mostly follows the
airport's construction stages.

Per-cluster flags (from `npm run lumon -- discovery-eval`):
- **date-driven**: 224-02 holds 56 % of the windows of its dates;
- **season-driven**: 224-01 is 63 % post-monsoon dates (archive 38 %);
  112-14 only 5 % (14 dates, mostly May);
- **one dominant place**: 112-09 (85 % one place), 112-11 (78 %),
  112-12 (66 %), 112-07 (58 %), 112-13 (57 %).

## 4. Manual inspection (representatives + 7 members per cluster)

| Verdict | Clusters | What the images show |
|---|---|---|
| Useful cross-place groups | 224-02, 224-03, 112-01, 112-02, 112-04, 112-05, 112-10 | built airport complex (terminal, runway, aprons) across several windows 2023–2026; bright orange earthworks 2019–2021; bare open ground across 13 places; grey built blocks; creek with embankment; creek crossing open ground |
| Haze / atmosphere-driven | 224-01, 112-03, 112-13 | muted, hazy or dark dates of different content |
| Season / clipping-driven | 112-14 | bright white creek-side patches (surfaces clipped by the fixed stretch), mostly May dates; weakest compactness (0.907) and a mixed tail |
| One dominant place (stable place, little discovery value) | 112-09, 112-11, 112-12, 112-07 | an urban corner, a village, a creek meander seen on most dates |
| Weak / mixed | 224-04, 112-06, 112-08 | mixed creek/vegetation/fill edges, villages with roads, assorted edges |

## 5. Determinism and incremental updates (real data)

- **Determinism**: two builds of the same 2,118 embeddings gave the identical
  cluster for **2,118 / 2,118** chips (same input fingerprint).
- **Incremental test** (on a scratch copy of the database; the real archive
  was not modified): clustered only the 1,914 chips before 2026, then added
  the 204 chips of 2026 with `update()`: **195 assigned, 9 unassigned (4 %)**
  in 28 ms. Pair agreement (Rand index) of those assignments with a full
  re-cluster of all chips: **0.98** (2.24 km), **0.89** (1.12 km).

What is recomputed:

| Operation | Recomputed | Not recomputed |
|---|---|---|
| New imagery → `semantic-index` | embeddings of the NEW chips only (existing cache) | all existing embeddings |
| `discovery-update` | cosine of each new chip to the frozen centroids of its family; counts of the touched clusters | old assignments, centroids, other clusters, embeddings |
| `discovery-build` | k selection and k-means over all embeddings → a new version (old one kept, superseded) | embeddings (read only) |

**Limitation:** this is incremental *assignment*, not incremental
re-clustering. New kinds of imagery cannot form new clusters until the next
build; chips outside every cluster's radius stay UNASSIGNED and the status
says PARTIAL.

## 6. PS 26227 §2.2.4 mapping

| Requirement | Implementation |
|---|---|
| Identify one interesting location/image | any chip: a semantic-search result, an image-similarity result, a cluster member on the map, or an imagery tile clicked on the map → DISCOVER CLUSTER |
| Discover other locations with comparable characteristics | the chip's cluster → its other member **places**, each with how many of its dates fall in the cluster, ranked by similarity to the reference, thumbnails, SHOW CLUSTER ON MAP |
| Without a new query for each site | clusters are pre-computed for the whole archive; exploring a group needs no text and no new search |
| More than nearest-neighbour similarity | archive-wide grouping into versioned clusters with members, representatives, places, extent, compactness and provenance; browsing clusters needs no reference at all (Imagery → DISCOVERY / CLUSTERS) |
| Incremental addition of imagery | embeddings incremental; cluster assignment incremental with frozen centroids; explicit versions for re-clustering |

**IMAGE SIMILARITY**: reference → ranked nearest results (a list that only
exists for that query).
**DISCOVERY / CLUSTERING**: archive → groups of related locations (stored,
versioned) → the analyst explores a group, starting from any member or from
the cluster list.

## 7. Multi-AOI readiness

Every member stores its AOI, scene id, date, footprint and embedding version
(model key + preprocessing version). All AOIs of a chip size are clustered
together, places are keyed by (AOI, size, window), and clusters count AOIs.
A future real AOI only needs ingesting and indexing; `discovery-update`
assigns its chips, `discovery-build` re-clusters across AOIs. No additional
AOI was fabricated.

## 8. Costs

No dependency added. Disk: ~1.1 MB per version in SQLite (`discovery_*`
tables; two versions stored = 2.2 MB). Memory: ~130 MB peak for a build.
Time: 0.5 s per build, ~30 ms per update of 200 chips, ~100 ms per discovery
request.

## 9. Limitations

- Groupings, not classes; never named after objects or activities.
- Weak structure (silhouette ≈ 0.2); some clusters follow haze, season,
  clipping or one dominant place (listed above).
- One small AOI: the 2.24 km family has only 9 places, so its clusters
  contain almost all of them — on the map a 2.24 km cluster covers most of
  the AOI; the 1.12 km family (25 places) is more discriminating.
- Results are within the RemoteCLIP embedding at 10 m, with the same
  resolution and haze limits as semantic search.
