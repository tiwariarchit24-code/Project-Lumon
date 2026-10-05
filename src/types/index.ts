// ---------------------------------------------------------------------------
// Shared data types for LUMON COMMAND.
//
// These mirror the JSON returned by the local Lumon API (server/lumon/routes).
// They are deliberately plain: most fields come straight from the database.
// ---------------------------------------------------------------------------

// ----- Navigation ----------------------------------------------------------

// One entry in the left navigation rail. Each entry opens a drawer section.
export type NavItem = {
  id: string; // section id, e.g. "sources"
  label: string; // text shown in the drawer header and tooltip
  code: string; // two-letter code shown in the rail
};

export type NavGroup = {
  title: string; // e.g. "OSINT"
  items: NavItem[];
};

// ----- Map layers (from /api/layers) ----------------------------------------

export type LayerKind = "boundary" | "reference" | "events" | "entities" | "imagery" | "analysis" | "pilot" | "planned";

export type Layer = {
  id: string;
  name: string;
  group: string;
  kind: LayerKind;
  description: string;
  dataset?: string; // boundary dataset id (kind "boundary")
  source_id?: string; // reference layers: the source that builds the file
  pilot?: string; // pilot layers: the pilot id (e.g. "nit-raipur")
  source: string | null; // who provides the data (provider name)
  types?: string[]; // event/entity types shown by this layer
  nonspatial?: boolean; // shown in the timeline only (no location)
  color?: string;
  enabled: boolean; // shown on the map right now
  available: boolean; // data is staged and can be shown
  updated_at: string | null;
  coverage: string | null;
  offline_available: boolean;
  count: number | null;
  status_text: string; // e.g. "SNAPSHOT · 3.4 h old", "AVAILABLE LATER"
  // LIVE | DELAYED | SNAPSHOT | STALE | EMPTY | UNAVAILABLE | NOT STAGED | KEY REQUIRED | NOT IMPLEMENTED (null for boundaries/imagery)
  freshness: string | null;
};

// ----- Selection ----------------------------------------------------------

// What the analyst has selected on the map or in a list. The right
// intelligence panel shows details for it.
export type Selection =
  | { kind: "event"; id: string }
  | { kind: "entity"; id: string }
  | { kind: "change"; id: string }
  | { kind: "tile"; lon: number; lat: number }
  | { kind: "place"; name: string; lon: number; lat: number; placeKind: string; bbox?: number[] | null }
  | { kind: "location"; lon: number; lat: number }
  // A semantic image-search result: score/rank/query belong to the search
  // that found it (model similarity, not a probability).
  | { kind: "chip"; id: string; score?: number; rank?: number; query?: string; scoreKind?: string }
  // A model-generated candidate change region (BTC-B); never a verified change.
  | { kind: "ml-change"; id: string };

// A request to move the map camera. `key` makes repeated requests distinct.
export type FlyTarget = { lon: number; lat: number; zoom?: number; bbox?: number[] | null; key: number };

// ----- Time ----------------------------------------------------------------

// The time window that filters events on the map and in lists (ISO strings).
export type TimeWindow = { start: string; end: string; label: string };

// ----- Query plan (from /api/query/parse) ------------------------------------

export type PlanChip = { matched?: string | null; assumed?: boolean; note?: string };

export type QueryPlan = {
  text: string;
  intent: string;
  target: { kind: string; label: string; types?: string[] } | null;
  change: ({ class: string; direction: string | null } & PlanChip) | null;
  place: ({ name: string; kind: string; lon?: number; lat?: number; bbox?: number[] | null } & PlanChip) | null;
  distance: ({ value_m: number } & PlanChip) | null;
  reference: ({ kind: string } & PlanChip) | null;
  time: ({ start: string; end: string | null } & PlanChip) | null;
  sensor: string | null;
  filters: { min_magnitude?: { value: number; matched: string } };
  related_events: { types: string[]; matched: string } | null;
  evidence_type: string | null;
  unrecognised: string[];
  notes: string[];
  include_suppressed?: boolean;
  // Scope: India, or the NIT Raipur pilot (study area / campus) with its geometry status.
  study_area: { id: string; name: string; level: string; status: string | null; matched: string | null } | null;
  // The AI/ML capability the request needs (config/ai/capabilities.json) and its status.
  operation: { id: string; name: string; status: string; fallback: string | null; matched?: string | null } | null;
  target_class: { class: string; matched: string } | null;
};

