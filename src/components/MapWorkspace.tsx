import { useEffect, useRef, useState } from "react";
import * as maplibregl from "maplibre-gl";
import "../utils/maplibreWorker"; // must run before the first map is created
import MapControls from "./MapControls";
import { getJson, query } from "../api";
import {
  addOverlayLayers,
  buildLocalMapStyle,
  CLICKABLE_LAYERS,
  colorByLayer,
  dataUrlFor,
  GLOBE_VIEW_ZOOM,
  GRID_LAYER_IDS,
  INDIA_VIEW_CENTER,
  INDIA_VIEW_ZOOM,
  mapLayerIdsFor,
  sourceIdFor,
} from "../utils/mapStyle";
import { formatBearing, formatLatitude, formatLongitude } from "../utils/coordinates";
import { formatDistance } from "../utils/format";
import { visibleEventTypes } from "../utils/layers";
import type { EventsShown, FlyTarget, Layer, Selection, TimeWindow } from "../types";
import { visibilityFor } from "../utils/layerVisibility";

// ---------------------------------------------------------------------------
// The central map / globe workspace.
//
// Responsibilities:
//  1. create the MapLibre map once (local style, no internet)
//  2. keep the map's layers in step with the layer registry (on/off)
//  3. load data for visible layers from the local API, only for the current
//     viewport and time window (events) - never everything at once
//  4. report clicks (selection) and the view to the page
//  5. draw the selection highlight and the measuring line
//
// Props:
//  - layers:        registry layers with their `enabled` flag
//  - timeWindow:    events outside this window are not loaded
//  - highlight:     geometry of the current selection (drawn in blue)
//  - flyTo:         camera request from the page (search result, list click)
//  - refreshKey:    bump to reload change polygons (e.g. after a decision)
//  - onSelect:      called with what the analyst clicked
//  - onViewChange:  called with the viewport bbox after the map moves
//  - upcomingDays:  also show scheduled future events (launches) up to this
//                   many days ahead (0 = off); drawn hollow and labelled SCHEDULED
//  - onEventsShown: observed / scheduled event points currently loaded (status bar)
// ---------------------------------------------------------------------------
type MapWorkspaceProps = {
  layers: Layer[];
  sceneChoice: Record<string, string>; // AOI id -> scene id chosen for the imagery layer
  padding: { left: number; right: number; bottom: number }; // map area hidden by floating panels
  timeWindow: TimeWindow;
  highlight: GeoJSON.Geometry | null;
  flyTo: FlyTarget | null;
  refreshKey: number;
  onSelect: (selection: Selection) => void;
  onViewChange: (bbox: number[]) => void;
  upcomingDays: number;
  onEventsShown: (shown: EventsShown) => void;
  mlChangeRunId?: string | null; // which model run's candidates to draw (null: the latest run)
  discoveryCluster?: { clusterId: string; referenceChipId: string | null } | null; // discovery cluster to highlight
};

type AoiInfo = { id: string; name: string; label: string; footprint: GeoJSON.Polygon; latest_scene: string | null; image_corners: number[][] };

// "18 min" / "3.4 h" for tooltips.
function ageText(hours: number): string {
  return hours < 1 ? `${Math.max(1, Math.round(hours * 60))} min` : `${hours.toFixed(1)} h`;
}

// Tooltip text for a map feature, with the honesty labels spelled out.
function tooltipText(properties: Record<string, unknown>, layerId: string): string {
  if (properties.point_count) {
    const what = layerId.startsWith("entities") ? "infrastructure / places" : "events";
    return `${properties.point_count} ${what} — click to zoom`;
  }
  const title = String(properties.title ?? properties.change_class ?? "");
  if (properties.scheduled) {
    const precision = properties.time_precision ? ` (date known to the ${String(properties.time_precision).toLowerCase()})` : "";
    return `SCHEDULED · ${title} · planned ${String(properties.time ?? "").slice(0, 10)}${precision}`;
  }
  if (properties.stale && typeof properties.age_hours === "number") {
    return `${title} — STALE: position from ${ageText(properties.age_hours)} ago`;
  }
  return title;
}

