"""
Command-line entry point for Lumon backend tasks.

Run from the `server/` folder, for example:
    ../.venv/bin/python -m lumon.cli stage-boundaries

Each command is a small function below; `main` picks one by name.
"""

import json
import sys

from . import settings


def cmd_stage_boundaries(args):
    """Download and process the India boundary datasets (CONNECTED mode)."""
    from .staging import boundaries
    for result in boundaries.stage_all():
        if result["status"] == "staged":
            print(f"  staged {result['id']}: {result['feature_count']} features, {result['output_bytes'] // 1024} KB")


def _print_run(summary):
    """Print a one-line summary of a source refresh."""
    line = f"  {summary['source_id']:24} {summary['status']:17} in={summary['records_in']:<6} kept={summary['records_kept']:<6} outside={summary['records_outside']:<6} invalid={summary['records_invalid']}"
    if summary.get("invalid_reasons"):
        line += f" {summary['invalid_reasons']}"
    if summary.get("error"):
        line += f"  ({summary['error']})"
    print(line)


def cmd_refresh(args):
    """Refresh one OSINT source: refresh <source_id>."""
    from . import ingest
    _print_run(ingest.refresh_source(args[0]))


def cmd_refresh_all(args):
    """Refresh every enabled OSINT source in the registry."""
    from . import ingest
    from .sources import registry
    for source in registry.load_definitions():
        if source.get("enabled"):
            _print_run(ingest.refresh_source(source["id"]))


def cmd_ingest_imagery(args):
    """Stage Sentinel-2 scenes for an AOI: ingest-imagery <aoi_id> [max_new_scenes]."""
    from .imagery import archive
    aoi_id = args[0] if args else "demo-01"
    limit = int(args[1]) if len(args) > 1 else None
    print(json.dumps(archive.ingest_aoi(aoi_id, max_new_scenes=limit), indent=2))


def cmd_ingest_local(args):
    """Register local GeoTIFF/COG imagery offline: ingest-local <file|folder> --aoi ID [options]."""
    from .imagery import local_ingest
    usage = ("usage: ingest-local <file.tif|folder> --aoi ID [--acquired YYYY-MM-DD] [--sensor NAME] [--level TEXT]\n"
             "         [--bands B02=1,B03=2,B04=3,B08=4,B11=5,SCL=6] [--reflectance-offset 0|-0.1] [--scene-id ID]\n"
             "         [--aoi-name NAME] [--index]   (--index: then run the incremental semantic index + discovery update)")
    if not args or args[0].startswith("--"):
        print(usage)
        sys.exit(1)
    path, options, rest = args[0], {}, args[1:]
    flags = {"--aoi": "aoi_id", "--acquired": "acquired", "--sensor": "sensor", "--level": "level", "--bands": "bands",
             "--reflectance-offset": "reflectance_offset", "--scene-id": "scene_id", "--aoi-name": "aoi_name"}
    index_after = False
    while rest:
        flag = rest.pop(0)
        if flag == "--index":
            index_after = True
        elif flag in flags and rest:
            options[flags[flag]] = rest.pop(0)
        else:
            print(f"  unknown or incomplete option {flag}\n{usage}")
            sys.exit(1)
    if "reflectance_offset" in options:
        options["reflectance_offset"] = float(options["reflectance_offset"])
    aoi_id = options.pop("aoi_id", None)
    try:
        reports = local_ingest.ingest_path(path, aoi_id, **options)
    except local_ingest.LocalIngestError as error:
        print(f"  LOCAL INGEST FAILED\n  Reason: {error}")
        sys.exit(2)
    for report in reports:
        print(f"  {report['outcome']}: {report.get('path')}")
        for key in ("scene_id", "aoi_id", "sha256", "reason", "quality_status", "quarantine_reason", "view_path",
                    "acquired_at", "acquired_source", "sensor", "band_mapping", "radiometric_offset", "radiometric_offset_source"):
            if report.get(key) not in (None, "", {}):
                print(f"    {key:28} {report[key]}")
        for capability, state in (report.get("compatibility") or {}).items():
            print(f"    {capability:28} {state}")
    if index_after and any(r["outcome"] == "INGESTED" for r in reports):
        from .imagery import discovery, semantic
        for aoi in sorted({r["aoi_id"] for r in reports if r["outcome"] == "INGESTED"}):
            semantic.index(aoi, actor="cli")
        if discovery.get_version() is not None:
            discovery.update(actor="cli")
    if any(r["outcome"] == "LOCAL INGEST FAILED" for r in reports):
        sys.exit(2)


