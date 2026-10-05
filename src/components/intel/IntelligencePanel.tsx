import EventDetail from "./EventDetail";
import EntityDetail from "./EntityDetail";
import ChangeDetail from "./ChangeDetail";
import TileDetail from "./TileDetail";
import ChipDetail from "./ChipDetail";
import MlChangeDetail from "./MlChangeDetail";
import { Facts, Section } from "./common";
import type { QueryResultItem, Selection, SystemInfo } from "../../types";

// ---------------------------------------------------------------------------
// The right-hand INTELLIGENCE panel. It is contextual: it shows whatever the
// analyst selected (event, entity, change, imagery tile, image-search chip,
// place). With
// nothing selected it shows the investigation workflow and live counts.
//
// It floats over the map and can be collapsed to a thin strip.
// ---------------------------------------------------------------------------
type IntelligencePanelProps = {
  selection: Selection | null;
  system: SystemInfo | null;
  open: boolean;
  onToggleOpen: () => void;
  onClear: () => void;
  onSelect: (selection: Selection) => void;
  onHighlight: (geometry: GeoJSON.Geometry | null) => void;
  onDecision: () => void;
  onOpenAoi: (aoiId: string) => void;
  onOpenResult: (item: QueryResultItem) => void; // image chips: detail + footprint + scene on the map
  onShowScene: (aoiId: string, sceneId: string) => void; // show a dated image as the Sentinel-2 layer
  onShowCluster: (clusterId: string, referenceChipId: string | null, bbox: number[]) => void; // discovery cluster on the map
};

// The default view: how an investigation flows, plus real counts.
function NoSelection({ system }: { system: SystemInfo | null }) {
  return (
    <div className="detail fade-in">
      <div className="intel-status"><span className="status-dot status-dot--neutral" aria-hidden="true" />NO ACTIVE SELECTION</div>
      <Section title="INVESTIGATION WORKFLOW">
        <ol className="workflow">
          <li>Select a layer, event, entity or location on the map</li>
          <li>Open its context here: source, provenance, related events</li>
          <li>Ask a question in the command bar; check and edit the plan</li>
          <li>Open satellite evidence: before/after, gates, honest dates</li>
          <li>Confirm, reject or relabel; every decision is audited</li>
          <li>Export results as GeoPackage (System → Data)</li>
        </ol>
      </Section>
      <Section title="STAGED NOW">
        {system ? (
          <Facts rows={[
            ["OSINT EVENTS", system.counts.events],
            ["ENTITIES", system.counts.entities],
            ["SCENES", `${system.counts.scenes_usable} usable · ${system.counts.scenes_quarantined} quarantined`],
            ["CHANGE EVENTS", `${system.counts.changes_accepted} accepted · ${system.counts.changes_suppressed} suppressed`],
            ["DECISIONS", system.counts.decisions],
          ]} />
        ) : <div className="empty-note">Lumon API unreachable.</div>}
      </Section>
    </div>
  );
}

export default function IntelligencePanel(props: IntelligencePanelProps) {
  const { selection, open } = props;

  if (!open) {
    return (
      <aside className="intel intel--collapsed" aria-label="Intelligence panel (collapsed)">
        <button type="button" className="intel__expand" onClick={props.onToggleOpen} aria-label="Expand intelligence panel" title="Expand intelligence panel">
          <span aria-hidden="true">‹</span>
          <span className="intel__expand-label">INTELLIGENCE</span>
        </button>
      </aside>
    );
  }

  let body;
  if (!selection) {
    body = <NoSelection system={props.system} />;
  } else if (selection.kind === "event") {
    body = <EventDetail id={selection.id} onHighlight={props.onHighlight} onSelectEvent={(id) => props.onSelect({ kind: "event", id })} onOpenAoi={props.onOpenAoi} />;
  } else if (selection.kind === "entity") {
    body = <EntityDetail id={selection.id} onHighlight={props.onHighlight} onSelectEvent={(id) => props.onSelect({ kind: "event", id })} onSelectEntity={(id) => props.onSelect({ kind: "entity", id })} />;
  } else if (selection.kind === "change") {
    body = <ChangeDetail id={selection.id} onHighlight={props.onHighlight} onDecision={props.onDecision} onSelectTile={(lon, lat) => props.onSelect({ kind: "tile", lon, lat })} />;
  } else if (selection.kind === "tile") {
    body = <TileDetail lon={selection.lon} lat={selection.lat} onHighlight={props.onHighlight} onSelectChange={(id) => props.onSelect({ kind: "change", id })} onSelectTile={(lon, lat) => props.onSelect({ kind: "tile", lon, lat })} onOpenChip={props.onOpenResult}
      onShowCluster={props.onShowCluster} />;
  } else if (selection.kind === "ml-change") {
    body = <MlChangeDetail id={selection.id} onHighlight={props.onHighlight} onShowScene={props.onShowScene}
      onSelectChange={(id) => props.onSelect({ kind: "change", id })} />;
  } else if (selection.kind === "chip") {
    body = <ChipDetail id={selection.id} score={selection.score} rank={selection.rank} query={selection.query} scoreKind={selection.scoreKind}
      onHighlight={props.onHighlight} onOpenTile={(lon, lat) => props.onSelect({ kind: "tile", lon, lat })} onOpenChip={props.onOpenResult}
      onShowCluster={props.onShowCluster} />;
  } else {
    // A place from the gazetteer, or a plain map location.
    const name = selection.kind === "place" ? selection.name : "Selected location";
    body = (
      <div className="detail fade-in">
        <div className="detail__kind">{selection.kind === "place" ? `PLACE · ${selection.placeKind.toUpperCase()}` : "LOCATION"}</div>
        <h2 className="detail__title">{name}</h2>
        <Section title="POSITION">
          <Facts rows={[["LATITUDE", selection.lat.toFixed(5)], ["LONGITUDE", selection.lon.toFixed(5)], ["SOURCE", selection.kind === "place" ? "Staged gazetteer (Natural Earth / geoBoundaries)" : "Map click"]]} />
        </Section>
        <Section title="USE IN A QUESTION">
          <div className="empty-note">Ask about “this location”, e.g. <em>changes around this location</em> or <em>earthquakes near this location</em>. The plan uses these coordinates.</div>
        </Section>
      </div>
    );
  }

  return (
    <aside className="intel panel-enter" aria-label="Intelligence panel">
      <div className="panel-header">
        <span className="panel-header__title">INTELLIGENCE</span>
        {selection && <button type="button" className="text-button" onClick={props.onClear}>Clear</button>}
        <button type="button" className="icon-button" onClick={props.onToggleOpen} aria-label="Collapse intelligence panel" title="Collapse">›</button>
      </div>
      <div className="intel__body">{body}</div>
    </aside>
  );
}
