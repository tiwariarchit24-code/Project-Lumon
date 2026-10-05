import { useEffect, useState } from "react";
import { getJson, postJson } from "../../api";
import { formatArea, formatDate, formatTime } from "../../utils/format";
import { Facts, ListButton, PanelState, Section } from "./common";

// ---------------------------------------------------------------------------
// One MODEL-GENERATED CANDIDATE CHANGE region (BTC-B, OSCD checkpoint).
//
// Shows the same window before, after and as the model's score map, the
// region's score, the rule-based evidence checks run on it (seasonal
// vegetation/water, cloud edge, misregistration, size, persistence in later
// scenes), its overlap with the separate rule-based baseline (not ground
// truth), the two source scenes, model version and provenance, and lets the
// analyst reject it or mark it plausible (audited). There is deliberately no
// "confirmed" state: a model candidate is never turned into a verified change
// here.
// ---------------------------------------------------------------------------
type Spectral = { ndvi: number; mndwi: number; brightness: number };
type BaselineItem = { id: string; status: string; change_class: string; last_clean_before: string | null; earliest_supported_after: string | null; pixels_shared: number; change_window_overlaps_pair: boolean };
type Region = {
  id: string; label: string; run_id: string; geometry: GeoJSON.Polygon; bbox: number[]; lon: number; lat: number;
  area_m2: number; pixels: number; mean_score: number; max_score: number;
  review_status: string; reviewed_by: string | null; reviewed_at: string | null; review_note: string | null;
  evidence: {
    spectral_before: Spectral; spectral_after: Spectral; flags: string[]; flags_note: string;
    persistence: { scene_id: string; date: string; closer_to: string }[]; baseline_overlap: BaselineItem[]; baseline_note: string;
  };
  run: {
    id: string; model_key: string; preprocess_version: string; aoi_id: string; before_scene_id: string; after_scene_id: string;
    before_date: string; after_date: string; checks: { shift_px: number; joint_clear: number; month_gap: number; warnings: string[] };
    parameters: Record<string, unknown>; created_at: string; provenance: { id: string; processing: string } | null;
  };
};

type MlChangeDetailProps = {
  id: string;
  onHighlight: (geometry: GeoJSON.Geometry | null) => void;
  onShowScene: (aoiId: string, sceneId: string) => void;
  onSelectChange: (id: string) => void; // a rule-based change event
};

function savedAnalyst(): string {
  try {
    return window.localStorage.getItem("lumon.analyst") ?? "";
  } catch {
    return "";
  }
}