def cmd_analyse(args):
    """Run the change engine for an AOI: analyse <aoi_id>."""
    from .change import engine
    engine.run(args[0] if args else "demo-01")


def cmd_evaluate(args):
    """Run the evaluation harness (data/evaluation/latest.json), or the SIH 26227 harness: evaluate --manifest <file> [--seal]."""
    if "--manifest" in args:
        from . import ps_evaluation
        position = args.index("--manifest")
        if position + 1 >= len(args):
            print("usage: evaluate --manifest <manifest.json> [--seal]")
            sys.exit(1)
        manifest = args[position + 1]
        if "--seal" in args:
            print(f"  sealed {manifest}: content_sha256 {ps_evaluation.seal(manifest)}")
            return
        try:
            report = ps_evaluation.run(manifest)
        except ps_evaluation.EvaluationIntegrityError as error:
            print(f"  {error}")
            sys.exit(2)
        print(f"  {report['manifest']['manifest_id']} v{report['manifest']['manifest_version']}: "
              f"semantic {report['semantic_retrieval']['status']} · similarity {report['image_similarity']['status']} · "
              f"rule-based change {report['rule_based_change']['status']} · BTC-B {report['btc_b']['status']} · discovery {report['discovery'].get('status')}")
        print(f"  held-out: {report['held_out']['status']} · {len(report['not_available'])} metrics NOT AVAILABLE · {report['evaluation']['runtime_s']} s")
        for file in report["evaluation"].get("report_files", []):
            print(f"  wrote {file}")
        return
    from . import evaluation
    report = evaluation.run_all()
    print(json.dumps({k: report[k] for k in ("counts", "labels", "earliest_date_error", "decision_time")}, indent=2, default=str))


def cmd_export(args):
    """Write a GeoPackage of events, changes, scenes, decisions and provenance."""
    from . import export
    print(json.dumps(export.export_geopackage(), indent=2))


def cmd_verify_audit(args):
    """Verify the hash chain of the audit log."""
    from . import audit, db
    connection = db.connect()
    result = audit.verify_chain(connection)
    print(json.dumps(result))
    sys.exit(0 if result["ok"] else 2)


def cmd_worker(args):
    """Run the job worker as a separate process (Ctrl+C to stop)."""
    from . import worker
    print("  worker running; waiting for jobs")
    worker.run_forever()


def cmd_recheck_radiometry(args):
    """Re-decide and cross-check each scene's reflectance offset: recheck-radiometry <aoi_id>."""
    from .imagery import archive
    print(archive.recheck_radiometry(args[0] if args else "demo-01"))


def cmd_semantic_index(args):
    """Embed staged image chips with RemoteCLIP (cached): semantic-index [aoi_id]."""
    from .imagery import semantic
    summary = semantic.index(args[0] if args else None, actor="cli")
    for item in summary["excluded_scenes"]:
        print(f"  excluded {item['scene_id']}: {item['reason']}")


def cmd_semantic_search(args):
    """Rank image chips by RemoteCLIP similarity to a text: semantic-search "<text>" [limit]."""
    from .imagery import semantic
    result = semantic.search(args[0], limit=int(args[1]) if len(args) > 1 else 10)
    print(f"  {result['chips_searched']} chips from {result['scenes_searched']} scenes · {result['timing_ms']} ms")
    print(f"  score = {result['score_kind']}; distribution {result['score_distribution']}")
    for item in result["results"]:
        print(f"  #{item['rank']:<3} {item['score']:.4f}  {item['acquired_at'][:10]}  {item['chip_km']:.2f} km  "
              f"{item['lat']:.4f},{item['lon']:.4f}  {item['id']}")


