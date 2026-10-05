import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { getJson, postJson } from "../../api";
import { formatArea, formatDate, formatTime, labelCase } from "../../utils/format";
import EvidenceCard from "./EvidenceCard";
import { EvidenceTag, Facts, ListButton, PanelState, ProvenanceBlock, Section } from "./common";
import type { ProvenanceRecord } from "./common";
import { INFO } from "../../data/infoNotes";

// ---------------------------------------------------------------------------
// One change event (or suppressed candidate) with all of its evidence:
//   - before/after chips and the honest uncertainty window
//   - every false-alarm gate with its reason
//   - the uncalibrated score, clearly labelled
//   - the scenes used, provenance, earlier decisions
//   - CONFIRM / REJECT / RELABEL (logged + audited) and MORE LIKE THESE
// ---------------------------------------------------------------------------
type Gate = { gate: string; passed: boolean; detail: string };
type Scene = { id: string; acquired_at: string; sensor: string; quality_status: string; cloud_fraction: number | null; processing_level: string };
type Decision = { id: number; decision: string; new_label: string | null; analyst: string; reason: string | null; decided_at: string };

type ChangeRecord = {
  id: string; aoi_id: string; change_class: string; direction: string; from_class: string; to_class: string;
  geometry: GeoJSON.Geometry; lon: number; lat: number; area_m2: number; area_min_m2: number; area_max_m2: number;
  last_clean_before: string | null; earliest_supported_after: string | null;
  score: number; score_kind: string; status: string; review_state: string;
  gate_results: Gate[]; scenes: Scene[]; evidence_before: Scene | null; evidence_after: Scene | null;
  decisions: Decision[]; provenance: ProvenanceRecord | null;
};

type Similar = { tile_id: string; similarity: number; lon: number; lat: number; why: string[] };

type ChangeDetailProps = {
  id: string;
  onHighlight: (geometry: GeoJSON.Geometry | null) => void;
  onDecision: () => void; // tell the page to refresh map + queue
  onSelectTile: (lon: number, lat: number) => void;
};

const LABELS = ["construction", "clearance", "water-extent", "road-development", "other"];

// The analyst's name is remembered in this browser only (a convenience).
function savedAnalyst(): string {
  try {
    return window.localStorage.getItem("lumon.analyst") ?? "";
  } catch {
    return "";
  }
}

// Bounding box around the change, padded so the chips show context.
function chipBbox(geometry: GeoJSON.Geometry): number[] {
  const points: number[][] = (geometry as GeoJSON.MultiPolygon).coordinates.flat(2);
  const lons = points.map((p) => p[0]);
  const lats = points.map((p) => p[1]);
  const pad = 0.004;
  return [Math.min(...lons) - pad, Math.min(...lats) - pad, Math.max(...lons) + pad, Math.max(...lats) + pad];
}

