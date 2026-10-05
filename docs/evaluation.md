# Evaluation

```bash
npm run lumon -- evaluate          # writes data/evaluation/<time>.json and latest.json
```

The report also appears in SYSTEM → System status → Evaluation.
**Only measured values are reported.** When a metric needs labels that do
not exist yet, the report says *not measurable* and why.

## Metrics

| Metric | How it is measured | Needs |
|---|---|---|
| Query latency | 6 fixed questions, each parsed and run 5 times; median and max in ms | staged data |
| Change-analysis time | wall time of one full change-engine run per AOI (cached tile observations reused) | staged imagery |
| Storage footprint | bytes in the database, boundaries, reference layers, snapshots, imagery, raw downloads | — |
| Hardware | platform, CPU count, memory (from the OS) | — |
| Precision@K | among the top-K accepted candidates **that analysts reviewed**, the share confirmed or relabelled | analyst decisions |
| False alarms per 100 km² | rejected accepted-candidates ÷ AOI area | analyst decisions |
| Earliest-date error | days between `earliest_supported_after` and an independently known date | `config/evaluation/reference_dates.json` |
| Decision time | seconds from opening the evidence to the decision (audit log) | decisions made in the UI |

Unreviewed candidates are never assumed correct or incorrect.

### Reference dates file

Create `config/evaluation/reference_dates.json` when independent dates are
known (for example from a construction permit or a ground visit):

```json
{ "changes": [ { "candidate_id": "chg-…", "reference_date": "2022-11-15", "source": "describe the evidence" } ] }
```

## From score to probability (calibration plan)

The change score is **uncalibrated** ([change-detection.md](change-detection.md)).
Once at least about 50 reviewed candidates exist:

1. export decisions (GeoPackage `decisions` layer, or the `decisions` table);
2. fit a monotone calibration (isotonic regression or Platt scaling) of
   score against confirmed/rejected;
3. report reliability (calibration curve) and Brier score on a held-out
   part;
4. only then label the number a probability in the UI.

## Measured on the development machine (2026-10-04)

Apple M4 (`arm64`, 10 CPUs, 16 GB). From `data/evaluation/latest.json`:

- Change analysis (`demo-01`, 63 scenes, 2,160 tiles, cached observations): **0.74–0.76 s** over repeated runs
- Query latency, median: 0.4–63 ms across the 6 fixed queries (the
  change search with the water relation is the slowest)
- Precision@K, false alarms, earliest-date error, decision time:
  **not measurable yet** (no analyst decisions or reference dates
  recorded)

Re-run `evaluate` after reviewing candidates to get label-based metrics.

## SIH 26227 evaluation harness (manifest-driven)

`server/lumon/ps_evaluation.py`. One command, read-only, offline:

```bash
LUMON_MODE=airgapped npm run lumon -- evaluate --manifest config/evaluation/ps26227/lumon-demo01-internal-v1.json
npm run lumon -- evaluate --manifest <manifest> --seal   # after editing: bump manifest_version, then seal
```

Plain `npm run lumon -- evaluate` (above) is unchanged.

**Manifest** (`config/evaluation/ps26227/*.json`): `manifest_id`, `manifest_version`,
`created_at/by`, `creation_method`, `ground_truth_source`, `dataset` (AOI, sensor,
source, date range, every scene with its file SHA-256), `splits` (development /
evaluation / held-out with status), `semantic.queries` (text, split, k, relevance
label slot), `similarity.references` (chip, scope, relevance slot),
`rule_change.labels` (before/after scene, tile ids, change yes/no, class, direction,
known date, source, status), `btc_b.pairs` (scene pair, mask label slot),
`discovery.incremental_check`, `changelog`, `content_sha256`.

**Labels.** A label counts only when its status is `verified` and it names its
`source` (who established it and how). Relevance judgements: `{chip_id: 1|0}`;
`exhaustive: true` enables recall. Change labels are per tile set and scene pair.
BTC-B labels are change masks (GeoTIFF on the run's grid, SHA-256 in the manifest).
Pending queries get a `judgement_pool` (top-20 chips) in the report for analysts.

**Integrity (the run is refused, not degraded):** missing fields; content edited
without re-sealing; a reported version whose content changed (bump the version);
scenes not in the archive, with another checksum or no provenance; chips not in the
current embedding index; duplicate ids or scene pairs; the same chip or scene pair in
two splits (leakage); verified labels without a source; a BTC-B mask whose checksum
or grid differs; the embedding index or discovery version changing during the run.

**Metrics** (computed only from verified labels): precision@k, recall@k (exhaustive
sets), average precision (exhaustive or judged-depth, named as such); rule-based
change precision/recall/F1 per labelled site, class agreement, earliest-date error;
BTC-B pixel precision/recall/F1/IoU from masks. Reported separately and labelled:
gate SUPPRESSION STATISTICS (not precision), the OSCD BENCHMARK (not the archive),
STRUCTURAL cluster quality and the incremental-vs-full re-clustering agreement
(computed on a temporary database copy).

**Output:** `data/evaluation/ps26227/<manifest_id>/v<version>/<UTC>.json` and `.md`
(+ `latest.*`); the Markdown is generated from the JSON. Each run adds a provenance
record (kind `ps-evaluation`, input = manifest checksum) and an audit entry. The
report lists hardware, software versions, models with verified weight checksums,
storage, label counts, held-out status, every NOT AVAILABLE metric with its reason,
and the claims it does not support.

**Current manifest (`lumon-demo01-internal` v1.1.0):** 63 demo-01 scenes, 6 queries
(incl. the two PS examples, annotated with what RemoteCLIP cannot model), 2
similarity references, 5 BTC-B pairs, discovery hold-out from 2026-01-01. **All label
slots are pending** — no human labels exist — so every accuracy-type metric is NOT
AVAILABLE and the held-out split is PENDING. The evaluation split is internal (the
same scenes were used in development).