def cmd_semantic_eval(args):
    """Run the documented semantic-search evaluation queries (proxy agreement, no accuracy claim)."""
    from .imagery import semantic_eval
    report = semantic_eval.run()
    print(f"  model {report['model_key']} · {report['preprocess_version']} · top/bottom {report['top_n']}")
    for q in report["queries"]:
        proxy = (f"{q['proxy']}: top {q['proxy_top_mean']:.3f} / archive {q['proxy_archive_mean']:.3f} / bottom {q['proxy_bottom_mean']:.3f}"
                 f" -> {'agrees' if q['proxy_agrees'] else 'DOES NOT AGREE'}") if q.get("proxy") else "no proxy (visual review only)"
        print(f"  {q['query']:30} top {q['score_top']:.3f} median {q['score_median']:.3f} · {proxy}")


def cmd_discovery_build(args):
    """Cluster the cached RemoteCLIP embeddings into a new discovery version."""
    from .imagery import discovery
    version = discovery.build(actor="cli")
    for family, info in version["parameters"]["families"].items():
        print(f"  {int(family) * 10 / 1000:.2f} km chips: {info['n']} -> k={info['k']} (silhouette {info['silhouette']}; tried {info['candidates']})")


def cmd_discovery_update(args):
    """Assign newly embedded chips to the current discovery clusters (incremental, centroids frozen)."""
    from .imagery import discovery
    discovery.update(actor="cli")


def cmd_discovery_eval(args):
    """Structural evaluation of the current discovery clusters (no labels, no accuracy)."""
    import json
    from .imagery import discovery
    report = discovery.evaluate()
    for family, metrics in report["families"].items():
        print(f"  {family} px: {json.dumps(metrics)}")
    for cluster_id, metrics in report["clusters"].items():
        print(f"    {cluster_id}: {json.dumps(metrics)}")


def cmd_ml_change_pairs(args):
    """List pairs suitable for learned change detection: ml-change-pairs [aoi_id] [limit]."""
    from .change_ml import learned
    for pair in learned.suggest_pairs(args[0] if args else None, int(args[1]) if len(args) > 1 else 20):
        print(f"  {pair['before_date'][:10]} -> {pair['after_date'][:10]}  {pair['before_scene_id']}  {pair['after_scene_id']}")


def cmd_ml_change_run(args):
    """Run BTC-B on a pair (model-generated candidates): ml-change-run <before_scene_id> <after_scene_id>."""
    from .change_ml import learned
    try:
        run = learned.run_pair(args[0], args[1], actor="cli")
    except learned.PairRejected as error:
        print(f"  PAIR REFUSED: {'; '.join(error.problems)}")
        return
    print(f"  {run['label']}S: {run['regions_reported']} regions (+{run['regions_too_small']} below {learned.MIN_REGION_PX} px), "
          f"{run['candidate_pixels']}/{run['clear_pixels']} clear pixels flagged, {run['seconds']} s")
    print(f"  warnings: {run['checks']['warnings'] or 'none'}")
    print(f"  outputs: {run['probability_path']}, {run['mask_path']}")


def cmd_verify_offline(args):
    """Verify the whole backend works with the network blocked (Stage 11)."""
    from . import offline_check
    report = offline_check.run()
    for item in report["checks"]:
        print(f"  {item['result']:4}  {item['check']:42} {item['ms']:>6} ms  {item['detail']}")
    print(f"\n  {report['passed']} passed, {report['failed']} failed; blocked connection attempts: {report['blocked_connections'] or 'none'}")
    print(f"  ran on: {report['ran_on']} (deleted afterwards; real source health unchanged)")
    sys.exit(0 if report["failed"] == 0 and not report["blocked_connections"] else 2)


