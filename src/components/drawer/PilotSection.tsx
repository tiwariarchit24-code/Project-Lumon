import { useEffect, useState } from "react";
import { getJson } from "../../api";
import InfoTip from "../InfoTip";
import { INFO } from "../../data/infoNotes";
import { Facts, PanelState, Section } from "../intel/common";

// ---------------------------------------------------------------------------
// NIT RAIPUR · PILOT STUDY AREA
//
// The detailed demonstrator inside India-wide Lumon. Shows, from the
// backend registries (config/pilots, config/ai):
//   - the study area and the honest status of its geometry
//   - the pilot data registry, counted by availability state
//   - the AI/ML capabilities, counted by status, one row each with an ⓘ note
//   - the candidate models (none deployed)
// Nothing here is a result: every capability is NOT STAGED until a real
// model and real pilot data are staged.
// ---------------------------------------------------------------------------
type DataEntry = { name: string; category: string; expected_format: string; source: string; state: string; notes: string };
type Capability = {
  id: string; name: string; ui: string | null; purpose: string; status: string; requires_data: string[];
  model: string | null; component: string; output: string; fallback: string | null; limitations: string;
  // semantic-search only: state of its embedding index and what is indexed
  retrieval_status?: string;
  retrieval?: {
    reasons: string[]; model_name: string;
    scenes_indexed?: number; scenes_eligible?: number; chips_indexed?: number; // RemoteCLIP index
    runs?: number; regions?: Record<string, number>;                           // BTC-B change runs
    embeddings?: number; pending?: number; method?: string;                    // discovery clusters
  };
};
type Model = { id: string; name: string; task: string; source: string; deployment_status: string; compute_notes: string };
type PilotSummary = {
  short_name: string; canonical_name: string; description: string;
  study_areas: { id: string; name: string; level: string }[];
  study_area: { status: string; source: string; meaning: string; campus_boundary: boolean };
  data_registry: DataEntry[]; data_counts: Record<string, number>;
  capabilities: Capability[]; ai_counts: Record<string, number>; models: Model[]; registry_problems: string[];
};

type PilotSectionProps = { onFlyToBbox: (bbox: number[]) => void };

// Bounding box of a GeoJSON polygon feature collection (for "fly to").
function collectionBbox(collection: GeoJSON.FeatureCollection): number[] | null {
  const points = collection.features.flatMap((f) => (f.geometry as GeoJSON.Polygon).coordinates.flat());
  if (!points.length) return null;
  const lons = points.map((p) => p[0]);
  const lats = points.map((p) => p[1]);
  return [Math.min(...lons), Math.min(...lats), Math.max(...lons), Math.max(...lats)];
}

// "3 expected · 1 not available" from a {state: count} object, skipping zeros.
function countLine(counts: Record<string, number>, order: string[]): string {
  return order.filter((state) => counts[state]).map((state) => `${counts[state]} ${state.toLowerCase()}`).join(" · ") || "—";
}