export default function MlChangeDetail({ id, onHighlight, onShowScene, onSelectChange }: MlChangeDetailProps) {
  const [region, setRegion] = useState<Region | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [analyst, setAnalyst] = useState(savedAnalyst);
  const [note, setNote] = useState("");
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    setRegion(null);
    setError(null);
    getJson<Region>(`/api/ml-change/regions/${encodeURIComponent(id)}`)
      .then((record) => {
        setRegion(record);
        onHighlight({ type: "MultiPolygon", coordinates: [record.geometry.coordinates] });
      })
      .catch((e) => setError(e.offline ? "LUMON API UNREACHABLE" : "CANDIDATE REGION NOT FOUND"));
  }, [id, onHighlight]);

  async function review(decision: string) {
    if (!analyst.trim()) {
      setMessage("Enter your analyst name first (it is written to the audit trail).");
      return;
    }
    try {
      window.localStorage.setItem("lumon.analyst", analyst.trim());
    } catch {
      // not remembered; the review still goes through
    }
    try {
      setRegion(await postJson<Region>(`/api/ml-change/review/${encodeURIComponent(id)}`, { decision, analyst: analyst.trim(), note: note || null }));
      setMessage(`Recorded as ${decision.toUpperCase()} in the audit trail.`);
    } catch {
      setMessage("Not recorded: Lumon API unreachable.");
    }
  }

  if (error) return <PanelState state="error" text={error} />;
  if (!region) return <PanelState state="loading" />;

  // A window a little larger than the region, the same for all three images.
  const [w, s, e, n] = region.bbox;
  const pad = Math.max(e - w, n - s) * 0.4 + 0.003;
  const box = [w - pad, s - pad, e + pad, n + pad].join(",");
  const run = region.run;
  const { spectral_before: before, spectral_after: after } = region.evidence;
  const persisted = region.evidence.persistence.filter((p) => p.closer_to === "after").length;

  return (
    <div className="detail fade-in">
      <div className="detail__kind">ML CHANGE · BTC-B</div>
      <h2 className="detail__title">{formatDate(run.before_date)} → {formatDate(run.after_date)}</h2>
      <div className="detail__tags">
        <span className="evidence-tag evidence-tag--model">MODEL-GENERATED CANDIDATE</span>
        <span className="plain-tag">{region.review_status.toUpperCase()}</span>
      </div>
      <div className="notice detail__banner">
        A model-generated candidate change, not a verified change. The network was trained on urban changes in other
        Sentinel-2 imagery; this region only says the two images differ the way such changes did. Check the images.
      </div>

      <Section title="BEFORE · AFTER · MODEL SCORE" meta="same window">
        <div className="mlchange-triptych">
          <figure><img src={`/api/scenes/${run.before_scene_id}/quicklook.png?bbox=${box}&upscale=3`} alt="Before" /><figcaption>BEFORE {formatDate(run.before_date)}</figcaption></figure>
          <figure><img src={`/api/scenes/${run.after_scene_id}/quicklook.png?bbox=${box}&upscale=3`} alt="After" /><figcaption>AFTER {formatDate(run.after_date)}</figcaption></figure>
          <figure><img src={`/api/ml-change/runs/${encodeURIComponent(run.id)}/score.png?bbox=${box}&upscale=3`} alt="Model score above 0.5" /><figcaption>SCORE &gt; 0.5</figcaption></figure>
        </div>
        <div className="toolbar-row">
          <button type="button" className="action-button" onClick={() => onShowScene(run.aoi_id, run.before_scene_id)}>SHOW BEFORE ON MAP</button>
          <button type="button" className="action-button" onClick={() => onShowScene(run.aoi_id, run.after_scene_id)}>SHOW AFTER ON MAP</button>
        </div>
      </Section>

      <Section title="MODEL OUTPUT">
        <Facts rows={[
          ["SCORE", `mean ${region.mean_score.toFixed(2)} · max ${region.max_score.toFixed(2)} (model score, not a calibrated probability; threshold 0.5)`],
          ["SIZE", `${region.pixels} px at 10 m · ${formatArea(region.area_m2)}`],
          ["CENTRE", `${region.lat.toFixed(5)}, ${region.lon.toFixed(5)}`],
        ]} />
      </Section>

      <Section title="EVIDENCE CHECKS" meta="rule-based">
        <Facts rows={[
          ["NDVI", `${before.ndvi.toFixed(2)} → ${after.ndvi.toFixed(2)}`],
          ["MNDWI", `${before.mndwi.toFixed(2)} → ${after.mndwi.toFixed(2)}`],
          ["BRIGHTNESS", `${before.brightness.toFixed(3)} → ${after.brightness.toFixed(3)} (reflectance)`],
          ["PERSISTENCE", region.evidence.persistence.length ? `${persisted}/${region.evidence.persistence.length} later same-season scenes look like AFTER` : "no later same-season scene to check"],
        ]} />
        {region.evidence.flags.length === 0 && <div className="empty-note">No warning flags. That is not confirmation.</div>}
        {region.evidence.flags.map((flag) => <div key={flag} className="notice notice--partial">{flag}</div>)}
        <div className="empty-note">{region.evidence.flags_note}</div>
      </Section>

      <Section title="RULE-BASED BASELINE" meta={`${region.evidence.baseline_overlap.length} overlapping`}>
        <div className="empty-note">{region.evidence.baseline_note}</div>
        {region.evidence.baseline_overlap.map((item) => (
          <ListButton key={item.id} title={`${item.change_class} · ${item.status.toUpperCase()}`}
            meta={`${item.pixels_shared} px shared · window ${item.last_clean_before ?? "?"} → ${item.earliest_supported_after ?? "?"}${item.change_window_overlaps_pair ? " (inside this pair)" : " (outside this pair)"}`}
            onClick={() => onSelectChange(item.id)} />
        ))}
      </Section>

      <Section title="ANALYST REVIEW">
        <input className="field" placeholder="Analyst name" value={analyst} onChange={(e) => setAnalyst(e.target.value)} aria-label="Analyst name" />
        <input className="field" placeholder="Note (optional)" value={note} onChange={(e) => setNote(e.target.value)} aria-label="Review note" />
        <div className="toolbar-row">
          <button type="button" className="action-button" onClick={() => review("rejected")}>REJECT</button>
          <button type="button" className="action-button" onClick={() => review("plausible")}>PLAUSIBLE</button>
          {region.review_status !== "unreviewed" && <button type="button" className="action-button" onClick={() => review("unreviewed")}>RESET</button>}
        </div>
        {region.reviewed_by && <div className="empty-note">{region.review_status.toUpperCase()} by {region.reviewed_by}, {formatTime(region.reviewed_at)}{region.review_note ? ` · “${region.review_note}”` : ""}</div>}
        <div className="empty-note">PLAUSIBLE means worth following up; it does not verify the change.</div>
        {message && <div className="notice">{message}</div>}
      </Section>

      <Section title="SOURCE AND PROVENANCE">
        <Facts rows={[
          ["BEFORE", `${run.before_scene_id} · ${formatTime(run.before_date)}`],
          ["AFTER", `${run.after_scene_id} · ${formatTime(run.after_date)}`],
          ["PAIR CHECK", `misregistration ${run.checks.shift_px} px · ${Math.round(run.checks.joint_clear * 100)}% clear in both · ${run.checks.month_gap} month(s) apart in season`],
          ["MODEL", run.model_key],
          ["PREPROCESSING", run.preprocess_version],
          ["RUN", `${formatTime(run.created_at)}${run.provenance ? ` · ${run.provenance.id}` : ""}`],
        ]} />
        {run.checks.warnings.map((warning) => <div key={warning} className="notice notice--partial">{warning}</div>)}
      </Section>
    </div>
  );
}
