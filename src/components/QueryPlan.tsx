import type { QueryPlan as Plan } from "../types";

// ---------------------------------------------------------------------------
// The editable query plan, shown as "chips".
//
// Each chip shows one part of what the parser understood (CHANGE, PLACE,
// DISTANCE, TIME...). Values the analyst did not actually say but the
// engine needs are marked ASSUMED, so they can be checked and edited. Words
// the parser could not use are listed, never silently dropped.
//
// Props:
//  - plan:     the current plan
//  - onChange: called with an edited copy of the plan
// ---------------------------------------------------------------------------
type QueryPlanProps = {
  plan: Plan;
  onChange: (plan: Plan) => void;
};

const CHANGE_CLASSES = ["any", "construction", "clearance", "water-extent", "road-development", "other"];
const REFERENCES = ["", "river", "lake", "coast", "road", "railway"];

// One chip: small label on top, value (or editor) below, optional ASSUMED tag.
function Chip({ label, assumed, note, children, onRemove }: {
  label: string; assumed?: boolean; note?: string; children: React.ReactNode; onRemove?: () => void;
}) {
  return (
    <div className={assumed ? "chip chip--assumed" : "chip"} title={note}>
      <div className="chip__label">
        {label}
        {assumed && <span className="chip__assumed">ASSUMED</span>}
        {onRemove && <button type="button" className="chip__remove" onClick={onRemove} aria-label={`Remove ${label}`}>×</button>}
      </div>
      <div className="chip__value">{children}</div>
    </div>
  );
}

export default function QueryPlan({ plan, onChange }: QueryPlanProps) {
  // Helper: copy the plan with some fields replaced.
  const update = (changes: Partial<Plan>) => onChange({ ...plan, ...changes });

  return (
    <div className="plan">
      <div className="plan__intent">
        <span className="plan__intent-label">INTENT</span>
        <span className="plan__intent-value">{plan.intent.replace("-", " ").toUpperCase()}</span>
        {plan.evidence_type && <span className="evidence-tag">{plan.evidence_type}</span>}
      </div>

      <div className="plan__chips">
        {plan.study_area && (
          <Chip label="STUDY AREA" note={plan.study_area.status === "PROVISIONAL" ? "Provisional search area, not the campus boundary" : undefined}>
            {plan.study_area.name}
            {plan.study_area.status && <span className="chip__status">{plan.study_area.status}</span>}
          </Chip>
        )}

        {plan.operation && (
          <Chip label="OPERATION" note={plan.operation.fallback ? `Fallback today: ${plan.operation.fallback}` : undefined}>
            {plan.operation.name}
            <span className="chip__status">{plan.operation.status}</span>
          </Chip>
        )}

        {plan.target_class && <Chip label="TARGET">{plan.target_class.class}</Chip>}

        {plan.target && plan.target.kind !== "change" && (
          <Chip label={plan.target.kind === "event" ? "EVENT" : plan.target.kind === "entity" ? "ENTITY" : "TARGET"}>
            {(plan.target.types ?? [plan.target.label]).join(", ")}
          </Chip>
        )}

        {plan.change && (
          <Chip label="CHANGE">
            <select className="chip__select" value={plan.change.class} onChange={(e) => update({ change: { ...plan.change!, class: e.target.value, direction: null } })}>
              {CHANGE_CLASSES.map((value) => <option key={value} value={value}>{value}</option>)}
            </select>
          </Chip>
        )}

        {plan.place && (
          <Chip label={plan.place.kind === "map-selection" ? "LOCATION" : `PLACE · ${plan.place.kind.toUpperCase()}`} onRemove={() => update({ place: null })}>
            {plan.place.kind === "map-selection" && plan.place.lon !== undefined
              ? `${plan.place.lat?.toFixed(4)}, ${plan.place.lon.toFixed(4)}`
              : plan.place.name}
          </Chip>
        )}

        {plan.distance && (
          <Chip label="DISTANCE" assumed={plan.distance.assumed} note={plan.distance.note} onRemove={() => update({ distance: null })}>
            <input
              className="chip__input"
              type="number"
              min={0}
              step={0.1}
              value={Math.round(plan.distance.value_m / 100) / 10}
              onChange={(e) => update({ distance: { ...plan.distance!, value_m: Number(e.target.value) * 1000, assumed: false } })}
              aria-label="Distance in kilometres"
            />
            <span className="chip__unit">km</span>
          </Chip>
        )}

        {(plan.reference || plan.intent === "find-changes") && (
          <Chip label="REFERENCE">
            <select
              className="chip__select"
              value={plan.reference?.kind ?? ""}
              onChange={(e) => update({ reference: e.target.value ? { kind: e.target.value, matched: plan.reference?.matched } : null })}
            >
              {REFERENCES.map((value) => <option key={value} value={value}>{value || "none"}</option>)}
            </select>
          </Chip>
        )}

        <Chip label="TIME" assumed={plan.time?.assumed} note={plan.time?.note} onRemove={plan.time ? () => update({ time: null }) : undefined}>
          <input
            className="chip__input chip__input--date"
            type="date"
            value={plan.time?.start?.slice(0, 10) ?? ""}
            onChange={(e) => update({ time: e.target.value ? { start: `${e.target.value}T00:00:00Z`, end: plan.time?.end ?? null, matched: plan.time?.matched, assumed: false } : null })}
            aria-label="From date"
          />
          <span className="chip__unit">→</span>
          <input
            className="chip__input chip__input--date"
            type="date"
            value={plan.time?.end?.slice(0, 10) ?? ""}
            onChange={(e) => update({ time: { start: plan.time?.start ?? "2018-01-01T00:00:00Z", end: e.target.value ? `${e.target.value}T00:00:00Z` : null, matched: plan.time?.matched, assumed: false } })}
            aria-label="To date"
          />
        </Chip>

        {plan.filters.min_magnitude && (
          <Chip label="MAGNITUDE ≥" onRemove={() => update({ filters: {} })}>
            <input
              className="chip__input"
              type="number"
              step={0.1}
              value={plan.filters.min_magnitude.value}
              onChange={(e) => update({ filters: { min_magnitude: { value: Number(e.target.value), matched: plan.filters.min_magnitude!.matched } } })}
              aria-label="Minimum magnitude"
            />
          </Chip>
        )}

        {plan.sensor && <Chip label="SENSOR">{plan.sensor}</Chip>}

        {plan.related_events && (
          <Chip label="OSINT CONTEXT" onRemove={() => update({ related_events: null })}>{plan.related_events.types.join(", ")} within 50 km</Chip>
        )}

        {plan.intent === "find-changes" && (
          <Chip label="SUPPRESSED">
            <label className="chip__check">
              <input type="checkbox" checked={!!plan.include_suppressed} onChange={(e) => update({ include_suppressed: e.target.checked })} />
              include
            </label>
          </Chip>
        )}
      </div>

      {plan.unrecognised.length > 0 && (
        <div className="plan__warning">NOT UNDERSTOOD: {plan.unrecognised.join(", ")} — these words were ignored, not guessed.</div>
      )}
      {plan.notes.map((note) => <div key={note} className="plan__warning">{note}</div>)}
    </div>
  );
}
