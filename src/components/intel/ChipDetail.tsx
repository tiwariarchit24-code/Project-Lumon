import { useEffect, useState } from "react";
import { getJson } from "../../api";
import { formatDate, formatTime } from "../../utils/format";
import { Facts, PanelState, Section } from "./common";
import SimilarChips from "./SimilarChips";
import DiscoveryPanel from "./DiscoveryPanel";
import type { QueryResultItem } from "../../types";

// ---------------------------------------------------------------------------
// One image chip found by semantic image search (RemoteCLIP).
//
// Shows the chip exactly as the model saw it (same fixed colour stretch),
// the scene it comes from with its original metadata, the chip footprint,
// the search score (model similarity, NOT a probability or detection) and
// provenance. From here the analyst continues with the existing imagery
// workflow: the scene is shown on the map, and TILE HISTORY opens the 100 m
// tile at the chip centre. SIMILAR IMAGE CHIPS finds chips that look like
// this one (RemoteCLIP image-to-image similarity).
// ---------------------------------------------------------------------------
type ChipRecord = {
  id: string; scene_id: string; aoi_id: string; acquired_at: string; chip_px: number; footprint: GeoJSON.Polygon; bbox: number[];
  lon: number; lat: number; valid_fraction: number; file_sha256: string | null; model_key: string; preprocess_version: string;
  sensor: string; processing_level: string | null; crs: string | null; cloud_fraction: number | null; quality_status: string; source_url: string | null;
  provenance: { id: string; processing: string; processing_version: string; created_at: string } | null;
  scene_provenance: { id: string; input_ref: string; retrieved_at: string | null } | null;
};

type ChipDetailProps = {
  id: string;
  score?: number;
  rank?: number;
  query?: string;
  scoreKind?: string;
  onHighlight: (geometry: GeoJSON.Geometry | null) => void;
  onOpenTile: (lon: number, lat: number) => void;
  onOpenChip: (item: QueryResultItem) => void;
  onShowCluster: (clusterId: string, referenceChipId: string | null, bbox: number[]) => void;
};

export default function ChipDetail({ id, score, rank, query, scoreKind, onHighlight, onOpenTile, onOpenChip, onShowCluster }: ChipDetailProps) {
  const [chip, setChip] = useState<ChipRecord | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setChip(null);
    setError(null);
    getJson<ChipRecord>(`/api/semantic/chips/${encodeURIComponent(id)}`)
      .then((record) => {
        setChip(record);
        onHighlight({ type: "MultiPolygon", coordinates: [record.footprint.coordinates] });
      })
      .catch((e) => setError(e.offline ? "LUMON API UNREACHABLE" : "CHIP NOT INDEXED"));
  }, [id, onHighlight]);

  if (error) return <PanelState state="error" text={error} />;
  if (!chip) return <PanelState state="loading" />;

  const km = (chip.chip_px * 10) / 1000;
  return (
    <div className="detail fade-in">
      <div className="detail__kind">IMAGE CHIP · REMOTECLIP INDEX</div>
      <h2 className="detail__title">{formatDate(chip.acquired_at)} · {km} km chip</h2>
      <div className="detail__tags">
        <span className="evidence-tag evidence-tag--inferred">MODEL SIMILARITY</span>
        <span className="plain-tag">{chip.sensor.toUpperCase()}</span>
      </div>
      {query !== undefined && (
        <div className="notice detail__banner">
          {scoreKind?.startsWith("cosine similarity of image embeddings")
            ? <>Ranked #{rank} as looking like the example chip, image cosine similarity {score?.toFixed(4)}. Looking alike
                is not evidence of the same objects or activity: check the images yourself.</>
            : <>Ranked #{rank} for “{query}” with cosine similarity {score?.toFixed(4)}. This is how close the model's image and
                text embeddings are. It is not a probability or a detection: check the image yourself.</>}
        </div>
      )}

      <Section title="CHIP (AS THE MODEL SAW IT)">
        <img className="tile-chip" src={`/api/scenes/${chip.scene_id}/quicklook.png?bbox=${chip.bbox.join(",")}&upscale=2`} alt={`Image chip from ${formatDate(chip.acquired_at)}`} />
        <div className="empty-note">True colour (B04/B03/B02), fixed 0–0.3 reflectance stretch, {Math.round(chip.valid_fraction * 100)}% clear pixels (SCL).</div>
      </Section>

      <Section title="SCENE">
        <Facts rows={[
          ["SCENE ID", chip.scene_id],
          ["ACQUIRED", formatTime(chip.acquired_at)],
          ["AOI", chip.aoi_id],
          ["LEVEL", chip.processing_level],
          ["QUALITY", `${chip.quality_status}${chip.cloud_fraction !== null ? ` · ${Math.round(chip.cloud_fraction * 100)}% cloud in AOI` : ""}`],
          ["FILE SHA-256", chip.file_sha256 ? `${chip.file_sha256.slice(0, 16)}…` : null],
        ]} />
      </Section>

      <Section title="FOOTPRINT">
        <Facts rows={[
          ["CENTRE", `${chip.lat.toFixed(5)}, ${chip.lon.toFixed(5)}`],
          ["BOUNDS", chip.bbox.map((v) => v.toFixed(4)).join(", ")],
          ["SIZE", `${chip.chip_px} × ${chip.chip_px} px at 10 m (${km} × ${km} km), from the raster's own geotransform (${chip.crs})`],
        ]} />
        <button type="button" className="action-button" onClick={() => onOpenTile(chip.lon, chip.lat)}>TILE HISTORY AT CHIP CENTRE</button>
      </Section>

      <Section title="SIMILAR IMAGE CHIPS" meta="RemoteCLIP · AI/ML">
        <SimilarChips chipId={chip.id} onOpenChip={onOpenChip} />
      </Section>

      <Section title="DISCOVERY CLUSTER" meta="RemoteCLIP · AI/ML">
        <DiscoveryPanel chipId={chip.id} onOpenChip={onOpenChip} onShowCluster={onShowCluster} />
      </Section>

      <Section title="PROVENANCE">
        <Facts rows={[
          ["MODEL", chip.model_key],
          ["PREPROCESSING", chip.preprocess_version],
          ["EMBEDDED", chip.provenance ? `${formatTime(chip.provenance.created_at)} · ${chip.provenance.id}` : null],
          ["SCENE SOURCE", chip.scene_provenance?.input_ref ?? chip.source_url],
        ]} />
      </Section>
    </div>
  );
}
