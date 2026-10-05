import { useEffect, useState } from "react";
import { getJson, postJson } from "../../api";
import { formatDate } from "../../utils/format";
import { Facts, PanelState, Section } from "../intel/common";
import { INFO } from "../../data/infoNotes";
import SemanticSearch from "./SemanticSearch";
import DiscoverySection from "./DiscoverySection";
import type { QueryResultItem } from "../../types";

// ---------------------------------------------------------------------------
// IMAGERY ARCHIVE: the staged satellite AOIs and every scene record,
// including scenes that were QUARANTINED (with the reason) or found unusable.
// Clicking a usable scene's date shows that image on the map, so dates can
// be compared directly.
//
// SEMANTIC IMAGE SEARCH (RemoteCLIP, the first AI/ML capability) sits at the
// top: text -> ranked image chips from the same archive. DISCOVERY / CLUSTERS
// follows: the archive grouped by embedding similarity.
// ---------------------------------------------------------------------------
type Aoi = {
  id: string; latest_scene: string | null; name: string; label: string; purpose: string; bbox: number[]; scenes: number; first_scene: string | null; last_scene: string | null;
  quarantined: number; unusable: number; changes_accepted: number; changes_suppressed: number;
  imagery: { provider: string; collection: string; max_scene_cloud_percent: number | null }; // null: local AOI (no download filter)
};
type Scene = { id: string; acquired_at: string; quality_status: string; quarantine_reason: string | null; cloud_fraction: number | null; processing_level: string; radiometric_offset: number | null };

type ImagerySectionProps = {
  onFlyToBbox: (bbox: number[]) => void;
  sceneChoice: Record<string, string>;
  onShowScene: (aoiId: string, sceneId: string) => void;
  onOpenResult: (item: QueryResultItem) => void;
  onShowCluster: (clusterId: string, referenceChipId: string | null, bbox: number[]) => void;
  onOpenChipId: (chipId: string) => void;
};

export default function ImagerySection({ onFlyToBbox, sceneChoice, onShowScene, onOpenResult, onShowCluster, onOpenChipId }: ImagerySectionProps) {
  const [aois, setAois] = useState<Aoi[] | null>(null);
  const [scenes, setScenes] = useState<Record<string, Scene[]>>({});
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    getJson<Aoi[]>("/api/aois")
      .then((data) => {
        setAois(data);
        for (const aoi of data) getJson<Scene[]>(`/api/aois/${aoi.id}/scenes`).then((list) => setScenes((s) => ({ ...s, [aoi.id]: list })));
      })
      .catch(() => setError(true));
  }, []);

  async function ingest(aoiId: string) {
    try {
      const job = await postJson<{ job_id: number }>(`/api/aois/${aoiId}/ingest`, {});
      setMessage(`Incremental ingest queued (job ${job.job_id}). Needs CONNECTED mode; in air-gapped mode the job fails safely.`);
    } catch {
      setMessage("Ingest not queued: Lumon API unreachable.");
    }
  }

  if (error) return <PanelState state="error" text="LUMON API UNREACHABLE" />;
  if (!aois) return <PanelState state="loading" />;

  return (
    <>
      <SemanticSearch onOpenResult={onOpenResult} />
      <DiscoverySection onShowCluster={onShowCluster} onOpenChipId={onOpenChipId} />
      {aois.map((aoi) => (
        <Section key={aoi.id} title={`${aoi.label} · ${aoi.id}`} meta={aoi.name}
          info={{ ...INFO.imagery, state: `${aoi.scenes} usable, ${aoi.quarantined} quarantined, ${aoi.unusable} unusable; ${aoi.first_scene?.slice(0, 10) ?? "—"} to ${aoi.last_scene?.slice(0, 10) ?? "—"}` }}>
          <Facts rows={[
            ["SCENES", `${aoi.scenes} usable · ${aoi.quarantined} quarantined · ${aoi.unusable} unusable`],
            ["COVERAGE", aoi.first_scene ? `${formatDate(aoi.first_scene)} → ${formatDate(aoi.last_scene)}` : "NO IMAGERY STAGED"],
            ["SENSOR", aoi.imagery.max_scene_cloud_percent === null ? `${aoi.imagery.collection} files (sensor per scene)` : `${aoi.imagery.collection} (≤ ${aoi.imagery.max_scene_cloud_percent}% scene cloud)`],
            ["PROVIDER", aoi.imagery.provider],
            ["CHANGES", `${aoi.changes_accepted} accepted · ${aoi.changes_suppressed} suppressed`],
          ]} />
          <div className="empty-note">{aoi.purpose}</div>
          <div className="toolbar-row">
            <button type="button" className="action-button" onClick={() => onFlyToBbox(aoi.bbox)}>FLY TO AOI</button>
            {aoi.latest_scene && <button type="button" className="action-button" onClick={() => { onShowScene(aoi.id, aoi.latest_scene!); onFlyToBbox(aoi.bbox); }}>SHOW LATEST IMAGE</button>}
            <button type="button" className="action-button" onClick={() => ingest(aoi.id)}>INGEST NEW SCENES</button>
          </div>
          <div className="empty-note">Click a date to show that image on the map (Sentinel-2 layer).</div>
          <table className="table">
            <thead><tr><th>DATE</th><th>STATUS</th><th>CLOUD</th><th>NOTE</th></tr></thead>
            <tbody>
              {(scenes[aoi.id] ?? []).map((scene) => (
                <tr key={scene.id} className={`row--${scene.quality_status}${(sceneChoice[aoi.id] ?? aoi.latest_scene) === scene.id ? " row--shown" : ""}`} title={scene.id}>
                  <td>
                    {scene.quality_status === "usable" || scene.quality_status === "degraded" ? (
                      <button type="button" className="text-button" onClick={() => { onShowScene(aoi.id, scene.id); onFlyToBbox(aoi.bbox); }} title="Show this image on the map">
                        {formatDate(scene.acquired_at)}
                      </button>
                    ) : formatDate(scene.acquired_at)}
                  </td>
                  <td>{scene.quality_status.toUpperCase()}</td>
                  <td>{scene.cloud_fraction === null ? "—" : `${Math.round(scene.cloud_fraction * 100)}%`}</td>
                  <td className="table__note">{scene.quarantine_reason ?? `${scene.processing_level.replace("L2A baseline ", "PB ")}${scene.radiometric_offset ? ` · offset ${scene.radiometric_offset}` : ""}`}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      ))}
      {message && <div className="notice">{message}</div>}
      <Section title="LANDSAT / SENTINEL-1">
        <div className="empty-note">NOT STAGED. Only Sentinel-2 L2A is ingested in this build.</div>
      </Section>
    </>
  );
}
