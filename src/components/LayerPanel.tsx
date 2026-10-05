import { useState } from "react";
import type { Layer } from "../types";
import { formatAge } from "../utils/format";
import InfoTip from "./InfoTip";
import LayerSwitch from "./LayerSwitch";
import { LAYER_MIN_ZOOM } from "../utils/layerVisibility";
import { INFO } from "../data/infoNotes";

// ---------------------------------------------------------------------------
// The layer stack: every map layer from the layer registry (/api/layers),
// grouped (REFERENCE, AVIATION, ...), each with an ON/OFF switch when its
// data is staged, or an honest status ("AVAILABLE LATER", "NO RECORDS IN
// SNAPSHOT"...) when it is not.
//
// Two separate actions per row:
//   - the SWITCH shows or hides the layer on the map
//   - clicking the NAME selects the row and opens its details (what it
//     shows, coverage, provider); it never changes visibility
//
// Props:
//  - groups / layers: from the registry
//  - onToggle:        switch one layer on/off
//  - onClose:         close the panel
// ---------------------------------------------------------------------------
type LayerPanelProps = {
  groups: string[];
  layers: Layer[];
  onToggle: (layerId: string) => void;
  onClose: () => void;
};

export default function LayerPanel({ groups, layers, onToggle, onClose }: LayerPanelProps) {
  // Groups the analyst has collapsed (to keep the list short).
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [onlyAvailable, setOnlyAvailable] = useState(false);
  // The selected row (details open). Independent of ON/OFF.
  const [selected, setSelected] = useState<string | null>(null);

  function toggleGroup(group: string) {
    const next = new Set(collapsed);
    if (next.has(group)) next.delete(group);
    else next.add(group);
    setCollapsed(next);
  }

  const availableCount = layers.filter((l) => l.available).length;
  const shownCount = layers.filter((l) => l.enabled).length;

  return (
    <section className="layer-panel panel-enter" aria-label="Map layers">
      <div className="panel-header">
        <span className="panel-header__title">
          LAYERS
          <InfoTip {...INFO.layers} state={`${shownCount} on map · ${availableCount} of ${layers.length} staged · ${layers.filter((l) => l.freshness === "STALE").length} stale`} />
        </span>
        <span className="panel-header__meta">{shownCount} ON · {availableCount}/{layers.length} STAGED</span>
        <button type="button" className="icon-button" onClick={onClose} aria-label="Close layer panel">×</button>
      </div>
      <label className="layer-panel__filter">
        <input type="checkbox" checked={onlyAvailable} onChange={(e) => setOnlyAvailable(e.target.checked)} />
        Show only staged layers
      </label>

      {layers.length === 0 && <div className="empty-note panel-pad">No layers loaded — Lumon API unreachable.</div>}

      <div className="layer-panel__body">
        {groups.map((group) => {
          const groupLayers = layers.filter((l) => l.group === group && (!onlyAvailable || l.available));
          if (groupLayers.length === 0) return null;
          const isCollapsed = collapsed.has(group);
          return (
            <div className="layer-group" key={group}>
              <button type="button" className="layer-group__title" onClick={() => toggleGroup(group)} aria-expanded={!isCollapsed}>
                <span className={isCollapsed ? "caret" : "caret caret--open"} aria-hidden="true">▸</span>
                {group}
                <span className="layer-group__count">{groupLayers.filter((l) => l.enabled).length}/{groupLayers.length}</span>
              </button>
              {!isCollapsed && groupLayers.map((layer) => (
                <div key={layer.id} className="layer-item">
                <div className={`layer-row${layer.available ? "" : " layer-row--unavailable"}${selected === layer.id ? " layer-row--selected" : ""}`}>
                  <span className="layer-row__swatch" style={{ background: layer.available ? layer.color ?? "#7d8896" : "transparent" }} aria-hidden="true" />
                  <button type="button" className="layer-row__name layer-row__select" aria-expanded={selected === layer.id}
                    title="Show details (does not change visibility)" onClick={() => setSelected(selected === layer.id ? null : layer.id)}>
                    <span className="layer-row__label">{layer.name}</span>
                    {/* Count plus the honest freshness: LIVE / SNAPSHOT / STALE with age
                        for source-fed layers, staging time for boundaries and imagery. */}
                    <span className={`layer-row__meta${layer.freshness === "STALE" ? " layer-row__meta--stale" : ""}`}>
                      {layer.available
                        ? `${layer.count ?? "—"} · ${layer.freshness ? layer.status_text : `STAGED${layer.updated_at ? ` · ${formatAge(layer.updated_at)}` : ""}`}`
                        : layer.status_text}
                      {layer.available && LAYER_MIN_ZOOM[layer.id] ? ` · from zoom ${LAYER_MIN_ZOOM[layer.id]}` : ""}
                    </span>
                  </button>
                  {layer.available ? (
                    <LayerSwitch name={layer.name} on={layer.enabled} onToggle={() => onToggle(layer.id)} />
                  ) : (
                    <span className="layer-row__status" title={layer.status_text}>{layer.kind === "planned" ? "LATER" : "N/A"}</span>
                  )}
                </div>
                {selected === layer.id && (
                  <div className="layer-row__details fade-in">
                    <div>{layer.description}</div>
                    {layer.coverage && <div><span className="layer-row__key">COVERAGE</span> {layer.coverage}</div>}
                    {layer.source && <div><span className="layer-row__key">PROVIDER</span> {layer.source}</div>}
                    <div><span className="layer-row__key">ON MAP</span> {!layer.available ? `no (${layer.status_text})` : layer.enabled ? "yes" : "no — switch it ON to show it"}</div>
                    {LAYER_MIN_ZOOM[layer.id] && <div><span className="layer-row__key">ZOOM</span> drawn only from zoom {LAYER_MIN_ZOOM[layer.id]}; zoom in to see it</div>}
                  </div>
                )}
                </div>
              ))}
            </div>
          );
        })}
      </div>
    </section>
  );
}