export default function ChangeDetail({ id, onHighlight, onDecision, onSelectTile }: ChangeDetailProps) {
  const [change, setChange] = useState<ChangeRecord | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [analyst, setAnalyst] = useState(savedAnalyst);
  const [reason, setReason] = useState("");
  const [label, setLabel] = useState("construction");
  const [message, setMessage] = useState<string | null>(null);
  const [similar, setSimilar] = useState<{ results: Similar[]; method: string } | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    setError(null);
    getJson<ChangeRecord>(`/api/changes/${id}`)
      .then((data) => {
        setChange(data);
        onHighlight(data.geometry);
      })
      .catch((e) => setError(e.offline ? "LUMON API UNREACHABLE" : "CHANGE NOT FOUND"));
  }, [id, onHighlight, reloadKey]);

  // Opening the evidence is logged (used to measure decision time).
  useEffect(() => {
    setSimilar(null);
    setMessage(null);
    postJson(`/api/review/${id}/open`, {}).catch(() => undefined);
  }, [id]);

  async function decide(decision: "confirmed" | "rejected" | "relabelled") {
    if (!analyst.trim()) {
      setMessage("Enter your analyst name first (it is written to the audit trail).");
      return;
    }
    try {
      window.localStorage.setItem("lumon.analyst", analyst.trim());
    } catch {
      // storage unavailable (private mode): the name is still sent with the decision
    }
    try {
      const result = await postJson<{ audit_hash: string }>(`/api/review/${id}/decision`, {
        decision, analyst: analyst.trim(), reason: reason || null, new_label: decision === "relabelled" ? label : null,
      });
      setMessage(`${decision.toUpperCase()} · logged · audit ${result.audit_hash.slice(0, 12)}…`);
      setReason("");
      setReloadKey((k) => k + 1);
      onDecision();
    } catch (e) {
      setMessage(`Decision not saved: ${(e as Error).message}`);
    }
  }

  async function moreLikeThis() {
    try {
      setSimilar(await postJson<{ results: Similar[]; method: string }>("/api/review/more-like-these", { candidate_ids: [id], limit: 8 }));
    } catch {
      setSimilar({ results: [], method: "similarity unavailable" });
    }
  }

  if (error) return <PanelState state="error" text={error} />;
  if (!change) return <PanelState state="loading" />;

  const gates = change.gate_results.filter((g) => g.gate !== "class note");
  const note = change.gate_results.find((g) => g.gate === "class note");
  const passedCount = gates.filter((g) => g.passed).length;
  const factRows: [string, ReactNode][] = [
    ["TRANSITION", `${change.from_class} → ${change.to_class}`],
    ["DIRECTION", change.direction],
    ["AREA", `${formatArea(change.area_m2)} (range ${formatArea(change.area_min_m2)}–${formatArea(change.area_max_m2)})`],
    ["LAST CLEAR BEFORE", formatDate(change.last_clean_before)],
    ["EARLIEST SUPPORTED", formatDate(change.earliest_supported_after)],
    ["WINDOW", "The change happened between these two dates. No exact date is claimed."],
    ["LOCATION", `${change.lat.toFixed(5)}, ${change.lon.toFixed(5)}`],
    ["AOI", change.aoi_id],
  ];

  return (
    <div className="detail fade-in">
      <div className="detail__kind">CHANGE EVENT · {change.status === "accepted" ? "ACCEPTED" : "SUPPRESSED"}</div>
      <h2 className="detail__title">{labelCase(change.change_class)}</h2>
      <div className="detail__tags">
        <EvidenceTag type="OBSERVED" />
        <span className={`plain-tag plain-tag--${change.review_state}`}>{change.review_state.toUpperCase()}</span>
      </div>

      <Section title="EVIDENCE" meta="Sentinel-2 L2A · true colour"
        info={{ ...INFO.evidence, state: `${formatDate(change.last_clean_before)} → ${formatDate(change.earliest_supported_after)}` }}>
        <EvidenceCard before={change.evidence_before} after={change.evidence_after} bbox={chipBbox(change.geometry)} />
      </Section>

      <Section title="CHANGE"><Facts rows={factRows} /></Section>

      <Section title="CONFIDENCE" info={{ ...INFO.confidence, state: `${change.score.toFixed(2)} (uncalibrated); ${change.decisions.length} analyst decision(s) on this event` }}>
        <div className="score">
          <span className="score__value">{change.score.toFixed(2)}</span>
          <span className="score__kind">UNCALIBRATED SCORE — not a probability</span>
        </div>
        <div className="empty-note">Ranks the review queue. Formula: mean of before-agreement, after-agreement, persistence and magnitude (docs/evaluation.md).</div>
      </Section>

      <Section title="FALSE-ALARM GATES" meta={`${passedCount}/${gates.length} passed`}>
        <ul className="gates">
          {gates.map((gate) => (
            <li key={gate.gate} className={gate.passed ? "gate gate--pass" : "gate gate--fail"}>
              <span className="gate__mark" aria-hidden="true">{gate.passed ? "✓" : "✕"}</span>
              <span className="gate__name">{gate.gate.toUpperCase()}</span>
              <span className="gate__detail">{gate.detail}</span>
            </li>
          ))}
        </ul>
        {note && <div className="empty-note">CLASS NOTE: {note.detail}</div>}
      </Section>

      <Section title="ANALYST REVIEW">
        <div className="review-form">
          <input className="field" placeholder="Analyst name" value={analyst} onChange={(e) => setAnalyst(e.target.value)} aria-label="Analyst name" />
          <input className="field" placeholder="Reason (optional)" value={reason} onChange={(e) => setReason(e.target.value)} aria-label="Reason" />
          <div className="review-actions">
            <button type="button" className="review-button review-button--confirm" onClick={() => decide("confirmed")}>CONFIRM</button>
            <button type="button" className="review-button review-button--reject" onClick={() => decide("rejected")}>REJECT</button>
          </div>
          <div className="review-relabel">
            <select className="field" value={label} onChange={(e) => setLabel(e.target.value)} aria-label="New label">
              {LABELS.map((value) => <option key={value} value={value}>{value}</option>)}
            </select>
            <button type="button" className="review-button" onClick={() => decide("relabelled")}>RELABEL</button>
          </div>
          {message && <div className="review-message" role="status">{message}</div>}
        </div>
        {change.decisions.length > 0 && (
          <ul className="decision-list">
            {change.decisions.map((d) => (
              <li key={d.id}>{formatTime(d.decided_at)} · {d.analyst} · {d.decision.toUpperCase()}{d.new_label ? ` → ${d.new_label}` : ""}{d.reason ? ` · “${d.reason}”` : ""}</li>
            ))}
          </ul>
        )}
      </Section>

      <Section title="MORE LIKE THIS" meta="non-semantic features">
        <button type="button" className="action-button" onClick={moreLikeThis}>FIND SIMILAR SITES IN AOI</button>
        {similar && (
          <>
            <div className="empty-note">{similar.method}. Rejected candidates are used as negative examples.</div>
            {similar.results.length === 0 && <div className="empty-note">No similar tiles found.</div>}
            {similar.results.map((item) => (
              <ListButton key={item.tile_id} title={`${item.tile_id} · sim ${item.similarity.toFixed(2)}`} meta={item.why.join(" · ")} onClick={() => onSelectTile(item.lon, item.lat)} />
            ))}
          </>
        )}
      </Section>

      <Section title="OBSERVATIONS USED" meta={`${change.scenes.length} scenes`}>
        <div className="scene-strip">
          {change.scenes.map((scene) => (
            <span key={scene.id} className="scene-tick" title={`${scene.id} · ${scene.quality_status}`}>{scene.acquired_at.slice(0, 7)}</span>
          ))}
        </div>
      </Section>

      <Section title="PROVENANCE" info={{ ...INFO.provenance, state: change.provenance ? `${change.provenance.processing_version}` : "no record" }}><ProvenanceBlock record={change.provenance} /></Section>
    </div>
  );
}
