import { useEffect, useState } from "react";
import { getJson, query } from "../../api";
import { formatAge, formatTime } from "../../utils/format";
import { Facts, ListButton, PanelState, Section } from "../intel/common";
import type { Selection, SystemInfo, TimeWindow } from "../../types";

// ---------------------------------------------------------------------------
// OVERVIEW: what is staged, how fresh it is, and the latest events inside
// India for the current time window.
// ---------------------------------------------------------------------------
type Feature = { properties: { id: string; title: string; time: string; category: string } };

type OverviewProps = { system: SystemInfo | null; timeWindow: TimeWindow; onSelect: (selection: Selection) => void; onOpenPilot: () => void };

export default function OverviewSection({ system, timeWindow, onSelect, onOpenPilot }: OverviewProps) {
  const [latest, setLatest] = useState<Feature[] | null>(null);

  useEffect(() => {
    // Latest notable events (weather model points and aircraft are excluded:
    // they are continuous telemetry, not events an analyst reviews).
    const types = "earthquake,flood,cyclone,severe-storm,landslide,wildfire,fire-detection,drought,launch,volcano,disaster-alert,natural-event";
    getJson<{ features: Feature[] }>(`/api/events${query({ start: timeWindow.start, end: timeWindow.end, types, limit: 25 })}`)
      .then((data) => setLatest(data.features))
      .catch(() => setLatest(null));
  }, [timeWindow]);

  if (!system) return <PanelState state="error" text="LUMON API UNREACHABLE — start the backend (see README)" />;

  return (
    <>
      <Section title="OPERATING PICTURE">
        <Facts rows={[
          ["MODE", system.mode === "connected" ? "CONNECTED INGEST" : "AIR-GAPPED ANALYSIS"],
          ["BOUNDARY", system.boundary.staged ? "India land + EEZ staged" : `NOT STAGED (${system.boundary.missing.join(", ")})`],
          ["OSINT EVENTS", system.counts.events],
          ["ENTITIES", system.counts.entities],
          ["SCENES", `${system.counts.scenes_usable} usable, latest ${formatTime(system.latest_scene)}`],
          ["CHANGES", `${system.counts.changes_accepted} accepted / ${system.counts.changes_suppressed} suppressed`],
          ["LAST INGEST", system.last_source_success ? `${formatTime(system.last_source_success)} (${formatAge(system.last_source_success)})` : null],
        ]} />
      </Section>
      <Section title="PILOT" meta="NIT Raipur">
        <div className="empty-note">Detailed demonstrator inside India-wide coverage: study area, pilot data and AI/ML readiness.</div>
        <button type="button" className="action-button" onClick={onOpenPilot}>OPEN NIT RAIPUR PILOT</button>
      </Section>
      <Section title="LATEST EVENTS" meta={timeWindow.label}>
        {latest === null && <div className="empty-note">Events unavailable.</div>}
        {latest && latest.length === 0 && <div className="empty-note">NO EVENTS IN WINDOW</div>}
        {latest?.map((feature) => (
          <ListButton key={feature.properties.id} title={feature.properties.title} meta={`${formatTime(feature.properties.time)} · ${feature.properties.category}`}
            onClick={() => onSelect({ kind: "event", id: feature.properties.id })} />
        ))}
      </Section>
    </>
  );
}
