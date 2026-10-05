import { useCallback, useEffect, useMemo, useState } from "react";
import TopBar from "../components/TopBar";
import LeftNav from "../components/LeftNav";
import LeftDrawer from "../components/LeftDrawer";
import MapWorkspace from "../components/MapWorkspace";
import LayerPanel from "../components/LayerPanel";
import IntelligencePanel from "../components/intel/IntelligencePanel";
import Timeline from "../components/Timeline";
import StatusBar from "../components/StatusBar";
import { navigationGroups } from "../data/navigation";
import { getJson, postJson } from "../api";
import { windowForDays } from "../utils/time";
import { UPCOMING_DAYS, visibleEventTypes } from "../utils/layers";
import type { EventsShown, FlyTarget, Layer, QueryPlan, QueryResultItem, QueryRun, Selection, SystemInfo, TimeWindow } from "../types";
import { applyLayerChoices, toggleLayerChoice } from "../utils/layerVisibility";

// ---------------------------------------------------------------------------
// LUMON COMMAND - the analyst workstation.
//
// Layout: the map fills the screen under a thin top bar and status bar.
// Everything else floats over it so the map stays dominant:
//
//   [rail] [drawer]        MAP / GLOBE          [layers] [intelligence]
//                          [timeline]
//
// This component owns the shared state and passes it to the panels with
// ordinary props: what is selected, which layers are on, the time window,
// the current query plan and its results.
// ---------------------------------------------------------------------------

type Aoi = { id: string; bbox: number[] };
type SceneRow = { id: string; acquired_at: string; quality_status: string };

