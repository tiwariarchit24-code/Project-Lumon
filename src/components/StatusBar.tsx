import { useEffect, useState } from "react";
import StatusIndicator from "./StatusIndicator";
import { formatAge } from "../utils/format";
import type { EventsShown, SystemInfo, SystemStatus } from "../types";

// ---------------------------------------------------------------------------
// Narrow status bar along the bottom. Every value is real: read from the
// backend, counted on the map, or measured (API latency is the round-trip
// time of the last health check). "—" means not available.
//
// MODE says what the backend is ALLOWED to do (download or not); it is not
// a network test. DATA says how fresh the data actually is.
// ---------------------------------------------------------------------------
type StatusBarProps = {
  system: SystemInfo | null;
  apiOnline: boolean;
  apiLatencyMs: number | null;
  eventsShown: EventsShown;
};

// "3.4 h" style age for compact labels.
function shortAge(hours: number): string {
  if (hours < 1) return `${Math.max(1, Math.round(hours * 60))} min`;
  if (hours < 48) return `${hours.toFixed(1)} h`;
  return `${(hours / 24).toFixed(1)} d`;
}

// How fresh the OSINT data is, from the sources' own statuses:
//   LIVE              at least one source was downloaded recently while connected
//   SNAPSHOT · age    the newest data is a staged copy (air-gapped, or not refreshed)
//   STALE · age       every staged source is older than its freshness limit
// This replaces the old "NET CONNECTED", which only echoed the configured
// mode and never tested the network.
export function dataFreshness(system: SystemInfo | null): SystemStatus {
  if (!system) return { id: "data", label: "DATA", value: "—", tone: "neutral" };
  const byStatus = system.sources.by_status ?? {};
  const age = system.sources.newest_download_age_hours;
  if (byStatus["LIVE"]) return { id: "data", label: "DATA", value: `LIVE · ${byStatus["LIVE"]} SRC`, tone: "ok" };
  if (age === null) return { id: "data", label: "DATA", value: "NOT STAGED", tone: "pending" };
  if (byStatus["SNAPSHOT"]) return { id: "data", label: "DATA", value: `SNAPSHOT · ${shortAge(age)} OLD`, tone: "neutral" };
  return { id: "data", label: "DATA", value: `STALE · ${shortAge(age)} OLD`, tone: "pending" };
}

export default function StatusBar({ system, apiOnline, apiLatencyMs, eventsShown }: StatusBarProps) {
  const [now, setNow] = useState(new Date());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const items: SystemStatus[] = [
    { id: "api", label: "API", value: apiOnline ? "ONLINE" : "OFFLINE", tone: apiOnline ? "ok" : "error" },
    { id: "mode", label: "MODE", value: system ? (system.mode === "connected" ? "INGEST ENABLED" : "AIR-GAPPED") : "—", tone: "neutral" },
    dataFreshness(system),
    { id: "aoi", label: "AOI", value: system?.counts.scenes_usable ? "DEMO-01" : "NONE", tone: "neutral" },
    {
      id: "view", label: "EVENTS IN VIEW",
      value: eventsShown.scheduled ? `${eventsShown.observed} + ${eventsShown.scheduled} SCHEDULED` : String(eventsShown.observed),
      tone: "neutral",
    },
    { id: "scenes", label: "SCENES", value: system ? String(system.counts.scenes_usable) : "—", tone: "neutral" },
    { id: "ingest", label: "LAST INGEST", value: system?.last_source_success ? formatAge(system.last_source_success) : "—", tone: "neutral" },
    { id: "latency", label: "LATENCY", value: apiLatencyMs === null ? "—" : `${apiLatencyMs} ms`, tone: "neutral" },
  ];

  return (
    <footer className="statusbar">
      <span className="statusbar__product">LUMON COMMAND</span>
      <div className="statusbar__items">
        {items.map((status) => <StatusIndicator key={status.id} status={status} compact />)}
      </div>
      <span className="statusbar__clock">UTC {now.toISOString().slice(11, 19)}</span>
    </footer>
  );
}