def cmd_write_manifest(args):
    """Write docs/SOURCE_MANIFEST.md from what is actually staged (dates, checksums)."""
    from fastapi.testclient import TestClient
    from . import main
    import os
    os.environ["LUMON_EMBEDDED_WORKER"] = "0"
    with TestClient(main.app) as client:
        manifest = client.get("/api/manifest").json()
    lines = ["# Source manifest", "",
             "Generated by `python -m lumon.cli write-manifest` from the data staged on this machine.",
             "Retrieval times and SHA-256 checksums are those of the staged copies in `data/` (not committed).", "",
             "## Boundary and reference geography", "",
             "| Dataset | Provider | Version | License | Retrieved | SHA-256 (raw download) | Features |",
             "|---|---|---|---|---|---|---|"]
    for item in manifest["boundaries"].values():
        lines.append(f"| {item['name']} | {item['provider']} | {(item.get('source_version') or '—').replace(chr(10), ' ')} | {item['license']} | {item['retrieved_at']} | `{item['raw_sha256'][:16]}…` | {item['feature_count']} |")
    lines += ["", "## OSINT and reference sources", "",
              "| Source | Provider | License | Last success | Last snapshot SHA-256 | Health |", "|---|---|---|---|---|---|"]
    for item in manifest["sources"]:
        sha = f"`{item['last_snapshot_sha256'][:16]}…`" if item.get("last_snapshot_sha256") else "—"
        lines.append(f"| {item['name']} | {item['provider']} | {item['license']} | {item.get('last_success') or '—'} | {sha} | {item['health']} |")
    imagery = manifest["imagery"]
    lines += ["", "## Satellite imagery", "",
              f"- Collection: {imagery['collection']} ({imagery['provider']})",
              f"- License: {imagery['license']}",
              f"- Usable scenes staged: {imagery['scenes']} ({imagery['first']} → {imagery['last']})",
              "- Per-scene checksums and product identifiers: `scenes` table / GeoPackage export `scenes` layer.", "",
              "## Fonts", ""]
    for font in manifest["fonts"]:
        lines.append(f"- {font['name']} — {font['license']} — {font['source']} (retrieved {font['retrieved']})")
    lines += ["", "## Models", ""]
    for model in manifest["models"]:
        lines.append(f"- {model['name']}: **{model['status']}** (fallback: {model['fallback']})")
    path = settings.ROOT_DIR / "docs" / "SOURCE_MANIFEST.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    print(f"  wrote {path.relative_to(settings.ROOT_DIR)}")


COMMANDS = {
    "ingest-imagery": cmd_ingest_imagery,
    "ingest-local": cmd_ingest_local,
    "write-manifest": cmd_write_manifest,
    "verify-offline": cmd_verify_offline,
    "recheck-radiometry": cmd_recheck_radiometry,
    "analyse": cmd_analyse,
    "evaluate": cmd_evaluate,
    "export": cmd_export,
    "verify-audit": cmd_verify_audit,
    "worker": cmd_worker,
    "stage-boundaries": cmd_stage_boundaries,
    "refresh": cmd_refresh,
    "refresh-all": cmd_refresh_all,
    "semantic-index": cmd_semantic_index,
    "semantic-search": cmd_semantic_search,
    "semantic-eval": cmd_semantic_eval,
    "ml-change-pairs": cmd_ml_change_pairs,
    "discovery-build": cmd_discovery_build,
    "discovery-update": cmd_discovery_update,
    "discovery-eval": cmd_discovery_eval,
    "ml-change-run": cmd_ml_change_run,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print("Usage: python -m lumon.cli <command> [args]\n\nCommands:")
        for name, function in COMMANDS.items():
            print(f"  {name:22} {function.__doc__.strip().splitlines()[0]}")
        sys.exit(1)
    print(f"[lumon] mode={settings.operating_mode()}")
    from .net import OfflineError
    try:
        COMMANDS[sys.argv[1]](sys.argv[2:])
    except OfflineError as error:
        # Expected in air-gapped mode: explain instead of printing a traceback.
        print(f"  refused: {error}")
        sys.exit(3)


if __name__ == "__main__":
    main()
