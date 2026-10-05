import { sectionLabel } from "../data/navigation";
import OverviewSection from "./drawer/OverviewSection";
import SearchSection from "./drawer/SearchSection";
import ChangesSection from "./drawer/ChangesSection";
import DiscoverSection from "./drawer/DiscoverSection";
import OsintSection from "./drawer/OsintSection";
import ImagerySection from "./drawer/ImagerySection";
import LearnedChangeSection from "./drawer/LearnedChangeSection";
import EngineSection from "./drawer/EngineSection";
import SourcesSection from "./drawer/SourcesSection";
import PilotSection from "./drawer/PilotSection";
import { AuditSection, DecisionsSection } from "./drawer/RecordsSection";
import { DataSection, SystemSection } from "./drawer/SystemSection";
import type { Layer, QueryPlan, QueryResultItem, QueryRun, Selection, SystemInfo, TimeWindow } from "../types";

// ---------------------------------------------------------------------------
// The drawer that slides out from the navigation rail, over the map.
// It shows one section at a time (chosen in the rail). Each section is its
// own small component in components/drawer/.
// ---------------------------------------------------------------------------
type LeftDrawerProps = {
  section: string;
  onClose: () => void;
  system: SystemInfo | null;
  layers: Layer[];
  timeWindow: TimeWindow;
  bbox: number[] | null;
  refreshKey: number;
  selection: Selection | null;
  plan: QueryPlan | null;
  run: QueryRun | null;
  running: boolean;
  onPlanChange: (plan: QueryPlan) => void;
  onRun: () => void;
  onOpenResult: (item: QueryResultItem) => void;
  onSelect: (selection: Selection) => void;
  onToggleLayer: (id: string) => void;
  onSetLayer: (id: string, enabled: boolean) => void;
  onFlyToBbox: (bbox: number[]) => void;
  onRefresh: () => void;
  sceneChoice: Record<string, string>;
  onShowScene: (aoiId: string, sceneId: string) => void;
  onShowMlRun: (runId: string) => void;
  onShowCluster: (clusterId: string, referenceChipId: string | null, bbox: number[]) => void;
  onOpenChipId: (chipId: string) => void;
  onOpenSection: (id: string) => void;
};

// Sections opened from inside other panels rather than from the navigation.
const EXTRA_TITLES: Record<string, string> = { pilot: "NIT Raipur Pilot" };

export default function LeftDrawer(props: LeftDrawerProps) {
  const { section } = props;
  const selectedChange = props.selection?.kind === "change" ? props.selection.id : null;

  let body;
  if (section === "overview") body = <OverviewSection system={props.system} timeWindow={props.timeWindow} onSelect={props.onSelect} onOpenPilot={() => props.onOpenSection("pilot")} />;
  else if (section === "search") body = <SearchSection plan={props.plan} run={props.run} running={props.running} onPlanChange={props.onPlanChange} onRun={props.onRun} onOpenResult={props.onOpenResult} onOpenPilot={() => props.onOpenSection("pilot")} />;
  else if (section === "pilot") body = <PilotSection onFlyToBbox={props.onFlyToBbox} />;
  else if (section === "changes" || section === "review") body = (
    <ChangesSection mode={section} refreshKey={props.refreshKey} selectedId={selectedChange} onSelect={props.onSelect}
      onShowSuppressed={(show) => props.onSetLayer("suppressed-changes", show)} />
  );
  else if (section === "discover") body = <DiscoverSection onSelect={props.onSelect} />;
  else if (section.startsWith("osint:")) body = (
    <OsintSection group={section.slice(6)} layers={props.layers} timeWindow={props.timeWindow} bbox={props.bbox} onToggle={props.onToggleLayer} onSelect={props.onSelect} />
  );
  else if (section === "imagery") body = <ImagerySection onFlyToBbox={props.onFlyToBbox} sceneChoice={props.sceneChoice} onShowScene={props.onShowScene} onOpenResult={props.onOpenResult}
    onShowCluster={props.onShowCluster} onOpenChipId={props.onOpenChipId} />;
  else if (section === "engine") body = (
    <>
      {/* Rule-based baseline first, then the separate learned model. */}
      <EngineSection onRan={props.onRefresh} />
      <LearnedChangeSection onSelect={props.onSelect} onSetLayer={props.onSetLayer} onShowScene={props.onShowScene}
        onFlyToBbox={props.onFlyToBbox} onRefresh={props.onRefresh} onShowRun={props.onShowMlRun} />
    </>
  );
  else if (section === "decisions") body = <DecisionsSection refreshKey={props.refreshKey} onSelect={props.onSelect} />;
  else if (section === "audit") body = <AuditSection refreshKey={props.refreshKey} />;
  else if (section === "sources") body = <SourcesSection />;
  else if (section === "data") body = <DataSection />;
  else body = <SystemSection system={props.system} />;

  return (
    <section className="drawer panel-enter" aria-label={EXTRA_TITLES[section] ?? sectionLabel(section)}>
      <div className="panel-header">
        <span className="panel-header__title">{(EXTRA_TITLES[section] ?? sectionLabel(section)).toUpperCase()}</span>
        <button type="button" className="icon-button" onClick={props.onClose} aria-label="Close panel">×</button>
      </div>
      <div className="drawer__body" key={section}>{body}</div>
    </section>
  );
}
