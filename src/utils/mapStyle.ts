import type { Map as MapLibreMap, StyleSpecification, ExpressionSpecification } from "maplibre-gl";
import { buildGraticule } from "./graticule";
import type { Layer } from "../types";
import { LAYER_MIN_ZOOM } from "./layerVisibility";

// ---------------------------------------------------------------------------
// The MapLibre style and the Lumon overlay layers.
//
// The base style is fully local: a dark background and a latitude/longitude
// grid computed in the browser. Text labels use glyph files bundled in
// public/fonts, so nothing is downloaded from the internet.
//
// All Lumon data layers (boundaries, events, changes...) are added once when
// the map loads, with empty data and hidden. MapWorkspace later fills them
// with data from the local API and switches them on and off.
// ---------------------------------------------------------------------------

export const INDIA_VIEW_CENTER: [number, number] = [79, 22.5];
export const INDIA_VIEW_ZOOM = 3.7;
export const GLOBE_VIEW_ZOOM = 1.7;

export const GRID_LAYER_IDS = ["grid-minor", "grid-major", "grid-reference"];
export const FONT = ["Noto Sans Regular"];

const COLORS = {
  background: "#080b10",
  gridMinor: "#141c25",
  gridMajor: "#1f2a36",
  gridReference: "#3d5670",
};

const EMPTY = { type: "FeatureCollection" as const, features: [] };

// The base style passed to `new maplibregl.Map(...)`.
export function buildLocalMapStyle(): StyleSpecification {
  return {
    version: 8,
    // MapLibre needs an absolute URL template for glyphs.
    glyphs: `${window.location.origin}/fonts/{fontstack}/{range}.pbf`,
    sources: {
      grid: { type: "geojson", data: buildGraticule(2.5, 4) },
    },
    layers: [
      { id: "background", type: "background", paint: { "background-color": COLORS.background } },
      { id: "grid-minor", type: "line", source: "grid", filter: ["==", ["get", "kind"], "minor"], paint: { "line-color": COLORS.gridMinor, "line-width": 0.6 } },
      { id: "grid-major", type: "line", source: "grid", filter: ["==", ["get", "kind"], "major"], paint: { "line-color": COLORS.gridMajor, "line-width": 0.9 } },
      { id: "grid-reference", type: "line", source: "grid", filter: ["==", ["get", "kind"], "reference"], paint: { "line-color": COLORS.gridReference, "line-width": 1, "line-dasharray": [4, 3] } },
    ],
  };
}

// mapLayerIdsFor lives in layerVisibility.ts (dependency-free, unit-tested).
export { mapLayerIdsFor } from "./layerVisibility";

// Which data file feeds each boundary/reference layer.
export function dataUrlFor(layer: Layer): string | null {
  if (layer.kind === "boundary" && layer.dataset) return `/api/boundaries/${layer.dataset}.geojson`;
  if (layer.kind === "reference" && layer.source_id) return `/api/reference/${layer.source_id}.geojson`;
  // Pilot study area: provisional today; a verified boundary file replaces
  // it on the backend without any change here.
  if (layer.kind === "pilot" && layer.pilot) return `/api/pilots/${layer.pilot}/aoi.geojson`;
  return null;
}

// The MapLibre source id used by a boundary/reference layer.
export function sourceIdFor(layer: Layer): string {
  return `src-${layer.id}`;
}

// A colour expression: pick each point's colour from its "layer" property,
// using the colours in the layer registry.
export function colorByLayer(layers: Layer[]): ExpressionSpecification {
  const pairs: string[] = [];
  for (const layer of layers) {
    if (layer.color && (layer.kind === "events" || layer.kind === "entities")) {
      pairs.push(layer.id, layer.color);
    }
  }
  if (pairs.length === 0) return ["to-color", "#9fb4c8"];
  return ["match", ["get", "layer"], ...pairs, "#9fb4c8"] as unknown as ExpressionSpecification;
}

