import { useEffect, useState } from "react";
import { getJson, postJson } from "../../api";
import { formatBytes, formatTime } from "../../utils/format";
import { Facts, PanelState, Section } from "../intel/common";
import type { SystemInfo } from "../../types";

// ---------------------------------------------------------------------------
// SYSTEM STATUS and DATA & PROVENANCE.
//   system: mode, storage footprint, models, audit integrity, evaluation
//   data:   the source manifest (every dataset/API/font with licence and
//           checksum) and the GeoPackage export
// Every number shown is read from the backend; nothing is estimated.
// ---------------------------------------------------------------------------
type Evaluation = {
  status?: string; generated_at?: string; hardware?: Record<string, unknown>;
  query_latency_ms?: Record<string, { median_ms: number; max_ms: number; results: number }>;
  change_analysis?: Record<string, { seconds: number }>;
  labels?: { precision_at_k: unknown; false_alarms_per_100km2: unknown; reviewed: number };
  earliest_date_error?: unknown; decision_time?: unknown;
};

export function SystemSection({ system }: { system: SystemInfo | null }) {
  const [evaluation, setEvaluation] = useState<Evaluation | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  useEffect(() => {
    getJson<Evaluation>("/api/evaluation/latest").then(setEvaluation).catch(() => setEvaluation(null));
  }, []);
  if (!system) return <PanelState state="error" text="LUMON API UNREACHABLE" />;

  const storageRows = Object.entries(system.storage_bytes).map(([key, bytes]) => [key.toUpperCase(), formatBytes(bytes)] as [string, string]);
  const total = Object.values(system.storage_bytes).reduce((a, b) => a + b, 0);

  return (
    <>
      <Section title="OPERATING MODE">
        <Facts rows={[
          ["MODE", system.mode === "connected" ? "CONNECTED INGEST — sources may download" : "AIR-GAPPED — no external requests"],
          ["SWITCH", "Set LUMON_MODE in .env.local and restart the API"],
          ["AUDIT CHAIN", system.audit.ok ? `INTACT (${system.audit.entries} entries)` : `BROKEN at ${system.audit.broken_at}`],
          ["JOBS ACTIVE", system.counts.jobs_queued],
        ]} />
      </Section>
      <Section title="MODELS">
        {system.models.map((model) => (
          <div key={model.name} className="model-row">
            <div className="model-row__name">{model.name}</div>
            <div className="model-row__meta"><span className="result-kind result-kind--muted">{model.status}</span>{model.retrieval_status ? <span className="result-kind result-kind--muted">INDEX {model.retrieval_status}</span> : null} fallback: {model.fallback}</div>
          </div>
        ))}
      </Section>
      <Section title="STORAGE" meta={formatBytes(total)}><Facts rows={storageRows} /></Section>
      <Section title="EVALUATION" meta={evaluation?.generated_at ? formatTime(evaluation.generated_at) : "not run"}>
        {!evaluation || evaluation.status === "not run" ? (
          <div className="empty-note">Not run yet. Command: python -m lumon.cli evaluate</div>
        ) : (
          <Facts rows={[
            ["CHANGE RUN", Object.entries(evaluation.change_analysis ?? {}).map(([k, v]) => `${k}: ${v.seconds}s`).join(", ")],
            ...Object.entries(evaluation.query_latency_ms ?? {}).map(([q, v]) => [`“${q}”`, `${v.median_ms} ms median · ${v.results} results`] as [string, string]),
            ["PRECISION@K", typeof evaluation.labels?.precision_at_k === "string" ? evaluation.labels.precision_at_k : JSON.stringify(evaluation.labels?.precision_at_k)],
            ["FALSE ALARMS", typeof evaluation.labels?.false_alarms_per_100km2 === "string" ? evaluation.labels.false_alarms_per_100km2 : JSON.stringify(evaluation.labels?.false_alarms_per_100km2)],
            ["DATE ERROR", typeof evaluation.earliest_date_error === "string" ? evaluation.earliest_date_error : JSON.stringify(evaluation.earliest_date_error)],
            ["DECISION TIME", typeof evaluation.decision_time === "string" ? evaluation.decision_time : JSON.stringify(evaluation.decision_time)],
          ]} />
        )}
        <button type="button" className="action-button" onClick={() => postJson<{ job_id: number }>("/api/jobs", { kind: "evaluation", params: {} }).then((j) => setMessage(`Evaluation queued (job ${j.job_id}).`)).catch(() => setMessage("Not queued."))}>RUN EVALUATION</button>
        {message && <div className="notice">{message}</div>}
      </Section>
    </>
  );
}

