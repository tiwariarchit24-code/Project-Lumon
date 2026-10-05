import type { Layer } from "../types";

// ---------------------------------------------------------------------------
// Layer visibility rules, kept free of React and MapLibre so they can be
// unit-tested (src/utils/utils.test.ts).
//
//   layerChoices  the analyst's on/off choices, { layerId: true|false };
//                 a layer without a choice keeps its registry default
//   enabled       a layer is drawn only when it is chosen ON *and* its data
//                 is staged (available); unstaged layers can never be ON
// ---------------------------------------------------------------------------
export type LayerChoices = Record<string, boolean>;

// Registry layers with the analyst's choices applied.
export function applyLayerChoices<T extends Pick<Layer, "id" | "available" | "enabled">>(layers: T[], choices: LayerChoices): T[] {
  return layers.map((layer) => ({ ...layer, enabled: layer.available && (choices[layer.id] ?? layer.enabled) }));
}

// Flip one layer: ON becomes OFF and OFF becomes ON (starting from the
// registry default when the analyst has not chosen yet).
export function toggleLayerChoice(choices: LayerChoices, defaults: Pick<Layer, "id" | "enabled">[], id: string): LayerChoices {
  const current = choices[id] ?? defaults.find((l) => l.id === id)?.enabled ?? false;
  return { ...choices, [id]: !current };
}

// The MapLibre layer ids that belong to one registry layer, so the layer
// panel can switch them on and off together. Event and entity layers
// return [] because they share one map layer and are filtered by data type.
export function mapLayerIdsFor(layer: Pick<Layer, "id">): string[] {
  switch (layer.id) {
    case "india-boundary":
      return ["lyr-india-boundary-fill", "lyr-india-boundary-line"];
    case "coastal-boundary":
      return ["lyr-coastal-boundary-line"];
    case "states":
      return ["lyr-states-line"];
    case "districts":
      return ["lyr-districts-line"];
    case "cities":
      return ["lyr-cities-circle", "lyr-cities-label"];
    case "rivers":
      return ["lyr-rivers-line"];
    case "lakes":
      return ["lyr-lakes-fill"];
    case "roads":
      return ["lyr-roads-line"];
    case "railways":
      return ["lyr-railways-line"];
    case "aoi-footprints":
      return ["aois-fill", "aois-line", "aois-label"];
    case "pilot-nit-raipur":
      return ["lyr-pilot-nit-raipur-fill", "lyr-pilot-nit-raipur-line", "lyr-pilot-nit-raipur-label"];
    case "sentinel-2":
      return []; // image layers are added per AOI by MapWorkspace
    case "change-events":
      return ["changes-fill", "changes-line"];
    case "suppressed-changes":
      return ["suppressed-fill", "suppressed-line"];
    case "ml-change-candidates":
      return ["mlchange-fill", "mlchange-line"];
    case "discovery-cluster":
      return ["discovery-fill", "discovery-line"];
    default:
      return []; // events/entities layers are filtered by data, not by layer id
  }
}

// Layers drawn only from a minimum map zoom (too dense at India scale).
// The layer panel says so, so a switched-ON layer is never mistaken for broken.
export const LAYER_MIN_ZOOM: Record<string, number> = { districts: 4.5 };

// MapLibre "visibility" value for a layer.
export function visibilityFor(layer: Pick<Layer, "enabled" | "available">): "visible" | "none" {
  return layer.enabled && layer.available ? "visible" : "none";
}