// Add every overlay source and layer (empty, hidden) in drawing order:
// areas first, then lines, then points, then labels and selection on top.
export function addOverlayLayers(map: MapLibreMap): void {
  const hidden = { visibility: "none" as const };
  const addSource = (id: string, cluster = false) =>
    map.addSource(id, cluster
      ? { type: "geojson", data: EMPTY, cluster: true, clusterRadius: 38, clusterMaxZoom: 9 }
      : { type: "geojson", data: EMPTY });

  // Boundaries and reference geography.
  for (const id of ["src-india-boundary", "src-coastal-boundary", "src-states", "src-districts", "src-cities", "src-rivers", "src-lakes", "src-roads", "src-railways"]) {
    addSource(id);
  }
  map.addLayer({ id: "lyr-coastal-boundary-line", type: "line", source: "src-coastal-boundary", layout: hidden, paint: { "line-color": "#2c5f84", "line-width": 1, "line-dasharray": [3, 2] } });
  map.addLayer({ id: "lyr-india-boundary-fill", type: "fill", source: "src-india-boundary", layout: hidden, paint: { "fill-color": "#16212d", "fill-opacity": 0.55 } });
  map.addLayer({ id: "lyr-lakes-fill", type: "fill", source: "src-lakes", layout: hidden, paint: { "fill-color": "#173a55", "fill-opacity": 0.8 } });
  map.addLayer({ id: "lyr-districts-line", type: "line", source: "src-districts", minzoom: LAYER_MIN_ZOOM["districts"], layout: hidden, paint: { "line-color": "#2a3542", "line-width": 0.5 } });
  map.addLayer({ id: "lyr-states-line", type: "line", source: "src-states", layout: hidden, paint: { "line-color": "#4a5b6d", "line-width": ["interpolate", ["linear"], ["zoom"], 3, 0.6, 7, 1.2] } });
  map.addLayer({ id: "lyr-rivers-line", type: "line", source: "src-rivers", layout: hidden, paint: { "line-color": "#2a6a98", "line-width": ["interpolate", ["linear"], ["zoom"], 3, 0.6, 8, 1.6], "line-opacity": 0.85 } });
  map.addLayer({ id: "lyr-roads-line", type: "line", source: "src-roads", layout: hidden, paint: { "line-color": "#6b5a3e", "line-width": 0.8 } });
  map.addLayer({ id: "lyr-railways-line", type: "line", source: "src-railways", layout: hidden, paint: { "line-color": "#7a6a8a", "line-width": 0.8, "line-dasharray": [2, 2] } });
  map.addLayer({ id: "lyr-india-boundary-line", type: "line", source: "src-india-boundary", layout: hidden, paint: { "line-color": "#9fb4c8", "line-width": ["interpolate", ["linear"], ["zoom"], 3, 1, 8, 1.8] } });

  // NIT Raipur pilot study area: a quiet dashed outline. Its label carries
  // the geometry status (e.g. "NIT RAIPUR · PROVISIONAL").
  addSource("src-pilot-nit-raipur");
  map.addLayer({ id: "lyr-pilot-nit-raipur-fill", type: "fill", source: "src-pilot-nit-raipur", layout: hidden, paint: { "fill-color": "#c8c2e8", "fill-opacity": 0.04 } });
  map.addLayer({ id: "lyr-pilot-nit-raipur-line", type: "line", source: "src-pilot-nit-raipur", layout: hidden, paint: { "line-color": "#c8c2e8", "line-width": 1, "line-dasharray": [1.5, 2], "line-opacity": 0.8 } });

  // Satellite AOIs and change events.
  addSource("aois");
  addSource("changes");
  addSource("suppressed");
  addSource("mlchange");
  addSource("discovery");
  map.addLayer({ id: "aois-fill", type: "fill", source: "aois", layout: hidden, paint: { "fill-color": "#d2a24c", "fill-opacity": 0.06 } });
  // Satellite image overlays are inserted just below this marker layer.
  map.addLayer({ id: "imagery-anchor", type: "background", layout: hidden, paint: { "background-opacity": 0 } });
  map.addLayer({ id: "aois-line", type: "line", source: "aois", layout: hidden, paint: { "line-color": "#d2a24c", "line-width": 1.2, "line-dasharray": [3, 2] } });
  map.addLayer({ id: "suppressed-fill", type: "fill", source: "suppressed", layout: hidden, paint: { "fill-color": "#6b7480", "fill-opacity": 0.25 } });
  map.addLayer({ id: "suppressed-line", type: "line", source: "suppressed", layout: hidden, paint: { "line-color": "#8a939e", "line-width": 0.8, "line-dasharray": [2, 2] } });
  // Discovery cluster members (one footprint per place): cyan; the reference place in white.
  map.addLayer({ id: "discovery-fill", type: "fill", source: "discovery", layout: hidden, paint: { "fill-color": "#4dd0e1", "fill-opacity": 0.18 } });
  map.addLayer({ id: "discovery-line", type: "line", source: "discovery", layout: hidden, paint: { "line-color": ["case", ["==", ["get", "is_reference_place"], true], "#ffffff", "#4dd0e1"], "line-width": ["case", ["==", ["get", "is_reference_place"], true], 2.2, 1.4] } });
  // Model-generated candidate changes (BTC-B): magenta, dashed outline, so they
  // are never mistaken for the rule-based change events (orange).
  map.addLayer({ id: "mlchange-fill", type: "fill", source: "mlchange", layout: hidden, paint: { "fill-color": "#e040fb", "fill-opacity": ["case", ["==", ["get", "review_status"], "rejected"], 0.06, 0.28] } });
  map.addLayer({ id: "mlchange-line", type: "line", source: "mlchange", layout: hidden, paint: { "line-color": "#e040fb", "line-width": 1.2, "line-dasharray": [3, 1.5], "line-opacity": ["case", ["==", ["get", "review_status"], "rejected"], 0.35, 1] } });
  map.addLayer({ id: "changes-fill", type: "fill", source: "changes", layout: hidden, paint: { "fill-color": "#ff9f43", "fill-opacity": ["case", ["==", ["get", "review_state"], "rejected"], 0.12, 0.45] } });
  map.addLayer({ id: "changes-line", type: "line", source: "changes", layout: hidden, paint: { "line-color": ["case", ["==", ["get", "review_state"], "confirmed"], "#4fb286", "#ff9f43"], "line-width": 1.4 } });

  // OSINT entities and events (clustered points).
  addSource("entities", true);
  addSource("events", true);
  for (const source of ["entities", "events"]) {
    // Event clusters are blue; entity (infrastructure / places) clusters are
    // warm grey-amber with a double-weight ring, so the two never look alike.
    map.addLayer({ id: `${source}-clusters`, type: "circle", source, filter: ["has", "point_count"], paint: {
      "circle-color": source === "events" ? "#1f3a55" : "#3a3324",
      "circle-stroke-color": source === "events" ? "#7fb4dd" : "#c9a64f",
      "circle-stroke-width": source === "events" ? 1 : 2,
      "circle-radius": ["step", ["get", "point_count"], 9, 10, 12, 50, 16, 200, 20],
    } });
    map.addLayer({ id: `${source}-cluster-count`, type: "symbol", source, filter: ["has", "point_count"], layout: {
      "text-field": ["get", "point_count_abbreviated"], "text-font": FONT, "text-size": 10, "text-allow-overlap": true,
    }, paint: { "text-color": source === "events" ? "#d3dae3" : "#efe2bd" } });
    // Event points: scheduled (future) events are hollow rings; stale
    // telemetry (old aircraft/satellite positions) is faded with an amber
    // ring. Entity points have a light outline instead of a dark one.
    map.addLayer({ id: `${source}-points`, type: "circle", source, filter: ["!", ["has", "point_count"]], paint: {
      "circle-radius": source === "events" ? ["interpolate", ["linear"], ["coalesce", ["get", "magnitude"], 3], 2.5, 3.5, 6, 8] : 3.5,
      "circle-color": "#9fb4c8",
      "circle-stroke-color": source === "events" ? "#080b10" : "#e6d9b0",
      "circle-stroke-width": source === "events" ? ["case", ["==", ["get", "scheduled"], true], 1.8, 1] : 0.8,
      "circle-opacity": source === "events"
        ? ["case", ["==", ["get", "scheduled"], true], 0, ["==", ["get", "stale"], true], 0.45, 0.95]
        : 0.95,
    } });
  }

  // City and AOI labels (above points so they stay readable).
  // Only the larger cities (Natural Earth scale rank <= 4) get a dot and a label, to keep the map uncluttered.
  map.addLayer({ id: "lyr-cities-circle", type: "circle", source: "src-cities", filter: ["<=", ["coalesce", ["get", "rank"], 10], 4], layout: hidden, paint: { "circle-radius": 1.8, "circle-color": "#c9d3dd" } });
  map.addLayer({ id: "lyr-cities-label", type: "symbol", source: "src-cities", filter: ["<=", ["coalesce", ["get", "rank"], 10], 4], layout: {
    visibility: "none", "text-field": ["get", "name"], "text-font": FONT, "text-size": 10, "text-offset": [0, 0.9],
    "text-anchor": "top", "symbol-sort-key": ["coalesce", ["get", "rank"], 10],
  }, paint: { "text-color": "#8f9ba8", "text-halo-color": "#080b10", "text-halo-width": 1.2 } });
  map.addLayer({ id: "lyr-pilot-nit-raipur-label", type: "symbol", source: "src-pilot-nit-raipur", minzoom: 6, layout: {
    visibility: "none", "text-field": ["get", "label"], "text-font": FONT, "text-size": 10,
    "text-anchor": "top-left", "text-offset": [0.4, 0.4], "symbol-placement": "point",
  }, paint: { "text-color": "#c8c2e8", "text-halo-color": "#080b10", "text-halo-width": 1.2 } });
  map.addLayer({ id: "aois-label", type: "symbol", source: "aois", layout: {
    visibility: "none", "text-field": ["concat", ["get", "label"], " · ", ["get", "name"]], "text-font": FONT, "text-size": 10,
    "text-anchor": "bottom-left", "text-offset": [0, -0.4],
  }, paint: { "text-color": "#d2a24c", "text-halo-color": "#080b10", "text-halo-width": 1.2 } });

  // Measurement line and the analyst's current selection, always on top.
  addSource("measure");
  addSource("selection");
  map.addLayer({ id: "measure-line", type: "line", source: "measure", paint: { "line-color": "#eef2f6", "line-width": 1.4, "line-dasharray": [2, 1.5] } });
  map.addLayer({ id: "measure-points", type: "circle", source: "measure", filter: ["==", ["geometry-type"], "Point"], paint: { "circle-radius": 3, "circle-color": "#eef2f6" } });
  map.addLayer({ id: "selection-fill", type: "fill", source: "selection", filter: ["==", ["geometry-type"], "Polygon"], paint: { "fill-color": "#5fa8d3", "fill-opacity": 0.12 } });
  map.addLayer({ id: "selection-line", type: "line", source: "selection", paint: { "line-color": "#5fa8d3", "line-width": 2 } });
  map.addLayer({ id: "selection-point", type: "circle", source: "selection", filter: ["==", ["geometry-type"], "Point"], paint: {
    "circle-radius": 9, "circle-color": "rgba(0,0,0,0)", "circle-stroke-color": "#5fa8d3", "circle-stroke-width": 2,
  } });
}

// Layers that react to clicks, in priority order (top first).
export const CLICKABLE_LAYERS = ["events-clusters", "entities-clusters", "events-points", "entities-points", "changes-fill", "suppressed-fill", "mlchange-fill", "discovery-fill"];
