import { useCallback, useEffect, useState } from "react";
import { ApiError, getJson, postJson } from "../../api";
import { formatDate, formatTime } from "../../utils/format";
import { Facts, Section } from "../intel/common";
import { INFO } from "../../data/infoNotes";

// ---------------------------------------------------------------------------
// DISCOVERY / CLUSTERS (PS 26227 §2.2.4) in the Imagery drawer.
//
// The archive grouped by RemoteCLIP embedding similarity (spherical k-means
// per chip size, built from the cached embeddings; no model runs). Each
// cluster has a neutral name and can be opened to see representative chips
// and shown on the map. Starting from one chip instead: open any chip or map
// tile and use DISCOVER CLUSTER in the intelligence panel.
//
// BUILD makes a new version (re-cluster); UPDATE assigns newly embedded chips
// to the current clusters without re-clustering.
// ---------------------------------------------------------------------------
type FamilyInfo = { n: number; k: number; silhouette: number; candidates: { k: number; silhouette: number }[] };
type Status = {
  state: string; reasons: string[]; method: string; method_version: string; embeddings: number; pending: number; note: string;
  version: { id: string; created_at: string; n_embeddings: number; n_excluded: number; n_clusters: number; incremental_added: number;
    incremental_unassigned: number; parameters: { families: Record<string, FamilyInfo> } } | null;
};
type Cluster = {
  cluster_id: string; label: string; family_px: number; size: number; n_places: number; n_scenes: number; n_aois: number;
  bbox: number[]; first_date: string; last_date: string; representative_ids: string[]; mean_similarity: number;
};

type DiscoverySectionProps = {
  onShowCluster: (clusterId: string, referenceChipId: string | null, bbox: number[]) => void;
  onOpenChipId: (chipId: string) => void;
};

const TONE: Record<string, string> = { READY: "ok", PARTIAL: "pending", INDEXING: "pending", "NOT STAGED": "pending", UNAVAILABLE: "error" };

// Representative chips are loaded as chip records (their bbox gives the thumbnail).
type ChipRecord = { id: string; scene_id: string; bbox: number[]; acquired_at: string };

