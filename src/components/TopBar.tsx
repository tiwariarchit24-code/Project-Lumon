import Logo from "./Logo";
import CommandSearch from "./CommandSearch";
import StatusIndicator from "./StatusIndicator";
import type { QueryPlan, SystemInfo, SystemStatus } from "../types";

// ---------------------------------------------------------------------------
// Thin header across the top of the workstation:
//   left   - Project Lumon logo and product name
//   middle - command search (places, and natural-language questions)
//   right  - live system indicators computed from /api/system
// ---------------------------------------------------------------------------
type TopBarProps = {
  system: SystemInfo | null;
  apiOnline: boolean;
  onPlace: (place: { name: string; kind: string; lon: number; lat: number; bbox?: number[] | null }) => void;
  onPlan: (plan: QueryPlan) => void;
};

// Turn the system report into four header indicators. Every value here is
// read from the backend; nothing is assumed.
function headerStatus(system: SystemInfo | null, apiOnline: boolean): SystemStatus[] {
  if (!apiOnline || !system) {
    return [{ id: "api", label: "LUMON API", value: "UNREACHABLE", tone: "error" }];
  }
  // Source counts by actual freshness (see sources/registry.py on the backend).
  const byStatus = system.sources.by_status ?? {};
  const live = byStatus["LIVE"] ?? 0;
  const delayed = byStatus["DELAYED"] ?? 0;
  const snapshot = byStatus["SNAPSHOT"] ?? 0;
  const stale = byStatus["STALE"] ?? 0;
  const parts = [live && `${live} LIVE`, delayed && `${delayed} DELAYED`, snapshot && `${snapshot} SNAP`, stale && `${stale} STALE`].filter(Boolean);
  const modelsStaged = system.models.filter((m) => m.status !== "NOT STAGED").length;
  return [
    // MODE is what the backend may do (download or not), not a network test.
    { id: "mode", label: "MODE", value: system.mode === "connected" ? "INGEST ENABLED" : "AIR-GAPPED", tone: system.mode === "connected" ? "pending" : "ok" },
    { id: "boundary", label: "BOUNDARY", value: system.boundary.staged ? "INDIA STAGED" : "NOT STAGED", tone: system.boundary.staged ? "ok" : "pending" },
    { id: "sources", label: "SOURCES", value: parts.length ? parts.join(" · ") : "NONE STAGED", tone: live ? "ok" : stale ? "pending" : snapshot ? "neutral" : "pending" },
    { id: "archive", label: "ARCHIVE", value: system.counts.scenes_usable ? `${system.counts.scenes_usable} SCENES` : "NO IMAGERY", tone: system.counts.scenes_usable ? "ok" : "pending" },
    { id: "models", label: "MODELS", value: modelsStaged ? `${modelsStaged} STAGED` : "NOT STAGED", tone: modelsStaged ? "ok" : "pending" },
  ];
}

export default function TopBar({ system, apiOnline, onPlace, onPlan }: TopBarProps) {
  return (
    <header className="topbar">
      <Logo />
      <div className="topbar__center">
        <CommandSearch apiOnline={apiOnline} onPlace={onPlace} onPlan={onPlan} />
      </div>
      <div className="topbar__status" aria-label="System status">
        {headerStatus(system, apiOnline).map((status) => (
          <StatusIndicator key={status.id} status={status} />
        ))}
      </div>
    </header>
  );
}
