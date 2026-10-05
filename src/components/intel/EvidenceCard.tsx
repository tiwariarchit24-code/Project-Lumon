import { formatDate } from "../../utils/format";

// ---------------------------------------------------------------------------
// A before/after evidence card: two true-colour chips of the SAME place from
// two staged satellite images, with their dates and scene ids.
//
// The chips are rendered by the local API from the stored imagery with one
// fixed colour stretch, so differences between them are real differences
// in the data. The dashed outline marks the change footprint's bounding box.
// ---------------------------------------------------------------------------
type Scene = { id: string; acquired_at: string; sensor: string; quality_status: string; cloud_fraction: number | null };

type EvidenceCardProps = {
  before: Scene | null;
  after: Scene | null;
  bbox: number[]; // [min_lon, min_lat, max_lon, max_lat] of the area shown
};

function Chip({ scene, label, bbox }: { scene: Scene | null; label: string; bbox: number[] }) {
  if (!scene) {
    return (
      <figure className="evidence-chip evidence-chip--missing">
        <div className="evidence-chip__image">NO CLEAR IMAGE</div>
        <figcaption><span className="evidence-chip__label">{label}</span></figcaption>
      </figure>
    );
  }
  return (
    <figure className="evidence-chip">
      <img className="evidence-chip__image" src={`/api/scenes/${scene.id}/quicklook.png?bbox=${bbox.join(",")}&upscale=3`} alt={`${label}: ${scene.id}`} loading="lazy" />
      <figcaption>
        <span className="evidence-chip__label">{label}</span>
        <span className="evidence-chip__date">{formatDate(scene.acquired_at)}</span>
        <span className="evidence-chip__scene" title={scene.id}>{scene.id.split("_").slice(0, 3).join("_")}</span>
      </figcaption>
    </figure>
  );
}

export default function EvidenceCard({ before, after, bbox }: EvidenceCardProps) {
  return (
    <div className="evidence-card">
      <Chip scene={before} label="LAST CLEAR BEFORE" bbox={bbox} />
      <Chip scene={after} label="EARLIEST SUPPORTED AFTER" bbox={bbox} />
    </div>
  );
}