export default function DiscoverySection({ onShowCluster, onOpenChipId }: DiscoverySectionProps) {
  const [status, setStatus] = useState<Status | null>(null);
  const [clusters, setClusters] = useState<Cluster[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [chips, setChips] = useState<Record<string, ChipRecord>>({});
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(() => {
    getJson<Status>("/api/discovery/status").then(setStatus).catch(() => setStatus(null));
    getJson<Cluster[]>("/api/discovery/clusters").then(setClusters).catch(() => setClusters([]));
  }, []);
  useEffect(load, [load]);

  // While a job runs, poll; reload when it ends.
  useEffect(() => {
    if (status?.state !== "INDEXING") return;
    const timer = window.setTimeout(load, 2000);
    return () => window.clearTimeout(timer);
  }, [status, load]);

  // Representative chips of the opened cluster (for thumbnails).
  useEffect(() => {
    const cluster = clusters.find((c) => c.cluster_id === open);
    if (!cluster) return;
    for (const id of cluster.representative_ids) {
      if (chips[id]) continue;
      getJson<ChipRecord>(`/api/semantic/chips/${encodeURIComponent(id)}`)
        .then((record) => setChips((all) => ({ ...all, [id]: record })))
        .catch(() => undefined);
    }
  }, [open, clusters, chips]);

  async function queue(kind: "build" | "update") {
    try {
      const job = await postJson<{ job_id: number }>(`/api/discovery/${kind}`, {});
      setMessage(kind === "build" ? `Re-clustering queued (job ${job.job_id}): a new version; embeddings are only read.`
        : `Update queued (job ${job.job_id}): new chips are assigned to the current clusters.`);
      setStatus((s) => (s ? { ...s, state: "INDEXING" } : s));
    } catch (error) {
      const detail = error instanceof ApiError ? (error.detail as { state?: string; reasons?: string[] } | null) : null;
      setMessage(detail?.state ? `${detail.state}: ${(detail.reasons ?? []).join("; ")}` : "Not queued: Lumon API unreachable.");
    }
  }

  const state = status?.state ?? "UNAVAILABLE";
  const version = status?.version;
  const families = [...new Set(clusters.map((c) => c.family_px))].sort((a, b) => b - a);

  return (
    <Section title="DISCOVERY / CLUSTERS" meta={`AI/ML · ${state}`}
      info={{ ...INFO.discovery, state: version ? `${state}; ${version.n_clusters} clusters from ${version.n_embeddings} chip embeddings (${version.id})` : state }}>
      <div className="semantic-status">
        <span className={`status-dot status-dot--${TONE[state] ?? "neutral"}`} aria-hidden="true" />
        <span className="semantic-status__state">{state}</span>
        <span className="semantic-status__model">RemoteCLIP embeddings · {status?.method ?? "spherical k-means"}</span>
      </div>
      {version && (
        <Facts rows={[
          ["VERSION", `${version.id} · ${formatTime(version.created_at)}`],
          ["CLUSTERED", `${version.n_embeddings} embeddings${version.n_excluded ? ` (${version.n_excluded} invalid excluded)` : ""} → ${version.n_clusters} clusters`],
          ...Object.entries(version.parameters.families).sort((a, b) => Number(b[0]) - Number(a[0])).map(([family, info]) =>
            [`${Number(family) * 10 / 1000} km`, `k = ${info.k} of ${info.candidates.map((c) => c.k).join("/")} · silhouette ${info.silhouette.toFixed(3)}`] as [string, string]),
          ["INCREMENTAL", `${version.incremental_added} added · ${version.incremental_unassigned} unassigned · ${status?.pending ?? 0} pending`],
        ]} />
      )}
      {status?.reasons.map((reason) => <div key={reason} className="notice notice--partial">{reason}</div>)}
      <div className="notice">{status?.note ?? "Clusters are embedding-based groupings, not semantic classes."}</div>
      <div className="toolbar-row">
        <button type="button" className="action-button" onClick={() => queue("build")} disabled={state === "INDEXING"}>BUILD CLUSTERS</button>
        <button type="button" className="action-button" onClick={() => queue("update")} disabled={state === "INDEXING" || !version}>UPDATE</button>
      </div>
      {message && <div className="notice">{message}</div>}

      {families.map((family) => (
        <div key={family}>
          <div className="intel-subtitle">{family * 10 / 1000} KM CHIPS · {clusters.filter((c) => c.family_px === family).length} CLUSTERS</div>
          {clusters.filter((c) => c.family_px === family).map((cluster) => (
            <div key={cluster.cluster_id} className="source-card">
              <button type="button" className="source-card__head" onClick={() => setOpen(open === cluster.cluster_id ? null : cluster.cluster_id)} aria-expanded={open === cluster.cluster_id}>
                <span className="source-card__name">{cluster.label}</span>
                <span className="source-card__sub">{cluster.size} chips · {cluster.n_places} places · {cluster.n_scenes} dates · compactness {cluster.mean_similarity.toFixed(3)}</span>
              </button>
              {open === cluster.cluster_id && (
                <div className="source-card__body fade-in">
                  <div className="chip-grid">
                    {cluster.representative_ids.map((id) => chips[id] ? (
                      <button key={id} type="button" className="chip-card" onClick={() => onOpenChipId(id)} title={id}>
                        <img src={`/api/scenes/${chips[id].scene_id}/quicklook.png?bbox=${chips[id].bbox.join(",")}`} alt="Representative chip" loading="lazy" />
                        <span className="chip-card__meta">representative</span>
                        <span className="chip-card__meta">{formatDate(chips[id].acquired_at)}</span>
                      </button>
                    ) : <div key={id} className="chip-card"><span className="chip-card__meta">loading…</span></div>)}
                  </div>
                  <Facts rows={[["DATES", `${formatDate(cluster.first_date)} → ${formatDate(cluster.last_date)}`]]} />
                  <button type="button" className="action-button" onClick={() => onShowCluster(cluster.cluster_id, null, cluster.bbox)}>SHOW CLUSTER ON MAP</button>
                </div>
              )}
            </div>
          ))}
        </div>
      ))}
    </Section>
  );
}
