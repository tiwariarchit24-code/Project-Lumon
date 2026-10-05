import { useEffect, useState } from "react";
import { getJson, query } from "../../api";
import { formatArea, formatDate, labelCase } from "../../utils/format";
import { ListButton, PanelState, Section } from "../intel/common";
import type { Selection } from "../../types";
import { INFO } from "../../data/infoNotes";

// ---------------------------------------------------------------------------
// CHANGES and REVIEW QUEUE (same data, two uses):
//   - "changes": all accepted change events, with SHOW SUPPRESSED to also
//     list candidates rejected by the false-alarm gates (with the gates that
//     failed), so the analyst can see what was excluded and why
//   - "review":  the ranked queue - unreviewed first, then by score
// ---------------------------------------------------------------------------
type Candidate = {
  id: string; aoi_id: string; change_class: string; from_class: string; to_class: string; status: string; review_state: string;
  score: number; area_m2: number; earliest_supported_after: string | null; last_clean_before: string | null;
  failed_gates: { gate: string; detail: string }[]; priority: number;
};

type ChangesSectionProps = {
  mode: "changes" | "review";
  refreshKey: number;
  selectedId: string | null;
  onSelect: (selection: Selection) => void;
  onShowSuppressed: (show: boolean) => void;
};

export default function ChangesSection({ mode, refreshKey, selectedId, onSelect, onShowSuppressed }: ChangesSectionProps) {
  const [items, setItems] = useState<Candidate[] | null>(null);
  const [error, setError] = useState(false);
  const [showSuppressed, setShowSuppressed] = useState(false);
  const [classFilter, setClassFilter] = useState("all");

  useEffect(() => {
    getJson<Candidate[]>(`/api/review/queue${query({ include_suppressed: mode === "changes" && showSuppressed })}`)
      .then((data) => {
        setItems(data);
        setError(false);
      })
      .catch(() => setError(true));
  }, [mode, showSuppressed, refreshKey]);

  if (error) return <PanelState state="error" text="LUMON API UNREACHABLE" />;
  if (!items) return <PanelState state="loading" />;

  const classes = ["all", ...Array.from(new Set(items.map((i) => i.change_class)))];
  const shown = items.filter((i) => classFilter === "all" || i.change_class === classFilter);
  const unreviewed = items.filter((i) => i.review_state === "unreviewed" && i.status === "accepted").length;

  return (
    <>
      <Section title={mode === "review" ? "REVIEW QUEUE" : "CHANGE EVENTS"} meta={mode === "review" ? `${unreviewed} awaiting review` : `${shown.length} shown · rule-based`}
        info={mode === "review"
          ? { ...INFO.reviewQueue, state: `${unreviewed} awaiting review of ${items.filter((i) => i.status === "accepted").length} accepted` }
          : { ...INFO.changeDetection, state: `${items.filter((i) => i.status === "accepted").length} accepted${showSuppressed ? `, ${items.filter((i) => i.status === "suppressed").length} suppressed shown` : ""}` }}>
        <div className="toolbar-row">
          <select className="field field--small" value={classFilter} onChange={(e) => setClassFilter(e.target.value)} aria-label="Filter by change class">
            {classes.map((value) => <option key={value} value={value}>{value}</option>)}
          </select>
          {mode === "changes" && (
            <label className="check">
              <input type="checkbox" checked={showSuppressed} onChange={(e) => { setShowSuppressed(e.target.checked); onShowSuppressed(e.target.checked); }} />
              SHOW SUPPRESSED
            </label>
          )}
        </div>
        {items.length === 0 && <div className="empty-note">NO IMAGERY STAGED or no change candidates yet. Run the change engine (EO → Change engine).</div>}
        {shown.map((item) => (
          <ListButton
            key={item.id}
            active={item.id === selectedId}
            title={<>
              {mode === "review" && <span className="result-kind">#{item.priority}</span>}
              {labelCase(item.change_class)} <span className="muted">{item.from_class}→{item.to_class}</span>
              {item.status === "suppressed" && <span className="result-kind result-kind--muted">SUPPRESSED</span>}
              {item.review_state !== "unreviewed" && <span className={`result-kind result-kind--${item.review_state}`}>{item.review_state.toUpperCase()}</span>}
            </>}
            meta={item.status === "suppressed"
              ? `failed: ${item.failed_gates.map((g) => g.gate).join(", ")}`
              : `${formatDate(item.last_clean_before)} → ${formatDate(item.earliest_supported_after)} · ${formatArea(item.area_m2)} · score ${item.score.toFixed(2)} (uncal.)`}
            onClick={() => onSelect({ kind: "change", id: item.id })}
          />
        ))}
      </Section>
      {mode === "review" && (
        <Section title="PRIORITY RULE">
          <div className="empty-note">Unreviewed first, then higher uncalibrated score, then larger area. Scores rank work; they are not probabilities.</div>
        </Section>
      )}
    </>
  );
}
