import { useCallback, useEffect, useState } from "react";
import { ApiError, getJson, postJson } from "../../api";
import { formatDate } from "../../utils/format";
import { Facts, ListButton, Section } from "../intel/common";
import type { Selection } from "../../types";

// ---------------------------------------------------------------------------
// LEARNED CHANGE DETECTION (BTC-B, OSCD checkpoint) in Change Explorer.
//
// Separate from the rule-based change engine above it: own status, own runs,
// own map layer ("ML change candidates", magenta). Everything it produces is
// a MODEL-GENERATED CANDIDATE CHANGE. Steps:
//   1. choose a before/after pair (suggested same-season pairs, or any two
//      usable scenes) and CHECK it: refused pairs explain why
//   2. RUN: inference is queued as a job (~20 s on this CPU)
//   3. inspect runs: before / after / score images, candidate regions; a
//      region opens in the intelligence panel for review
// When the model is not staged the section says so and RUN is disabled;
// the rule-based engine is never run in its place.
// ---------------------------------------------------------------------------
type Status = {
  state: string; reasons: string[]; label: string; model_name: string; model_key: string; license: string;
  verified_here: string; training_data: string; preprocess_version: string; runs: number; regions: Record<string, number>;
};
type Scene = { id: string; acquired_at: string; quality_status: string; aoi_id?: string };
type Pair = { before_scene_id: string; after_scene_id: string; before_date: string; after_date: string; aoi_id: string; years_apart: number };
type Check = { ok: boolean; problems: string[]; warnings: string[]; shift_px?: number; joint_clear?: number; month_gap?: number; days_apart?: number };
type Run = {
  id: string; aoi_id: string; before_scene_id: string; after_scene_id: string; before_date: string; after_date: string;
  candidate_pixels: number; clear_pixels: number; regions_reported: number; regions_too_small: number; seconds: number;
  review_counts: Record<string, number>; checks: { warnings: string[] };
};
type RegionFeature = { properties: { id: string; mean_score: number; pixels: number; review_status: string } };

type LearnedChangeSectionProps = {
  onSelect: (selection: Selection) => void;
  onSetLayer: (id: string, enabled: boolean) => void;
  onShowScene: (aoiId: string, sceneId: string) => void;
  onFlyToBbox: (bbox: number[]) => void;
  onRefresh: () => void;
  onShowRun: (runId: string) => void; // the map draws this run's candidates
};

const TONE: Record<string, string> = { READY: "ok", INDEXING: "pending", "NOT STAGED": "pending", UNAVAILABLE: "error" };

