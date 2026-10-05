import QueryPlan from "../QueryPlan";
import { Facts, ListButton, Section } from "../intel/common";
import { formatTime } from "../../utils/format";
import type { QueryPlan as Plan, QueryResultItem, QueryRun } from "../../types";
import { INFO } from "../../data/infoNotes";

// ---------------------------------------------------------------------------
// SEARCH: the parsed plan (editable chips), the RUN button, the capability
// check (what cannot be answered and why) and the ranked results with
// "why it matched" reasons.
// ---------------------------------------------------------------------------
type SearchSectionProps = {
  plan: Plan | null;
  run: QueryRun | null;
  running: boolean;
  onPlanChange: (plan: Plan) => void;
  onRun: () => void;
  onOpenResult: (item: QueryResultItem) => void;
  onOpenPilot?: () => void;
};

export default function SearchSection({ plan, run, running, onPlanChange, onRun, onOpenResult, onOpenPilot }: SearchSectionProps) {
  if (!plan) {
    return (
      <Section title="ASK A QUESTION">
        <div className="empty-note">
          Type a question in the command bar and press Enter, for example “Show recent earthquakes near Delhi” or
          “Show newly constructed areas near rivers since 2022”. The question becomes an editable plan here before anything runs.
        </div>
      </Section>
    );
  }
  return (
    <>
      <Section title="QUERY PLAN" meta={`“${plan.text}”`}
        info={{ ...INFO.search, state: run ? `last run: ${run.capability.state}, ${run.count} results` : `intent ${plan.intent}; not run yet` }}>
        <QueryPlan plan={plan} onChange={onPlanChange} />
        <button type="button" className="action-button action-button--primary" onClick={onRun} disabled={running}>
          {running ? "RUNNING…" : "RUN PLAN"}
        </button>
      </Section>

      {run && (
        <>
          {/* SUPPORTED: fully answerable. PARTIAL: runs, but a source it needs
              (key, failed, not implemented) is missing, so results may be
              incomplete. UNSUPPORTED: refused - no source can answer it. */}
          <Section title="CAPABILITY CHECK" meta={{ SUPPORTED: "ANSWERABLE", PARTIAL: "PARTIAL", "NOT STAGED": "NOT STAGED", UNSUPPORTED: "CANNOT ANSWER" }[run.capability.state]}>
            {run.capability.state === "NOT STAGED" && (
              <div className="notice notice--partial">NOT STAGED — understood, but the data or model it needs is not staged. Nothing was run and no results are shown.</div>
            )}
            {run.capability.issues.map((issue) => <div key={issue} className={run.capability.state === "NOT STAGED" ? "notice" : "notice notice--error"}>{issue}</div>)}
            {(run.capability.partial ?? []).map((item) => <div key={item} className="notice notice--partial">PARTIAL — {item} Results may be incomplete.</div>)}
            {run.capability.warnings.map((warning) => <div key={warning} className="notice">{warning}</div>)}
            {run.capability.state === "SUPPORTED" && run.capability.warnings.length === 0 && <div className="empty-note">All parts of the plan can be evaluated with staged data.</div>}
          </Section>
          {run.semantic && (
            <Section title={plan.intent === "find-similar" ? "IMAGE SIMILARITY" : "IMAGE SEARCH"} meta="RemoteCLIP · AI/ML">
              <div className="notice">{run.semantic.note}</div>
              <Facts rows={[
                plan.intent === "find-similar" ? ["EXAMPLE", `${run.semantic.query} (chip at the selected location)`] : ["TEXT TO MODEL", `“${run.semantic.query}”`],
                ["MODEL", `${run.semantic.model.model ?? ""} ${run.semantic.model.architecture ?? ""}`.trim()],
                ["SEARCHED", `${run.semantic.chips_searched} chips from ${run.semantic.scenes_searched} scenes`],
                ["QUERY TIME", `${Math.round(run.semantic.timing_ms.text_encode + run.semantic.timing_ms.rank)} ms${run.semantic.timing_ms.model_load > 50 ? ` (+ model load ${(run.semantic.timing_ms.model_load / 1000).toFixed(1)} s)` : ""}`],
                ["SCORE", run.semantic.score_kind],
              ]} />
            </Section>
          )}
          <Section title="RESULTS" meta={`${run.count} · ${run.area.name ?? ""}`}>
            {plan.study_area && plan.study_area.id !== "india" && onOpenPilot && (
              <button type="button" className="text-button" onClick={onOpenPilot}>Open NIT Raipur pilot ›</button>
            )}
            {run.count === 0 && run.capability.supported && <div className="empty-note">No matching records in staged data. This is not evidence that nothing happened.</div>}
            {run.results.map((item) => (
              <ListButton
                key={`${item.kind}-${item.id}`}
                title={<>
                  {item.kind === "chip" && item.bbox && <img className="result-thumb" src={`/api/scenes/${item.scene_id}/quicklook.png?bbox=${item.bbox.join(",")}`} alt="" loading="lazy" />}
                  <span className="result-kind">{item.kind.toUpperCase()}</span>{item.title}
                </>}
                meta={item.kind === "chip"
                  ? item.why.join(" · ")
                  : [item.time ? formatTime(item.time) : null, item.score !== undefined ? `score ${item.score.toFixed(2)} (uncal.)` : null, ...item.why.slice(-2)].filter(Boolean).join(" · ")}
                onClick={() => onOpenResult(item)}
              />
            ))}
            {run.results.some((r) => r.related_events?.length) && (
              <div className="empty-note">Related OSINT events within 50 km are listed in each change's reasons.</div>
            )}
          </Section>
        </>
      )}
    </>
  );
}