type Readout = { lon: number | null; lat: number | null; zoom: number; bearing: number };

// Respect the operating system's "reduce motion" setting for camera moves.
function motionDuration(normalMs: number): number {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : normalMs;
}

// Viewport as [min_lon, min_lat, max_lon, max_lat], rounded to keep URLs short.
function viewBbox(map: maplibregl.Map): number[] {
  const bounds = map.getBounds();
  return [bounds.getWest(), bounds.getSouth(), bounds.getEast(), bounds.getNorth()].map((v) => Math.round(v * 1000) / 1000);
}

// Distance in metres along a list of [lon, lat] points (great-circle).
function pathLength(points: number[][]): number {
  let total = 0;
  for (let i = 1; i < points.length; i++) {
    total += new maplibregl.LngLat(points[i - 1][0], points[i - 1][1]).distanceTo(new maplibregl.LngLat(points[i][0], points[i][1]));
  }
  return total;
}

// The map layers currently visible among those that react to clicks.
function visibleClickable(map: maplibregl.Map): string[] {
  return CLICKABLE_LAYERS.filter((id) => map.getLayer(id) && map.getLayoutProperty(id, "visibility") !== "none");
}

export default function MapWorkspace({
  layers, sceneChoice, padding, timeWindow, highlight, flyTo, refreshKey, onSelect, onViewChange, upcomingDays, onEventsShown, mlChangeRunId = null, discoveryCluster = null,
}: MapWorkspaceProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  // Boundary/reference files already loaded into the map (load each once).
  const loadedFiles = useRef<Set<string>>(new Set());
  // Which scene each AOI's imagery layer currently shows, so the layer is
  // only touched when the scene really changes (see 3d below).
  const shownScenes = useRef<Record<string, string>>({});

  const [ready, setReady] = useState(false);
  const [projection, setProjection] = useState<"mercator" | "globe">("mercator");
  const [readout, setReadout] = useState<Readout>({ lon: null, lat: null, zoom: INDIA_VIEW_ZOOM, bearing: 0 });
  const [bbox, setBbox] = useState<number[] | null>(null);
  const [tooltip, setTooltip] = useState<{ x: number; y: number; text: string } | null>(null);
  const [measuring, setMeasuring] = useState(false);
  const [measurePoints, setMeasurePoints] = useState<number[][]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  // AOIs (footprints, image corners, latest scene), loaded once per refresh.
  const [aois, setAois] = useState<AoiInfo[]>([]);

  // The map's event handlers are registered once but must see the latest
  // props/state, so they read these refs (updated on every render).
  const measuringRef = useRef(measuring);
  measuringRef.current = measuring;
  const onSelectRef = useRef(onSelect);
  onSelectRef.current = onSelect;
  const onViewChangeRef = useRef(onViewChange);
  onViewChangeRef.current = onViewChange;

  // ----- 1. Create the map once ---------------------------------------------
  useEffect(() => {
    if (!containerRef.current) return;
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: buildLocalMapStyle(),
      center: INDIA_VIEW_CENTER,
      zoom: INDIA_VIEW_ZOOM,
      minZoom: 1,
      maxZoom: 16,
      attributionControl: false, // sources are credited in the Data & provenance panel
    });
    mapRef.current = map;
    map.addControl(new maplibregl.ScaleControl({ maxWidth: 90, unit: "metric" }), "bottom-right");

    map.on("load", () => {
      addOverlayLayers(map);
      setReady(true);
      const box = viewBbox(map);
      setBbox(box);
      onViewChangeRef.current(box);
    });

    map.on("mousemove", (event) => {
      setReadout((previous) => ({ ...previous, lon: event.lngLat.lng, lat: event.lngLat.lat }));
      // Hover tooltip: the title of the feature under the mouse, if any.
      const feature = map.queryRenderedFeatures(event.point, { layers: visibleClickable(map) })[0];
      map.getCanvas().style.cursor = feature ? "pointer" : measuringRef.current ? "crosshair" : "";
      if (!feature) {
        setTooltip(null);
        return;
      }
      setTooltip({ x: event.point.x, y: event.point.y, text: tooltipText(feature.properties ?? {}, feature.layer.id) });
    });
    map.on("mouseout", () => {
      setReadout((previous) => ({ ...previous, lon: null, lat: null }));
      setTooltip(null);
    });
    map.on("move", () => setReadout((previous) => ({ ...previous, zoom: map.getZoom(), bearing: map.getBearing() })));
    map.on("moveend", () => {
      const box = viewBbox(map);
      setBbox(box);
      onViewChangeRef.current(box);
    });

    map.on("click", async (event) => {
      if (measuringRef.current) {
        setMeasurePoints((points) => [...points, [event.lngLat.lng, event.lngLat.lat]]);
        return;
      }
      const feature = map.queryRenderedFeatures(event.point, { layers: visibleClickable(map) })[0];
      if (!feature) {
        // Empty map: report the location (the page decides whether it is an
        // imagery tile inside an AOI or just a place for "this location").
        onSelectRef.current({ kind: "location", lon: event.lngLat.lng, lat: event.lngLat.lat });
        return;
      }
      const properties = feature.properties ?? {};
      const layerId = feature.layer.id;
      if (properties.point_count) {
        // A cluster: zoom in until it splits into separate points.
        const source = map.getSource(layerId.startsWith("events") ? "events" : "entities") as maplibregl.GeoJSONSource;
        const zoom = await source.getClusterExpansionZoom(properties.cluster_id);
        const [lon, lat] = (feature.geometry as GeoJSON.Point).coordinates;
        map.easeTo({ center: [lon, lat], zoom, duration: motionDuration(400) });
        return;
      }
      if (layerId === "events-points") onSelectRef.current({ kind: "event", id: properties.id });
      else if (layerId === "entities-points") onSelectRef.current({ kind: "entity", id: properties.id });
      else if (layerId === "mlchange-fill") onSelectRef.current({ kind: "ml-change", id: properties.id });
      else if (layerId === "discovery-fill") onSelectRef.current({ kind: "chip", id: properties.id });
      else onSelectRef.current({ kind: "change", id: properties.id });
    });

    // Keep the map sized to its box when panels change size.
    const resizeObserver = new ResizeObserver(() => map.resize());
    resizeObserver.observe(containerRef.current);
    return () => {
      resizeObserver.disconnect();
      map.remove();
      mapRef.current = null;
      loadedFiles.current = new Set();
      shownScenes.current = {};
      setReady(false);
    };
  }, []);

  // ----- 2. Show/hide layers and load their files ---------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const layerColour = colorByLayer(layers);
    map.setPaintProperty("events-points", "circle-color", layerColour);
    map.setPaintProperty("entities-points", "circle-color", layerColour);
    // Scheduled (future) events are drawn as hollow rings in their layer
    // colour; stale telemetry gets an amber ring (and is faded, see mapStyle).
    map.setPaintProperty("events-points", "circle-stroke-color",
      ["case", ["==", ["get", "scheduled"], true], layerColour, ["==", ["get", "stale"], true], "#d2a24c", "#080b10"]);

    for (const layer of layers) {
      const visible = visibilityFor(layer) === "visible";
      for (const mapLayerId of mapLayerIdsFor(layer)) {
        if (map.getLayer(mapLayerId)) map.setLayoutProperty(mapLayerId, "visibility", visibilityFor(layer));
      }
      // Boundary and reference layers come from one GeoJSON file each.
      const url = dataUrlFor(layer);
      if (visible && url && !loadedFiles.current.has(url)) {
        loadedFiles.current.add(url);
        getJson<GeoJSON.FeatureCollection>(url)
          .then((data) => (map.getSource(sourceIdFor(layer)) as maplibregl.GeoJSONSource | undefined)?.setData(data))
          .catch(() => {
            loadedFiles.current.delete(url);
            setLoadError(`${layer.name}: data not staged`);
          });
      }
    }
  }, [layers, ready]);

  // ----- 3a. Events for the current viewport and time window -----------------
  // Historical events come from the time window; scheduled future events
  // (launches) only from the separate UPCOMING window, flagged scheduled.
  const eventTypes = visibleEventTypes(layers);
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready || !bbox) return;
    const source = map.getSource("events") as maplibregl.GeoJSONSource;
    if (!eventTypes) {
      source.setData({ type: "FeatureCollection", features: [] });
      onEventsShown({ observed: 0, scheduled: 0 });
      return;
    }
    // On the globe the view can extend past +/-180; clamp it.
    const box = [Math.max(-180, bbox[0]), Math.max(-90, bbox[1]), Math.min(180, bbox[2]), Math.min(90, bbox[3])];
    let cancelled = false;
    getJson<GeoJSON.FeatureCollection & { upcoming_total: number }>(`/api/events${query({
      bbox: box.join(","), start: timeWindow.start, end: timeWindow.end, types: eventTypes, upcoming_days: upcomingDays || null,
    })}`)
      .then((data) => {
        if (cancelled) return;
        source.setData(data);
        onEventsShown({ observed: data.features.length - data.upcoming_total, scheduled: data.upcoming_total });
        setLoadError(null);
      })
      .catch(() => !cancelled && setLoadError("Events unavailable: Lumon API unreachable"));
    return () => {
      cancelled = true;
    };
  }, [eventTypes, timeWindow, bbox, ready, upcomingDays, onEventsShown]);

  // ----- 3b. Entities (airports, ports, power plants) ------------------------
  const entityTypes = layers.filter((l) => l.kind === "entities" && l.enabled).flatMap((l) => l.types ?? []).join(",");
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const source = map.getSource("entities") as maplibregl.GeoJSONSource;
    if (!entityTypes) {
      source.setData({ type: "FeatureCollection", features: [] });
      return;
    }
    getJson<GeoJSON.FeatureCollection>(`/api/entities${query({ types: entityTypes })}`)
      .then((data) => source.setData(data))
      .catch(() => setLoadError("Entities unavailable: Lumon API unreachable"));
  }, [entityTypes, ready]);

  // ----- 3c. Satellite AOIs and change polygons -------------------------------
  const showChanges = layers.some((l) => l.id === "change-events" && l.enabled);
  const showSuppressed = layers.some((l) => l.id === "suppressed-changes" && l.enabled);
  const showMlChanges = layers.some((l) => l.id === "ml-change-candidates" && l.enabled);
  const showDiscovery = layers.some((l) => l.id === "discovery-cluster" && l.enabled);
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    getJson<AoiInfo[]>("/api/aois")
      .then((list) => {
        setAois(list); // also used by the imagery overlay (3d), so it is fetched once
        (map.getSource("aois") as maplibregl.GeoJSONSource).setData({
          type: "FeatureCollection",
          features: list.map((aoi) => ({ type: "Feature", geometry: aoi.footprint, properties: { id: aoi.id, name: aoi.name, label: aoi.label } })),
        });
      })
      .catch(() => undefined);
    if (showChanges) {
      getJson<GeoJSON.FeatureCollection>("/api/changes?status=accepted")
        .then((data) => (map.getSource("changes") as maplibregl.GeoJSONSource).setData(data))
        .catch(() => undefined);
    }
    if (showSuppressed) {
      getJson<GeoJSON.FeatureCollection>("/api/changes?status=suppressed")
        .then((data) => (map.getSource("suppressed") as maplibregl.GeoJSONSource).setData(data))
        .catch(() => undefined);
    }
    if (showMlChanges) {
      // Model output: separate source, endpoint and colour from the rule-based
      // events. One run (one date pair) at a time: the chosen one, or the latest.
      getJson<GeoJSON.FeatureCollection>(`/api/ml-change/regions${query({ run_id: mlChangeRunId })}`)
        .then((data) => (map.getSource("mlchange") as maplibregl.GeoJSONSource).setData(data))
        .catch(() => undefined);
    }
    // Discovery: the member places of the chosen cluster (one footprint per place).
    const discoverySource = map.getSource("discovery") as maplibregl.GeoJSONSource | undefined;
    if (showDiscovery && discoveryCluster) {
      getJson<GeoJSON.FeatureCollection>(`/api/discovery/clusters/${encodeURIComponent(discoveryCluster.clusterId)}/places.geojson${query({ reference: discoveryCluster.referenceChipId })}`)
        .then((data) => discoverySource?.setData(data))
        .catch(() => undefined);
    } else {
      discoverySource?.setData({ type: "FeatureCollection", features: [] });
    }
  }, [ready, refreshKey, showChanges, showSuppressed, showMlChanges, mlChangeRunId, showDiscovery, discoveryCluster]);

  // ----- 3d. Satellite imagery overlay (one true-colour image per AOI) ---------
  // The image comes from the local archive (/api/scenes/<id>/quicklook.png)
  // and is pinned to the AOI's exact corners. By default the latest usable
  // scene is shown; the Imagery drawer or the timeline playhead can choose
  // another date.
  //
  // To keep playback smooth, the layer is only touched when the scene for an
  // AOI actually changes: same scene -> nothing happens; a different scene
  // -> the existing image source is updated in place (updateImage); the
  // layer is only created the first time and removed when switched off.
  const showImagery = layers.some((l) => l.id === "sentinel-2" && l.enabled);
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    for (const aoi of aois) {
      const id = `imagery-${aoi.id}`;
      const wanted = showImagery ? sceneChoice[aoi.id] ?? aoi.latest_scene : null;
      if (wanted === (shownScenes.current[aoi.id] ?? null)) continue; // nothing changed
      const source = map.getSource(id) as maplibregl.ImageSource | undefined;
      if (!wanted) {
        if (map.getLayer(id)) map.removeLayer(id);
        if (source) map.removeSource(id);
        delete shownScenes.current[aoi.id];
        continue;
      }
      const url = `/api/scenes/${wanted}/quicklook.png`;
      const coordinates = aoi.image_corners as [[number, number], [number, number], [number, number], [number, number]];
      if (source) {
        source.updateImage({ url, coordinates });
      } else {
        map.addSource(id, { type: "image", url, coordinates });
        map.addLayer({ id, type: "raster", source: id, paint: { "raster-opacity": 0.92, "raster-fade-duration": 200 } }, "imagery-anchor");
      }
      shownScenes.current[aoi.id] = wanted;
    }
  }, [ready, aois, showImagery, sceneChoice]);

  // ----- 3e. Keep the camera centred on the VISIBLE part of the map -----------
  // Floating panels cover the map's edges. Telling MapLibre how much is
  // covered makes "centre", fly-to and fit-bounds use the visible area.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    map.easeTo({ padding: { top: 40, left: padding.left, right: padding.right, bottom: padding.bottom }, duration: motionDuration(260) });
  }, [padding.left, padding.right, padding.bottom, ready]);

  // ----- 4. Selection highlight -------------------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    (map.getSource("selection") as maplibregl.GeoJSONSource).setData({
      type: "FeatureCollection",
      features: highlight ? [{ type: "Feature", geometry: highlight, properties: {} }] : [],
    });
  }, [highlight, ready]);

  // ----- 5. Camera requests from the page --------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready || !flyTo) return;
    if (flyTo.bbox) {
      map.fitBounds([[flyTo.bbox[0], flyTo.bbox[1]], [flyTo.bbox[2], flyTo.bbox[3]]], { padding: 80, duration: motionDuration(900), maxZoom: 13 });
    } else {
      map.flyTo({ center: [flyTo.lon, flyTo.lat], zoom: flyTo.zoom ?? Math.max(map.getZoom(), 9), duration: motionDuration(900) });
    }
  }, [flyTo, ready]);

  // ----- 6. Measuring line ------------------------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const features: GeoJSON.Feature[] = measurePoints.map((p) => ({ type: "Feature", geometry: { type: "Point", coordinates: p }, properties: {} }));
    if (measurePoints.length > 1) features.push({ type: "Feature", geometry: { type: "LineString", coordinates: measurePoints }, properties: {} });
    (map.getSource("measure") as maplibregl.GeoJSONSource).setData({ type: "FeatureCollection", features });
  }, [measurePoints, ready]);

  // ----- Control rail actions ----------------------------------------------------
  function toggleProjection() {
    const map = mapRef.current;
    if (!map) return;
    const next = projection === "mercator" ? "globe" : "mercator";
    setProjection(next);
    map.setProjection({ type: next });
    map.easeTo({ center: INDIA_VIEW_CENTER, zoom: next === "globe" ? GLOBE_VIEW_ZOOM : INDIA_VIEW_ZOOM, duration: motionDuration(700) });
  }

  function toggleGrid(visible: boolean) {
    const map = mapRef.current;
    if (!map) return;
    for (const id of GRID_LAYER_IDS) map.setLayoutProperty(id, "visibility", visible ? "visible" : "none");
  }

  return (
    <main className="workspace" aria-label="Map workspace">
      <div ref={containerRef} className="workspace__map" />

      <MapControls
        projection={projection}
        bearing={readout.bearing}
        measuring={measuring}
        onZoomIn={() => mapRef.current?.zoomIn({ duration: motionDuration(250) })}
        onZoomOut={() => mapRef.current?.zoomOut({ duration: motionDuration(250) })}
        onResetNorth={() => mapRef.current?.easeTo({ bearing: 0, pitch: 0, duration: motionDuration(400) })}
        onResetView={() => mapRef.current?.easeTo({ center: INDIA_VIEW_CENTER, zoom: projection === "globe" ? GLOBE_VIEW_ZOOM : INDIA_VIEW_ZOOM, bearing: 0, pitch: 0, duration: motionDuration(700) })}
        onToggleProjection={toggleProjection}
        onToggleMeasure={() => {
          setMeasuring(!measuring);
          setMeasurePoints([]);
        }}
        onToggleGrid={toggleGrid}
        onGoTo={(lon, lat) => mapRef.current?.flyTo({ center: [lon, lat], zoom: 10, duration: motionDuration(900) })}
      />

      {measuring && (
        <div className="map-measure" role="status">
          <span className="map-measure__label">MEASURE</span>
          <span>{measurePoints.length < 2 ? "Click points on the map" : formatDistance(pathLength(measurePoints))}</span>
          <button type="button" className="text-button" onClick={() => setMeasurePoints([])}>Clear</button>
        </div>
      )}

      {tooltip && tooltip.text && (
        <div className="map-tooltip" style={{ left: tooltip.x + 12, top: tooltip.y + 12 }}>{tooltip.text}</div>
      )}

      {loadError && <div className="map-error" role="status">{loadError}</div>}

      {/* Coordinate readout (bottom left, above the timeline). */}
      <div className="map-readout" aria-live="off">
        <span className="map-readout__label">LAT</span>
        <span className="map-readout__value">{readout.lat === null ? "—" : formatLatitude(readout.lat)}</span>
        <span className="map-readout__label">LON</span>
        <span className="map-readout__value">{readout.lon === null ? "—" : formatLongitude(readout.lon)}</span>
        <span className="map-readout__label">Z</span>
        <span className="map-readout__value">{readout.zoom.toFixed(1)}</span>
        <span className="map-readout__label">BRG</span>
        <span className="map-readout__value">{formatBearing(readout.bearing)}</span>
      </div>
    </main>
  );
}