export type QueryResultItem = {
  kind: "event" | "entity" | "change" | "place" | "chip";
  id: string;
  title: string;
  type: string;
  time?: string;
  lon: number | null;
  lat: number | null;
  evidence_type: string;
  why: string[];
  score?: number;
  score_kind?: string;
  related_events?: { id: string; title: string; time: string; distance_m: number }[];
  bbox?: number[] | null;
  // Semantic image-search chips only (kind "chip"):
  rank?: number;
  query?: string;
  scene_id?: string;
  aoi_id?: string;
  acquired_at?: string;
  chip_km?: number;
  footprint?: GeoJSON.Polygon;
};

// ----- Semantic image search (/api/semantic/...) -------------------------------

// NOT STAGED | INDEXING | READY | PARTIAL | UNAVAILABLE
export type SemanticStatus = {
  state: string; reasons: string[]; model_name: string; architecture: string; framework: string; license: string | null;
  weights_sha256: string; preprocess_version: string; model_loaded: boolean;
  scenes_total: number; scenes_eligible: number; scenes_indexed: number; scenes_processed: number;
  scenes_excluded_by_quality: number; scenes_excluded_by_indexer: { scene_id: string; reason: string }[];
  chips_indexed: number; chips_excluded: number; chip_sizes_px: number[]; max_invalid_fraction: number; score_kind: string;
};

// What one search reports besides its results.
export type SemanticRunInfo = {
  query: string; model: Record<string, string>; score_kind: string; chips_searched: number; scenes_searched: number;
  score_distribution: { median: number; max: number; min: number } | null;
  timing_ms: { model_load: number; text_encode: number; rank: number; total: number }; note: string;
};

export type QueryRun = {
  plan: QueryPlan;
  // SUPPORTED: fully answerable; PARTIAL: runs, but a needed source/key is
  // missing; UNSUPPORTED: refused, no source can answer it.
  // NOT STAGED: understood and designed for, but the data/model it needs is
  // not staged, so nothing runs and no results are invented.
  capability: { state: "SUPPORTED" | "PARTIAL" | "NOT STAGED" | "UNSUPPORTED"; supported: boolean; issues: string[]; partial: string[]; warnings: string[] };
  area: { kind: string; name?: string };
  results: QueryResultItem[];
  count: number;
  semantic?: SemanticRunInfo; // image searches only
};

// ----- System (from /api/system) -------------------------------------------

export type SystemInfo = {
  mode: "connected" | "airgapped";
  boundary: { staged: boolean; missing: string[] };
  counts: Record<string, number>;
  sources: {
    total: number;
    by_health: Record<string, number>;
    by_status: Record<string, number>; // LIVE / SNAPSHOT / STALE / KEY REQUIRED ...
    newest_download_age_hours: number | null; // age of the most recent successful download
  };
  last_source_success: string | null;
  latest_scene: string | null;
  storage_bytes: Record<string, number>;
  models: { name: string; status: string; fallback: string; retrieval_status?: string }[];
  audit: { ok: boolean; entries: number; broken_at?: number; reason?: string };
};

// Events currently loaded on the map: observed/recorded ones in the time
// window, and scheduled future ones (UPCOMING window), counted separately.
export type EventsShown = { observed: number; scheduled: number };

// One label/value pair in the top bar or status bar.
export type StatusTone = "ok" | "neutral" | "pending" | "error";
export type SystemStatus = { id: string; label: string; value: string; tone: StatusTone };

// Evidence types used across the product: every claim says how it is known.
export type EvidenceType = "OBSERVED" | "INFERRED" | "GIS-DERIVED" | "VERIFIED RECORD" | "UNVERIFIABLE";
