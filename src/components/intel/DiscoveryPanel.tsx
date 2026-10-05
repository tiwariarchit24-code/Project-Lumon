import { useEffect, useState } from "react";
import { ApiError, postJson } from "../../api";
import { formatDate } from "../../utils/format";
import { Facts } from "./common";
import type { QueryResultItem } from "../../types";

// ---------------------------------------------------------------------------
// DISCOVERY from a reference chip (PS 26227 §2.2.4).
//
//   reference chip -> the embedding cluster it belongs to -> the other
//   member PLACES of that cluster, ranked by similarity to the reference
//
// Unlike image similarity (a ranked list of nearest chips), this shows a
// pre-computed GROUP of the whole archive that the analyst can explore and
// show on the map. Cluster names are neutral (CLUSTER 07 · 1.12 km): a
// cluster is an embedding-based grouping, not a semantic class.
// ---------------------------------------------------------------------------
type Place = {
  place: string; aoi_id: string; best_chip_id: string; scene_id: string; acquired_at: string; score: number | null;
  dates: number; first_date: string; last_date: string; footprint: GeoJSON.Polygon; bbox: number[]; lon: number; lat: number;
  is_reference_place: boolean;
};
type Discovery = {
  cluster: { cluster_id: string; label: string; size: number; n_places: number; n_scenes: number; n_aois: number; bbox: number[];
    first_date: string; last_date: string; mean_similarity: number; representative_ids: string[] };
  membership: string; reference_chip_id: string; version_id: string; score_kind: string; places: Place[]; note: string;
};

type DiscoveryPanelProps = {
  chipId?: string;                    // the reference chip ...
  at?: { lon: number; lat: number };  // ... or the chip under this map point
  onOpenChip: (item: QueryResultItem) => void;
  onShowCluster: (clusterId: string, referenceChipId: string | null, bbox: number[]) => void;
};

const PLACE_SCORE_KIND = "cosine similarity of image embeddings (cluster member vs reference chip; not a probability)";

export default function DiscoveryPanel({ chipId, at, onOpenChip, onShowCluster }: DiscoveryPanelProps) {
  const [result, setResult] = useState<Discovery | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // A new reference starts fresh.
  useEffect(() => {
    setResult(null);
    setMessage(null);
  }, [chipId, at?.lon, at?.lat]);

  async function run() {
    setBusy(true);
    setMessage(null);
    try {
      setResult(await postJson<Discovery>("/api/discovery/discover", chipId ? { chip_id: chipId } : { at }));
    } catch (error) {
      const detail = error instanceof ApiError ? (error.detail as { state?: string; reasons?: string[] } | string | null) : null;
      if (detail && typeof detail === "object" && detail.state) setMessage(`${detail.state}: ${(detail.reasons ?? []).join("; ")}`);
      else setMessage(error instanceof ApiError && error.status === 404 ? "No indexed image chip here." : `Not available: ${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  function open(place: Place) {
    onOpenChip({
      kind: "chip", id: place.best_chip_id, title: `${result?.cluster.label} member`, type: "image-chip", lon: place.lon, lat: place.lat,
      evidence_type: "MODEL SIMILARITY", why: [], score: place.score ?? undefined, score_kind: PLACE_SCORE_KIND,
      scene_id: place.scene_id, aoi_id: place.aoi_id, acquired_at: place.acquired_at, footprint: place.footprint, bbox: place.bbox,
      query: `${result?.cluster.label} (reference ${result?.reference_chip_id})`,
    });
  }

  if (!result) {
    return (
      <div className="similar-chips">
        <button type="button" className="action-button" onClick={run} disabled={busy}>{busy ? "LOOKING UP…" : "DISCOVER CLUSTER"}</button>
        {message && <div className="notice notice--partial">{message}</div>}
      </div>
    );
  }

  const cluster = result.cluster;
  const others = result.places.filter((p) => !p.is_reference_place);
  return (
    <div className="similar-chips fade-in">
      <div className="discovery-head">
        <span className="discovery-head__label">{cluster.label}</span>
        <span className="discovery-head__meta">{result.membership === "member" ? "reference is a member" : result.membership}</span>
      </div>
      <Facts rows={[
        ["SIZE", `${cluster.size} chips · ${cluster.n_places} places · ${cluster.n_scenes} dates${cluster.n_aois > 1 ? ` · ${cluster.n_aois} AOIs` : ""}`],
        ["DATES", `${formatDate(cluster.first_date)} → ${formatDate(cluster.last_date)}`],
        ["COMPACTNESS", `${cluster.mean_similarity.toFixed(3)} mean member–centroid cosine`],
        ["VERSION", result.version_id],
      ]} />
      <div className="notice">{result.note}</div>
      <button type="button" className="action-button" onClick={() => onShowCluster(cluster.cluster_id, result.reference_chip_id, cluster.bbox)}>
        SHOW CLUSTER ON MAP
      </button>
      <div className="intel-subtitle">OTHER PLACES IN THIS CLUSTER ({others.length}) · ranked by similarity to the reference</div>
      {others.length === 0 && <div className="empty-note">The cluster holds only the reference place.</div>}
      <div className="chip-grid">
        {others.map((place) => (
          <button key={place.place} type="button" className="chip-card" onClick={() => open(place)}
            title={`${place.best_chip_id}\n${place.dates} dates in this cluster (${place.first_date.slice(0, 10)} → ${place.last_date.slice(0, 10)})`}>
            <img src={`/api/scenes/${place.scene_id}/quicklook.png?bbox=${place.bbox.join(",")}`} alt={`Member place, ${formatDate(place.acquired_at)}`} loading="lazy" />
            <span className="chip-card__meta">{place.score?.toFixed(3)} · {place.dates} dates</span>
            <span className="chip-card__meta">best date {formatDate(place.acquired_at)}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
