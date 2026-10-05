import { useEffect, useState } from "react";
import { getJson, postJson, query } from "../../api";
import { formatDate, labelCase } from "../../utils/format";
import { Facts, ListButton, PanelState, Section } from "./common";
import SimilarChips from "./SimilarChips";
import DiscoveryPanel from "./DiscoveryPanel";
import type { QueryResultItem } from "../../types";

// ---------------------------------------------------------------------------
// The observation history of one 100 m imagery tile (click inside an AOI).
//
// The strip shows every staged scene in date order, coloured by the land
// class the classifier gave this tile in that image. Hatched cells are
// cloudy/masked looks: they are shown as missing, never filled in.
// ---------------------------------------------------------------------------
type HistoryItem = { scene_id: string; date: string; valid_fraction: number; land_class: string | null; class_fractions: Record<string, number> | null };
type TileRecord = {
  tile_id: string; aoi_id: string; aoi_name: string; geometry: GeoJSON.Geometry; lon: number; lat: number;
  history: HistoryItem[]; changes: { id: string; change_class: string; status: string; earliest_supported_after: string }[];
  classifier: string; scl_invalid_classes: string[];
};
type Similar = { tile_id: string; similarity: number; lon: number; lat: number; why: string[] };

type TileDetailProps = {
  lon: number;
  lat: number;
  onHighlight: (geometry: GeoJSON.Geometry | null) => void;
  onSelectChange: (id: string) => void;
  onSelectTile: (lon: number, lat: number) => void;
  onOpenChip: (item: QueryResultItem) => void;
  onShowCluster: (clusterId: string, referenceChipId: string | null, bbox: number[]) => void;
};

export default function TileDetail({ lon, lat, onHighlight, onSelectChange, onSelectTile, onOpenChip, onShowCluster }: TileDetailProps) {
  const [tile, setTile] = useState<TileRecord | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [similar, setSimilar] = useState<Similar[] | null>(null);

  useEffect(() => {
    setTile(null);
    setError(null);
    setSimilar(null);
    getJson<TileRecord>(`/api/tiles/at${query({ lon, lat })}`)
      .then((data) => {
        setTile(data);
        onHighlight(data.geometry);
      })
      .catch((e) => setError(e.offline ? "LUMON API UNREACHABLE" : "NO IMAGERY STAGED AT THIS LOCATION"));
  }, [lon, lat, onHighlight]);

  async function findSimilar() {
    if (!tile) return;
    try {
      const result = await postJson<{ results: Similar[] }>("/api/similar", { aoi_id: tile.aoi_id, positive: [tile.tile_id], limit: 8 });
      setSimilar(result.results);
    } catch {
      setSimilar([]);
    }
  }

  if (error) return <PanelState state="error" text={error} />;
  if (!tile) return <PanelState state="loading" />;

  const clear = tile.history.filter((h) => h.land_class);
  const latest = clear[clear.length - 1];
  const pad = 0.003;

  return (
    <div className="detail fade-in">
      <div className="detail__kind">IMAGERY TILE · 100 M</div>
      <h2 className="detail__title">{tile.tile_id}</h2>
      <div className="detail__tags"><span className="plain-tag">{tile.aoi_name}</span></div>

      <Section title="LATEST CLEAR IMAGE">
        {latest ? (
          <img className="tile-chip" src={`/api/scenes/${latest.scene_id}/quicklook.png?bbox=${[tile.lon - pad, tile.lat - pad, tile.lon + pad, tile.lat + pad].join(",")}&upscale=4`} alt={`Latest clear image of ${tile.tile_id}`} />
        ) : <div className="empty-note">No clear observation.</div>}
        {latest && <div className="empty-note">{formatDate(latest.date)} · {latest.scene_id}</div>}
      </Section>

      <Section title="OBSERVATION HISTORY" meta={`${clear.length}/${tile.history.length} clear`}>
        <div className="class-strip" role="img" aria-label="Land class per observation">
          {tile.history.map((item) => (
            <span key={item.scene_id} className={`class-cell class-cell--${item.land_class ?? "masked"}`} title={`${item.date}: ${item.land_class ?? "masked (cloud/shadow)"} · ${Math.round(item.valid_fraction * 100)}% clear`} />
          ))}
        </div>
        <div className="class-legend">
          {["water", "vegetation", "bare", "built", "mixed", "masked"].map((name) => (
            <span key={name}><span className={`class-cell class-cell--${name}`} />{name}</span>
          ))}
        </div>
        <Facts rows={[
          ["FIRST", formatDate(tile.history[0]?.date)],
          ["LAST", formatDate(tile.history[tile.history.length - 1]?.date)],
          ["CLASSIFIER", tile.classifier],
          ["MASKED AS", tile.scl_invalid_classes.join(", ")],
        ]} />
      </Section>

      <Section title="CHANGE EVENTS" meta={`${tile.changes.length}`}>
        {tile.changes.length === 0 && <div className="empty-note">No change candidate includes this tile.</div>}
        {tile.changes.map((change) => (
          <ListButton key={change.id} title={`${labelCase(change.change_class)} · ${change.status}`} meta={`first supported ${change.earliest_supported_after}`} onClick={() => onSelectChange(change.id)} />
        ))}
      </Section>

      {/* ML: RemoteCLIP embeddings of the image chip at this point. Kept
          separate from the non-semantic spectral "similar tiles" below. */}
      <Section title="SIMILAR IMAGERY" meta="RemoteCLIP · AI/ML">
        <SimilarChips at={{ lon, lat }} onOpenChip={onOpenChip} />
      </Section>

      <Section title="DISCOVERY CLUSTER" meta="RemoteCLIP · AI/ML">
        <DiscoveryPanel at={{ lon, lat }} onOpenChip={onOpenChip} onShowCluster={onShowCluster} />
      </Section>

      <Section title="SIMILAR TILES" meta="non-semantic">
        <button type="button" className="action-button" onClick={findSimilar}>FIND TILES LIKE THIS</button>
        {similar && similar.length === 0 && <div className="empty-note">No similar tiles.</div>}
        {similar?.map((item) => (
          <ListButton key={item.tile_id} title={`${item.tile_id} · sim ${item.similarity.toFixed(2)}`} meta={item.why.join(" · ")} onClick={() => onSelectTile(item.lon, item.lat)} />
        ))}
      </Section>
    </div>
  );
}