type Manifest = {
  boundaries: Record<string, { name: string; provider: string; dataset: string; license: string; attribution: string; retrieved_at: string; raw_sha256: string; feature_count: number; source_version: string | null }>;
  sources: { id: string; name: string; provider: string; license: string; attribution: string; last_success: string | null; last_snapshot_sha256: string | null; health: string }[];
  imagery: { collection: string; provider: string; license: string; scenes: number; first: string | null; last: string | null };
  fonts: { name: string; license: string; source: string; retrieved: string }[];
  models: { name: string; status: string }[];
};

export function DataSection() {
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [exportResult, setExportResult] = useState<{ download: string; layers: Record<string, number> } | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    getJson<Manifest>("/api/manifest").then(setManifest).catch(() => setError(true));
  }, []);
  if (error) return <PanelState state="error" text="LUMON API UNREACHABLE" />;
  if (!manifest) return <PanelState state="loading" />;

  return (
    <>
      <Section title="EXPORT">
        <button type="button" className="action-button" onClick={() => postJson<{ download: string; layers: Record<string, number> }>("/api/export/geopackage", {}).then(setExportResult).catch(() => setExportResult(null))}>
          EXPORT GEOPACKAGE
        </button>
        {exportResult && (
          <div className="notice notice--ok">
            Written: {Object.entries(exportResult.layers).map(([k, v]) => `${k} ${v}`).join(" · ")}. <a className="text-link" href={exportResult.download}>Download .gpkg</a>
          </div>
        )}
      </Section>
      <Section title="BOUNDARY DATASETS">
        {Object.values(manifest.boundaries).map((b) => (
          <div key={b.name} className="manifest-row">
            <div className="manifest-row__name">{b.name}</div>
            <Facts rows={[["PROVIDER", b.provider], ["DATASET", b.dataset], ["VERSION", b.source_version], ["LICENSE", b.license], ["RETRIEVED", formatTime(b.retrieved_at)], ["SHA-256", <code title={b.raw_sha256}>{b.raw_sha256.slice(0, 16)}…</code>], ["FEATURES", b.feature_count]]} />
          </div>
        ))}
      </Section>
      <Section title="SATELLITE IMAGERY">
        <Facts rows={[["COLLECTION", manifest.imagery.collection], ["PROVIDER", manifest.imagery.provider], ["LICENSE", manifest.imagery.license], ["SCENES", manifest.imagery.scenes], ["RANGE", `${formatTime(manifest.imagery.first)} → ${formatTime(manifest.imagery.last)}`]]} />
      </Section>
      <Section title="OSINT SOURCES">
        {manifest.sources.map((s) => (
          <div key={s.id} className="manifest-row">
            <div className="manifest-row__name">{s.name}</div>
            <div className="manifest-row__meta">{s.provider} · {s.license} · last snapshot {s.last_snapshot_sha256 ? s.last_snapshot_sha256.slice(0, 12) + "…" : "none"}</div>
          </div>
        ))}
      </Section>
      <Section title="FONTS / MODELS">
        {manifest.fonts.map((f) => <div key={f.name} className="manifest-row__meta">{f.name} · {f.license} · {f.source} · {f.retrieved}</div>)}
        {manifest.models.map((m) => <div key={m.name} className="manifest-row__meta">{m.name} · {m.status}</div>)}
      </Section>
    </>
  );
}