export default function CommandPage() {
  // ----- Backend status ------------------------------------------------------
  const [system, setSystem] = useState<SystemInfo | null>(null);
  const [apiOnline, setApiOnline] = useState(true);
  const [apiLatency, setApiLatency] = useState<number | null>(null);

  // ----- Layers ------------------------------------------------------------
  const [registry, setRegistry] = useState<{ groups: string[]; layers: Layer[] }>({ groups: [], layers: [] });
  // The analyst's own on/off choices, kept separate from the registry
  // defaults so they survive registry reloads.
  const [layerChoices, setLayerChoices] = useState<Record<string, boolean>>({});

  // ----- Panels ------------------------------------------------------------
  const [section, setSection] = useState<string | null>(null); // drawer closed: the map comes first
  const [layerPanelOpen, setLayerPanelOpen] = useState(false);
  const [intelOpen, setIntelOpen] = useState(true);
  // Navigation panel: expanded (labels) or collapsed (codes). Remembered in
  // this browser only; defaults to expanded on wide screens.
  const [navExpanded, setNavExpanded] = useState<boolean>(() => {
    try {
      const saved = window.localStorage.getItem("lumon.navExpanded");
      if (saved !== null) return saved === "1";
    } catch {
      // storage unavailable: fall through to the default
    }
    return window.innerWidth >= 1200;
  });
  function toggleNav() {
    setNavExpanded((expanded) => {
      try {
        window.localStorage.setItem("lumon.navExpanded", expanded ? "0" : "1");
      } catch {
        // not saved; the choice still applies for this session
      }
      return !expanded;
    });
  }

  // ----- Map state ---------------------------------------------------------
  const [selection, setSelection] = useState<Selection | null>(null);
  const [highlight, setHighlight] = useState<GeoJSON.Geometry | null>(null);
  const [flyTo, setFlyTo] = useState<FlyTarget | null>(null);
  const [bbox, setBbox] = useState<number[] | null>(null);
  const [eventsShown, setEventsShown] = useState<EventsShown>({ observed: 0, scheduled: 0 });
  // Show scheduled future events (launches) in a separate UPCOMING window.
  const [showUpcoming, setShowUpcoming] = useState(true);
  const [refreshKey, setRefreshKey] = useState(0);
  const [aois, setAois] = useState<Aoi[]>([]);
  // Which staged scene the imagery layer shows per AOI (default: latest).
  const [sceneChoice, setSceneChoice] = useState<Record<string, string>>({});
  // Usable scenes per AOI, so the timeline playhead can pick the image to show.
  const [aoiScenes, setAoiScenes] = useState<Record<string, SceneRow[]>>({});
  // Which learned-change run the ML candidates layer draws (null: the latest).
  const [mlChangeRunId, setMlChangeRunId] = useState<string | null>(null);
  // Which discovery cluster the map highlights (and the reference chip it is ranked against).
  const [discoveryCluster, setDiscoveryCluster] = useState<{ clusterId: string; referenceChipId: string | null } | null>(null);

  // ----- Time --------------------------------------------------------------
  const [baseWindow, setBaseWindow] = useState<TimeWindow>(() => windowForDays(30, "30D"));
  const [cursor, setCursor] = useState<string | null>(null);

  // ----- Query -------------------------------------------------------------
  const [plan, setPlan] = useState<QueryPlan | null>(null);
  const [run, setRun] = useState<QueryRun | null>(null);
  const [running, setRunning] = useState(false);

  // Poll the backend: a cheap health check every 15 s (also measures the
  // round-trip latency shown in the status bar) and the system report.
  const loadSystem = useCallback(() => {
    const started = performance.now();
    getJson("/api/health")
      .then(() => {
        setApiOnline(true);
        setApiLatency(Math.round(performance.now() - started));
        return getJson<SystemInfo>("/api/system");
      })
      .then(setSystem)
      .catch(() => {
        setApiOnline(false);
        setApiLatency(null);
      });
  }, []);

  useEffect(() => {
    loadSystem();
    const timer = window.setInterval(loadSystem, 15000);
    return () => window.clearInterval(timer);
  }, [loadSystem]);

  // The layer registry and the AOI list (reloaded after decisions/refreshes).
  useEffect(() => {
    getJson<{ groups: string[]; layers: Layer[] }>("/api/layers").then(setRegistry).catch(() => undefined);
    getJson<Aoi[]>("/api/aois")
      .then((list) => {
        setAois(list);
        for (const aoi of list) {
          getJson<SceneRow[]>(`/api/aois/${aoi.id}/scenes`)
            .then((rows) => setAoiScenes((all) => ({ ...all, [aoi.id]: rows.filter((r) => r.quality_status === "usable" || r.quality_status === "degraded") })))
            .catch(() => undefined);
        }
      })
      .catch(() => undefined);
  }, [refreshKey, apiOnline]);

  // Timeline -> imagery: when the playhead moves, show for each AOI the
  // latest usable scene acquired on or before that day. Clearing the
  // playhead goes back to the latest scene.
  useEffect(() => {
    if (!cursor) {
      setSceneChoice((current) => (Object.keys(current).length ? {} : current));
      return;
    }
    const choice: Record<string, string> = {};
    for (const [aoiId, rows] of Object.entries(aoiScenes)) {
      const before = rows.filter((r) => r.acquired_at.slice(0, 10) <= cursor);
      if (before.length) choice[aoiId] = before[before.length - 1].id;
    }
    // Only replace the state when a scene actually changed, so playback
    // steps that stay on the same image do not trigger any map work.
    setSceneChoice((current) => {
      const same = Object.keys(choice).length === Object.keys(current).length
        && Object.entries(choice).every(([aoiId, sceneId]) => current[aoiId] === sceneId);
      return same ? current : choice;
    });
  }, [cursor, aoiScenes]);

  // Registry layers with the analyst's choices applied.
  const layers = useMemo(
    () => applyLayerChoices(registry.layers, layerChoices),
    [registry, layerChoices],
  );

  // The time window the map and lists use: the playhead (if any) cuts it short.
  const timeWindow = useMemo<TimeWindow>(
    () => (cursor ? { ...baseWindow, end: `${cursor}T23:59:59Z` } : baseWindow),
    [baseWindow, cursor],
  );

  // ----- Actions ---------------------------------------------------------------

  const toggleLayer = useCallback((id: string) => {
    setLayerChoices((choices) => toggleLayerChoice(choices, registry.layers, id));
  }, [registry]);

  const setLayer = useCallback((id: string, enabled: boolean) => {
    setLayerChoices((choices) => ({ ...choices, [id]: enabled }));
  }, []);

  // Anything selected on the map or in a list. A click on empty map inside
  // a staged AOI becomes an imagery-tile selection; elsewhere a location.
  const select = useCallback((next: Selection) => {
    if (next.kind === "location") {
      const point = { lon: next.lon, lat: next.lat };
      const inAoi = aois.some((a) => point.lon >= a.bbox[0] && point.lon <= a.bbox[2] && point.lat >= a.bbox[1] && point.lat <= a.bbox[3]);
      next = inAoi ? { kind: "tile", ...point } : next;
      setHighlight({ type: "Point", coordinates: [point.lon, point.lat] });
    }
    setSelection(next);
    setIntelOpen(true);
  }, [aois]);

  const clearSelection = useCallback(() => {
    setSelection(null);
    setHighlight(null);
  }, []);

  const fly = useCallback((target: Omit<FlyTarget, "key">) => setFlyTo({ ...target, key: Date.now() }), []);

  // A place chosen in the command search: select it and fly there.
  const choosePlace = useCallback((place: { name: string; kind: string; lon: number; lat: number; bbox?: number[] | null }) => {
    setSelection({ kind: "place", name: place.name, lon: place.lon, lat: place.lat, placeKind: place.kind, bbox: place.bbox });
    setHighlight({ type: "Point", coordinates: [place.lon, place.lat] });
    setIntelOpen(true);
    fly({ lon: place.lon, lat: place.lat, zoom: place.kind === "city" ? 9 : undefined, bbox: place.bbox ?? null });
  }, [fly]);

  // A parsed plan arrives from the command bar: show it in the Search drawer.
  const receivePlan = useCallback((next: QueryPlan) => {
    setPlan(next);
    setRun(null);
    setSection("search");
  }, []);

  // Run the (possibly edited) plan. "This location" uses the current selection.
  const runPlan = useCallback(async () => {
    if (!plan) return;
    let toRun = plan;
    if (plan.place?.kind === "map-selection" && selection && "lon" in selection) {
      toRun = { ...plan, place: { ...plan.place, lon: selection.lon, lat: selection.lat } };
      setPlan(toRun);
    }
    setRunning(true);
    try {
      const result = await postJson<QueryRun>("/api/query/run", { plan: toRun });
      setRun(result);
      // Make the matching layers visible so results appear on the map.
      const types = new Set(toRun.target?.types ?? []);
      for (const layer of registry.layers) {
        if ((layer.types ?? []).some((t) => types.has(t))) setLayer(layer.id, true);
      }
      if (toRun.intent === "find-changes") setLayer("change-events", true);
      if (toRun.intent === "search-imagery") setLayer("sentinel-2", true);
      if (toRun.time?.start) setBaseWindow({ start: toRun.time.start, end: toRun.time.end ?? new Date().toISOString().slice(0, 19) + "Z", label: "QUERY" });
      // Frame the results on the map.
      const points = result.results.filter((r) => r.lon !== null && r.lat !== null);
      if (points.length === 1) fly({ lon: points[0].lon!, lat: points[0].lat!, zoom: 11 });
      if (points.length > 1) {
        const lons = points.map((p) => p.lon!);
        const lats = points.map((p) => p.lat!);
        fly({ lon: lons[0], lat: lats[0], bbox: [Math.min(...lons), Math.min(...lats), Math.max(...lons), Math.max(...lats)] });
      }
    } catch {
      setRun(null);
      setApiOnline(false);
    } finally {
      setRunning(false);
    }
  }, [plan, selection, registry, setLayer, fly]);

  // Open one query result: select it and move the map to it.
  const openResult = useCallback((item: QueryResultItem) => {
    if (item.kind === "chip") {
      // Semantic image-search chip: detail panel, footprint outline (the
      // map flies to it), and its scene shown as the imagery layer.
      setSelection({ kind: "chip", id: item.id, score: item.score, rank: item.rank, query: item.query, scoreKind: item.score_kind });
      setIntelOpen(true);
      if (item.footprint) setHighlight({ type: "MultiPolygon", coordinates: [item.footprint.coordinates] });
      if (item.aoi_id && item.scene_id) {
        setSceneChoice((choice) => ({ ...choice, [item.aoi_id!]: item.scene_id! }));
        setLayer("sentinel-2", true);
      }
      return;
    }
    if (item.kind === "event") select({ kind: "event", id: item.id });
    else if (item.kind === "entity") select({ kind: "entity", id: item.id });
    else if (item.kind === "change") select({ kind: "change", id: item.id });
    else if (item.lon !== null && item.lat !== null) choosePlace({ name: item.title, kind: item.type, lon: item.lon, lat: item.lat, bbox: item.bbox });
    if (item.lon !== null && item.lat !== null && item.kind !== "place") fly({ lon: item.lon, lat: item.lat, zoom: item.kind === "change" ? 13 : 8 });
  }, [select, choosePlace, fly, setLayer]);

  // Show one dated image of an AOI as the Sentinel-2 layer.
  const showScene = useCallback((aoiId: string, sceneId: string) => {
    setSceneChoice((choice) => ({ ...choice, [aoiId]: sceneId }));
    setLayer("sentinel-2", true);
  }, [setLayer]);

  // Discovery: highlight one cluster's member places on the map.
  const showCluster = useCallback((clusterId: string, referenceChipId: string | null, box: number[]) => {
    setDiscoveryCluster({ clusterId, referenceChipId });
    setLayer("discovery-cluster", true);
    fly({ lon: box[0], lat: box[1], bbox: box });
  }, [setLayer, fly]);

  // Open an image chip by id (e.g. a cluster representative).
  const openChipId = useCallback((chipId: string) => {
    setSelection({ kind: "chip", id: chipId });
    setIntelOpen(true);
  }, []);

  // OSINT -> satellite: open an AOI's change context from an event.
  const openAoi = useCallback((aoiId: string) => {
    const aoi = aois.find((a) => a.id === aoiId);
    if (aoi) fly({ lon: aoi.bbox[0], lat: aoi.bbox[1], bbox: aoi.bbox });
    setLayer("change-events", true);
    setLayer("aoi-footprints", true);
    setSection("changes");
  }, [aois, fly, setLayer]);

  // After a decision, ingest or analysis: reload what may have changed.
  const refreshAll = useCallback(() => {
    setRefreshKey((k) => k + 1);
    loadSystem();
  }, [loadSystem]);

  // Fly to the selected change/event when it is chosen from a list.
  useEffect(() => {
    if (!highlight || !selection || selection.kind === "location" || selection.kind === "place" || selection.kind === "tile") return;
    if (highlight.type === "Point") return; // points are flown to by the list action
    const coordinates: number[][] = (highlight as GeoJSON.MultiPolygon).coordinates?.flat(2) ?? [];
    if (!coordinates.length) return;
    const lons = coordinates.map((c) => c[0]);
    const lats = coordinates.map((c) => c[1]);
    fly({ lon: lons[0], lat: lats[0], bbox: [Math.min(...lons) - 0.01, Math.min(...lats) - 0.01, Math.max(...lons) + 0.01, Math.max(...lats) + 0.01] });
  }, [highlight, selection, fly]);

  // Keyboard shortcuts (ignored while typing in a field):
  //   Esc  clear the selection, or close the open drawer
  //   L    show/hide the layer panel
  //   /    focus the command search (handled in CommandSearch)
  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      const target = event.target as HTMLElement;
      if (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT") return;
      if (event.key === "Escape") {
        if (selection) clearSelection();
        else setSection(null);
      } else if (event.key.toLowerCase() === "l" && !event.metaKey && !event.ctrlKey) {
        setLayerPanelOpen((open) => !open);
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [selection, clearSelection]);

  const activeLayerCount = layers.filter((l) => l.enabled).length;

  return (
    <div className={["app", intelOpen ? "" : "app--intel-collapsed", navExpanded ? "app--nav-expanded" : ""].join(" ").trim()}>
      <TopBar system={system} apiOnline={apiOnline} onPlace={choosePlace} onPlan={receivePlan} />

      <div className="stage">
        <MapWorkspace
          layers={layers}
          sceneChoice={sceneChoice}
          padding={{ left: section ? 340 : 0, right: intelOpen ? 370 : 30, bottom: 90 }}
          timeWindow={timeWindow}
          highlight={highlight}
          flyTo={flyTo}
          refreshKey={refreshKey}
          onSelect={select}
          onViewChange={setBbox}
          upcomingDays={showUpcoming ? UPCOMING_DAYS : 0}
          onEventsShown={setEventsShown}
          mlChangeRunId={mlChangeRunId}
          discoveryCluster={discoveryCluster}
        />

        <LeftNav
          groups={navigationGroups}
          activeId={section}
          expanded={navExpanded}
          onSelect={(id) => setSection(section === id ? null : id)}
          onToggleExpanded={toggleNav}
        />

        {section && (
          <LeftDrawer
            section={section}
            onClose={() => setSection(null)}
            system={system}
            layers={layers}
            timeWindow={timeWindow}
            bbox={bbox}
            refreshKey={refreshKey}
            selection={selection}
            plan={plan}
            run={run}
            running={running}
            onPlanChange={setPlan}
            onRun={runPlan}
            onOpenResult={openResult}
            onSelect={select}
            onToggleLayer={toggleLayer}
            onSetLayer={setLayer}
            onFlyToBbox={(box) => fly({ lon: box[0], lat: box[1], bbox: box })}
            sceneChoice={sceneChoice}
            onOpenSection={setSection}
            onShowScene={showScene}
            onShowMlRun={setMlChangeRunId}
            onShowCluster={showCluster}
            onOpenChipId={openChipId}
            onRefresh={refreshAll}
          />
        )}

        <button
          type="button"
          className={layerPanelOpen ? "layers-button layers-button--active" : "layers-button"}
          onClick={() => setLayerPanelOpen(!layerPanelOpen)}
          aria-expanded={layerPanelOpen}
        >
          LAYERS <span className="layers-button__badge">{activeLayerCount}</span>
        </button>
        {layerPanelOpen && (
          <LayerPanel groups={registry.groups} layers={layers} onToggle={toggleLayer} onClose={() => setLayerPanelOpen(false)} />
        )}

        <IntelligencePanel
          selection={selection}
          system={system}
          open={intelOpen}
          onToggleOpen={() => setIntelOpen(!intelOpen)}
          onClear={clearSelection}
          onSelect={select}
          onHighlight={setHighlight}
          onDecision={refreshAll}
          onOpenAoi={openAoi}
          onOpenResult={openResult}
          onShowScene={showScene}
          onShowCluster={showCluster}
        />

        <Timeline
          window={baseWindow}
          cursor={cursor}
          refreshKey={refreshKey}
          types={visibleEventTypes(layers)}
          bbox={bbox}
          upcomingDays={showUpcoming ? UPCOMING_DAYS : 0}
          onToggleUpcoming={() => setShowUpcoming(!showUpcoming)}
          onPreset={(days, label) => setBaseWindow(windowForDays(days, label))}
          onCursor={setCursor}
        />

        {!apiOnline && (
          <div className="offline-banner" role="alert">
            LUMON API UNREACHABLE — map shows local grid only. Start the backend: <code>npm run api</code>
          </div>
        )}
      </div>

      <StatusBar system={system} apiOnline={apiOnline} apiLatencyMs={apiLatency} eventsShown={eventsShown} />
    </div>
  );
}