export default function LearnedChangeSection({ onSelect, onSetLayer, onShowScene, onFlyToBbox, onRefresh, onShowRun }: LearnedChangeSectionProps) {
  const [status, setStatus] = useState<Status | null>(null);
  const [scenes, setScenes] = useState<Scene[]>([]);
  const [pairs, setPairs] = useState<Pair[]>([]);
  const [before, setBefore] = useState("");
  const [after, setAfter] = useState("");
  const [check, setCheck] = useState<Check | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [regions, setRegions] = useState<RegionFeature[]>([]);
  const [message, setMessage] = useState<string | null>(null);
  const [aoiBox, setAoiBox] = useState<Record<string, number[]>>({});

  const load = useCallback(() => {
    getJson<Status>("/api/ml-change/status").then(setStatus).catch(() => setStatus(null));
    getJson<Run[]>("/api/ml-change/runs").then(setRuns).catch(() => setRuns([]));
  }, []);

  useEffect(() => {
    load();
    getJson<Pair[]>("/api/ml-change/pairs/suggested?limit=60").then(setPairs).catch(() => setPairs([]));
    getJson<{ id: string; bbox: number[] }[]>("/api/aois").then(async (aois) => {
      setAoiBox(Object.fromEntries(aois.map((a) => [a.id, a.bbox])));
      const all: Scene[] = [];
      for (const aoi of aois) {
        const list = await getJson<Scene[]>(`/api/aois/${aoi.id}/scenes`);
        all.push(...list.filter((s) => s.quality_status === "usable" || s.quality_status === "degraded").map((s) => ({ ...s, aoi_id: aoi.id })));
      }
      setScenes(all);
    }).catch(() => undefined);
  }, [load]);

  // While a run is in progress, poll the status; reload runs when it ends.
  useEffect(() => {
    if (status?.state !== "INDEXING") return;
    const timer = window.setTimeout(() => {
      getJson<Status>("/api/ml-change/status").then((next) => {
        setStatus(next);
        if (next.state !== "INDEXING") {
          load();
          onRefresh();
          setMessage("Run finished. Open it below; candidates also appear on the map layer ML CHANGE CANDIDATES.");
        }
      }).catch(() => undefined);
    }, 3000);
    return () => window.clearTimeout(timer);
  }, [status, load, onRefresh]);

  useEffect(() => {
    if (!open) return;
    getJson<{ features: RegionFeature[] }>(`/api/ml-change/regions?run_id=${encodeURIComponent(open)}`)
      .then((data) => setRegions([...data.features].sort((a, b) => b.properties.mean_score * b.properties.pixels - a.properties.mean_score * a.properties.pixels)))
      .catch(() => setRegions([]));
  }, [open]);

  async function runCheck(b = before, a = after) {
    setCheck(null);
    if (!b || !a) return;
    try {
      setCheck(await getJson<Check>(`/api/ml-change/pairs/check?before=${encodeURIComponent(b)}&after=${encodeURIComponent(a)}`));
    } catch {
      setMessage("Check failed: Lumon API unreachable.");
    }
  }

  function choosePair(value: string) {
    const pair = pairs[Number(value)];
    if (!pair) return;
    setBefore(pair.before_scene_id);
    setAfter(pair.after_scene_id);
    runCheck(pair.before_scene_id, pair.after_scene_id);
  }

  async function start() {
    setMessage(null);
    try {
      const job = await postJson<{ job_id: number; warnings: string[] }>("/api/ml-change/runs", { before_scene_id: before, after_scene_id: after });
      setMessage(`Model run queued (job ${job.job_id}); about 20 s on this machine.`);
      setStatus((s) => (s ? { ...s, state: "INDEXING" } : s));
    } catch (error) {
      const detail = error instanceof ApiError ? (error.detail as { state?: string; reasons?: string[] } | null) : null;
      setMessage(detail?.state ? `${detail.state}: ${(detail.reasons ?? []).join("; ")}` : "Not queued: Lumon API unreachable.");
    }
  }

  function showOnMap(run: Run) {
    onShowRun(run.id);
    onSetLayer("ml-change-candidates", true);
    onShowScene(run.aoi_id, run.after_scene_id);
    if (aoiBox[run.aoi_id]) onFlyToBbox(aoiBox[run.aoi_id]);
    onRefresh();
  }

  const state = status?.state ?? "UNAVAILABLE";
  const sceneLabel = (id: string) => {
    const scene = scenes.find((s) => s.id === id);
    return scene ? `${formatDate(scene.acquired_at)} · ${scene.id}` : id;
  };

  return (
    <Section title="LEARNED CHANGE DETECTION" meta={`AI/ML · ${state}`}>
      <span className="mlchange-label">MODEL-GENERATED CANDIDATE CHANGES</span>
      <div className="semantic-status">
        <span className={`status-dot status-dot--${TONE[state] ?? "neutral"}`} aria-hidden="true" />
        <span className="semantic-status__state">{state}</span>
        {status && <span className="semantic-status__model">{status.model_name}</span>}
      </div>
      {status && (
        <Facts rows={[
          ["MODEL", status.model_key],
          ["VERIFIED", status.verified_here],
          ["TRAINED ON", status.training_data],
          ["LICENCE", status.license],
          ["RUNS", `${status.runs} · regions ${Object.entries(status.regions).map(([k, v]) => `${v} ${k}`).join(", ") || "none"}`],
        ]} />
      )}
      {status?.reasons.map((reason) => <div key={reason} className="notice notice--partial">{reason}</div>)}
      <div className="empty-note">Separate from the rule-based engine above: it never replaces it, and its output is never a verified change.</div>

      <div className="intel-subtitle">PAIR</div>
      <select className="field mlchange-select" defaultValue="" onChange={(e) => choosePair(e.target.value)} aria-label="Suggested pairs">
        <option value="" disabled>Suggested: same season, different years ({pairs.length})</option>
        {pairs.map((pair, index) => (
          <option key={`${pair.before_scene_id}-${pair.after_scene_id}`} value={index}>
            {formatDate(pair.before_date)} → {formatDate(pair.after_date)} ({pair.years_apart} y)
          </option>
        ))}
      </select>
      <select className="field mlchange-select" value={before} onChange={(e) => { setBefore(e.target.value); runCheck(e.target.value, after); }} aria-label="Before scene">
        <option value="">Before scene…</option>
        {scenes.map((s) => <option key={s.id} value={s.id}>{formatDate(s.acquired_at)} · {s.id}</option>)}
      </select>
      <select className="field mlchange-select" value={after} onChange={(e) => { setAfter(e.target.value); runCheck(before, e.target.value); }} aria-label="After scene">
        <option value="">After scene…</option>
        {scenes.map((s) => <option key={s.id} value={s.id}>{formatDate(s.acquired_at)} · {s.id}</option>)}
      </select>
      {check && !check.ok && check.problems.map((p) => <div key={p} className="notice notice--error">REFUSED — {p}</div>)}
      {check?.ok && (
        <Facts rows={[
          ["CHECK", "pair can be compared"],
          ["REGISTRATION", `${check.shift_px} px measured (refused above 1.0)`],
          ["CLEAR IN BOTH", `${Math.round((check.joint_clear ?? 0) * 100)}%`],
          ["SEASON GAP", `${check.month_gap} month(s) · ${check.days_apart} days apart`],
        ]} />
      )}
      {check?.warnings.map((w) => <div key={w} className="notice notice--partial">{w}</div>)}
      <button type="button" className="action-button action-button--primary" onClick={start}
        disabled={state !== "READY" || !check?.ok}>{state === "INDEXING" ? "RUNNING…" : "RUN MODEL ON THIS PAIR"}</button>
      {message && <div className="notice">{message}</div>}

      <div className="intel-subtitle">RUNS ({runs.length})</div>
      {runs.length === 0 && <div className="empty-note">No model run yet.</div>}
      {runs.map((run) => (
        <div key={run.id} className="source-card">
          <button type="button" className="source-card__head" onClick={() => setOpen(open === run.id ? null : run.id)} aria-expanded={open === run.id}>
            <span className="source-card__name">{formatDate(run.before_date)} → {formatDate(run.after_date)}</span>
            <span className="source-card__sub">
              {run.regions_reported} candidate regions · {(100 * run.candidate_pixels / Math.max(run.clear_pixels, 1)).toFixed(1)}% of clear pixels
              {run.review_counts.rejected ? ` · ${run.review_counts.rejected} rejected` : ""}
            </span>
          </button>
          {open === run.id && (
            <div className="source-card__body fade-in">
              <div className="mlchange-triptych">
                <figure><img src={`/api/scenes/${run.before_scene_id}/quicklook.png`} alt="Before" /><figcaption>BEFORE</figcaption></figure>
                <figure><img src={`/api/scenes/${run.after_scene_id}/quicklook.png`} alt="After" /><figcaption>AFTER</figcaption></figure>
                <figure><img src={`/api/ml-change/runs/${encodeURIComponent(run.id)}/score.png`} alt="Model score above 0.5" /><figcaption>SCORE &gt; 0.5</figcaption></figure>
              </div>
              <Facts rows={[
                ["BEFORE", sceneLabel(run.before_scene_id)],
                ["AFTER", sceneLabel(run.after_scene_id)],
                ["NOT REPORTED", `${run.regions_too_small} regions below 9 px (0.09 ha)`],
                ["RUN TIME", `${run.seconds} s`],
                ["OUTPUTS", <><a href={`/api/ml-change/runs/${encodeURIComponent(run.id)}/probability.tif`}>probability.tif</a> · <a href={`/api/ml-change/runs/${encodeURIComponent(run.id)}/candidates.tif`}>candidates.tif</a></>],
              ]} />
              {run.checks.warnings.map((w) => <div key={w} className="notice notice--partial">{w}</div>)}
              <button type="button" className="action-button" onClick={() => showOnMap(run)}>SHOW CANDIDATES ON MAP</button>
              {regions.slice(0, 12).map((feature) => (
                <ListButton key={feature.properties.id} title={`Candidate · score ${feature.properties.mean_score.toFixed(2)}`}
                  meta={`${feature.properties.pixels} px · ${feature.properties.review_status}`}
                  onClick={() => { onShowRun(run.id); onSetLayer("ml-change-candidates", true); onSelect({ kind: "ml-change", id: feature.properties.id }); }} />
              ))}
              {regions.length > 12 && <div className="empty-note">{regions.length - 12} more on the map.</div>}
            </div>
          )}
        </div>
      ))}
    </Section>
  );
}