export default function PilotSection({ onFlyToBbox }: PilotSectionProps) {
  const [pilot, setPilot] = useState<PilotSummary | null>(null);
  const [bbox, setBbox] = useState<number[] | null>(null);
  const [error, setError] = useState(false);
  const [showData, setShowData] = useState(false);

  useEffect(() => {
    getJson<PilotSummary>("/api/pilots/nit-raipur").then(setPilot).catch(() => setError(true));
    getJson<GeoJSON.FeatureCollection>("/api/pilots/nit-raipur/aoi.geojson").then((c) => setBbox(collectionBbox(c))).catch(() => setBbox(null));
  }, []);

  if (error) return <PanelState state="error" text="LUMON API UNREACHABLE" />;
  if (!pilot) return <PanelState state="loading" />;

  const campus = pilot.study_areas.find((s) => s.level === "campus");
  const rows = pilot.capabilities.filter((c) => c.ui);
  const models = Object.fromEntries(pilot.models.map((m) => [m.id, m]));

  return (
    <>
      <Section title="PILOT STUDY AREA" meta={pilot.short_name}
        info={{ ...INFO.pilot, state: `${pilot.study_area.status}; campus boundary ${pilot.study_area.campus_boundary ? "staged" : "not staged"}` }}>
        <Facts rows={[
          ["STUDY AREA", campus?.name ?? pilot.short_name],
          ["GEOMETRY", <span className="pilot-status">{pilot.study_area.status}{pilot.study_area.campus_boundary ? "" : " · search area, not campus boundary"}</span>],
          ["SOURCE", pilot.study_area.source],
          ["ROLE", "Detailed demonstrator; India remains the broad coverage area"],
        ]} />
        {bbox && <button type="button" className="action-button" onClick={() => onFlyToBbox(bbox)}>FLY TO STUDY AREA</button>}
        {pilot.registry_problems.length > 0 && pilot.registry_problems.map((p) => <div key={p} className="notice notice--error">{p}</div>)}
      </Section>

      <Section title="DATA" meta={countLine(pilot.data_counts, ["AVAILABLE", "PARTIAL", "EXPECTED", "NOT STAGED", "NOT AVAILABLE"])}
        info={{ ...INFO.pilotData, state: `${pilot.data_registry.length} datasets registered; none institutional staged yet` }}>
        <button type="button" className="text-button" onClick={() => setShowData(!showData)} aria-expanded={showData}>
          {showData ? "Hide" : "Show"} data registry
        </button>
        {showData && (
          <ul className="pilot-list fade-in">
            {pilot.data_registry.map((entry) => (
              <li key={entry.name} className="pilot-row" title={`${entry.expected_format}\nSource: ${entry.source}${entry.notes ? `\n${entry.notes}` : ""}`}>
                <span className="pilot-row__name">{entry.name}</span>
                <span className="pilot-tag">{entry.state}</span>
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title="AI / ML" meta={countLine(pilot.ai_counts, ["READY", "STAGED", "PARTIAL", "NOT STAGED"])}
        info={{ ...INFO.pilotAI, state: `${pilot.capabilities.length} capabilities registered; ${pilot.ai_counts["READY"] ?? 0} ready, ${pilot.ai_counts["STAGED"] ?? 0} staged` }}>
        <ul className="pilot-list">
          {rows.map((item) => (
            <li key={item.id} className="pilot-capability">
              <div className="pilot-row">
                <span className="pilot-row__name">
                  {item.ui}
                  <InfoTip
                    title={item.name}
                    what={`${item.purpose} Output: ${item.output}.`}
                    data={item.requires_data.join(", ")}
                    state={`${item.status}${item.model ? ` · candidate model ${models[item.model]?.name ?? item.model} (${models[item.model]?.deployment_status ?? "unknown"})` : ""}${item.fallback ? ` · fallback today: ${item.fallback}` : ""}`}
                    limits={item.limitations}
                  />
                </span>
                <span className="pilot-tag">{item.status}</span>
              </div>
              <div className="pilot-row__line">{item.purpose}</div>
              {item.retrieval_status && item.retrieval && (
                <div className="pilot-row__line">
                  {item.retrieval.method !== undefined
                    ? <>CLUSTERS {item.retrieval_status} · {item.retrieval.embeddings} embeddings ({item.retrieval.method}) · groupings, not classes</>
                    : item.retrieval.runs !== undefined
                    ? <>MODEL {item.retrieval_status} · {item.retrieval.runs} demo-AOI pair runs · model-generated candidates only</>
                    : <>RETRIEVAL {item.retrieval_status} · {item.retrieval.scenes_indexed}/{item.retrieval.scenes_eligible} demo-AOI scenes, {item.retrieval.chips_indexed} chips</>}
                  {" "}· no NIT Raipur imagery staged
                </div>
              )}
            </li>
          ))}
        </ul>
      </Section>

      <Section title="MODELS" meta={`${pilot.models.filter((m) => m.deployment_status !== "NOT STAGED").length} staged`}>
        {pilot.models.map((model) => (
          <div key={model.id} className="pilot-row" title={`${model.task}\n${model.source}\n${model.compute_notes}`}>
            <span className="pilot-row__name">{model.name}</span>
            <span className="pilot-tag">{model.deployment_status}</span>
          </div>
        ))}
      </Section>
    </>
  );
}
