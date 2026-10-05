import type { ReactNode } from "react";
import InfoTip from "../InfoTip";
import type { InfoContent } from "../InfoTip";
import { formatAge, formatTime } from "../../utils/format";

// ---------------------------------------------------------------------------
// Small building blocks shared by the intelligence panel's detail views.
// ---------------------------------------------------------------------------

// A titled block inside the panel. `info` adds a small ⓘ explanation
// next to the title (only for sections that are not self-explanatory).
export function Section({ title, meta, info, children }: { title: string; meta?: ReactNode; info?: InfoContent; children: ReactNode }) {
  return (
    <section className="intel-section">
      <div className="intel-section__header">
        <span className="intel-section__title">{title}{info && <InfoTip {...info} />}</span>
        {meta !== undefined && <span className="intel-section__meta">{meta}</span>}
      </div>
      <div className="intel-section__content">{children}</div>
    </section>
  );
}

// A list of label/value facts. Missing values show "—", never a guess.
export function Facts({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="facts">
      {rows.map(([label, value]) => (
        <div className="facts__row" key={label}>
          <dt>{label}</dt>
          <dd>{value === null || value === undefined || value === "" ? "—" : value}</dd>
        </div>
      ))}
    </dl>
  );
}

// The evidence type badge: OBSERVED / INFERRED / GIS-DERIVED / VERIFIED RECORD / UNVERIFIABLE.
export function EvidenceTag({ type }: { type: string | null | undefined }) {
  if (!type) return null;
  const key = type.split(" ")[0].toLowerCase().replace(/[^a-z-]/g, "");
  return <span className={`evidence-tag evidence-tag--${key}`}>{type}</span>;
}

// Loading / error / empty states, so no panel is ever just blank.
export function PanelState({ state, text }: { state: "loading" | "error" | "empty"; text?: string }) {
  if (state === "loading") return <div className="panel-state"><span className="spinner" aria-hidden="true" />LOADING</div>;
  if (state === "error") return <div className="panel-state panel-state--error">{text ?? "UNAVAILABLE"}</div>;
  return <div className="panel-state">{text ?? "NO DATA"}</div>;
}

// The source card: who provided the data, licence, health and freshness.
export type SourceInfo = {
  id: string; name: string; provider: string; license: string; attribution: string; health: string;
  last_success: string | null; data_age_hours: number | null; coverage: string; connection: string; docs_url?: string;
  freshness_status?: string; freshness_label?: string;
};

export function SourceCard({ source }: { source: SourceInfo | null | undefined }) {
  if (!source) return <div className="empty-note">Source record unavailable</div>;
  return (
    <div className="source-card">
      <div className="source-card__name">{source.name}</div>
      <Facts rows={[
        ["PROVIDER", source.provider],
        ["STATUS", source.freshness_status ? <Freshness status={source.freshness_status} label={source.freshness_label} /> : <HealthText health={source.health} />],
        ["MODE", source.connection],
        ["UPDATED", source.last_success ? `${formatTime(source.last_success)} (${formatAge(source.last_success)})` : null],
        ["COVERAGE", source.coverage],
        ["LICENSE", source.license],
        ["CREDIT", source.attribution],
      ]} />
    </div>
  );
}

// Human-readable source health with a coloured dot.
export function HealthText({ health }: { health: string }) {
  const tone = health === "ok" ? "ok" : health === "offline-snapshot" ? "neutral" : health === "failed" ? "error" : "pending";
  const text: Record<string, string> = {
    "ok": "LIVE (CONNECTED)", "offline-snapshot": "USING LAST STAGED SNAPSHOT", "failed": "SOURCE UNAVAILABLE",
    "key-required": "KEY REQUIRED", "not-implemented": "NOT IMPLEMENTED", "never-run": "NOT REFRESHED YET",
  };
  return <span className="health"><span className={`status-dot status-dot--${tone}`} aria-hidden="true" />{text[health] ?? health.toUpperCase()}</span>;
}

// Data freshness with a coloured dot. `status` is the backend's freshness
// status (LIVE, DELAYED, SNAPSHOT, STALE, EMPTY, UNAVAILABLE, NOT STAGED, KEY REQUIRED,
// NOT IMPLEMENTED); `label` adds the age, e.g. "SNAPSHOT · 3.4 h old".
const FRESHNESS_TONE: Record<string, string> = {
  "LIVE": "ok", "DELAYED": "pending", "SNAPSHOT": "neutral", "STALE": "pending", "EMPTY": "neutral",
  "UNAVAILABLE": "error", "NOT STAGED": "pending", "KEY REQUIRED": "pending", "NOT IMPLEMENTED": "neutral",
};

export function Freshness({ status, label }: { status: string; label?: string | null }) {
  return (
    <span className={`health health--${status.toLowerCase().replace(/ /g, "-")}`}>
      <span className={`status-dot status-dot--${FRESHNESS_TONE[status] ?? "neutral"}`} aria-hidden="true" />
      {(label ?? status).toUpperCase()}
    </span>
  );
}

// Provenance record: where the data came from and what was done to it.
export type ProvenanceRecord = {
  id: string; kind: string; source_id: string | null; source_version: string | null; retrieved_at: string | null;
  input_ref: string | null; input_sha256: string | null; processing: string; processing_version: string;
  parameters: Record<string, unknown> | null;
};

export function ProvenanceBlock({ record }: { record: ProvenanceRecord | null | undefined }) {
  if (!record) return <div className="empty-note">No provenance record</div>;
  return (
    <Facts rows={[
      ["RECORD", <code>{record.id}</code>],
      ["KIND", record.kind],
      ["RETRIEVED", formatTime(record.retrieved_at)],
      ["INPUT", <span className="break">{record.input_ref}</span>],
      ["SHA-256", record.input_sha256 ? <code title={record.input_sha256}>{record.input_sha256.slice(0, 16)}…</code> : null],
      ["VERSION", record.source_version],
      ["PROCESSING", record.processing],
      ["PIPELINE", record.processing_version],
    ]} />
  );
}

// A clickable list row used for related events, results and queues.
export function ListButton({ title, meta, onClick, active }: { title: ReactNode; meta?: ReactNode; onClick: () => void; active?: boolean }) {
  return (
    <button type="button" className={active ? "list-row list-row--active" : "list-row"} onClick={onClick}>
      <span className="list-row__title">{title}</span>
      {meta && <span className="list-row__meta">{meta}</span>}
    </button>
  );
}
