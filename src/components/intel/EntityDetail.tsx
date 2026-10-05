import { useEffect, useState } from "react";
import { getJson } from "../../api";
import { formatDistance, formatTime, labelCase } from "../../utils/format";
import { EvidenceTag, Facts, ListButton, PanelState, ProvenanceBlock, Section, SourceCard } from "./common";
import type { ProvenanceRecord, SourceInfo } from "./common";
import { INFO } from "../../data/infoNotes";

// ---------------------------------------------------------------------------
// Details of one entity (airport, port, power plant): its attributes, the
// source card, provenance, recent events nearby, and the nearest entities of
// the same type ("similar entities" by type and proximity).
// ---------------------------------------------------------------------------
type EntityRecord = {
  id: string; type: string; category: string; name: string; geometry: GeoJSON.Geometry; lon: number; lat: number;
  attributes: Record<string, unknown>; updated_at: string;
  source: SourceInfo | null; provenance: ProvenanceRecord | null;
  related_events: { id: string; title: string; type: string; start_time: string; distance_m: number }[];
  similar_entities: { id: string; name: string; distance_m: number }[];
};

type EntityDetailProps = {
  id: string;
  onHighlight: (geometry: GeoJSON.Geometry | null) => void;
  onSelectEvent: (id: string) => void;
  onSelectEntity: (id: string) => void;
};

export default function EntityDetail({ id, onHighlight, onSelectEvent, onSelectEntity }: EntityDetailProps) {
  const [entity, setEntity] = useState<EntityRecord | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setEntity(null);
    setError(null);
    getJson<EntityRecord>(`/api/entities/${encodeURIComponent(id)}`)
      .then((data) => {
        setEntity(data);
        onHighlight(data.geometry);
      })
      .catch((e) => setError(e.offline ? "LUMON API UNREACHABLE" : "ENTITY NOT FOUND"));
  }, [id, onHighlight]);

  if (error) return <PanelState state="error" text={error} />;
  if (!entity) return <PanelState state="loading" />;

  const rows: [string, React.ReactNode][] = Object.entries(entity.attributes)
    .filter(([, value]) => value !== null && value !== "")
    .map(([key, value]) => [labelCase(key), String(value)]);

  return (
    <div className="detail fade-in">
      <div className="detail__kind">ENTITY · {labelCase(entity.type)}</div>
      <h2 className="detail__title">{entity.name}</h2>
      <div className="detail__tags"><EvidenceTag type="GIS-DERIVED" /><span className="plain-tag">{entity.category}</span></div>

      <Section title="ENTITY">
        <Facts rows={[
          ["LOCATION", `${entity.lat.toFixed(4)}, ${entity.lon.toFixed(4)}`],
          ["RECORD UPDATED", formatTime(entity.updated_at)],
          ...rows,
        ]} />
      </Section>

      <Section title="SOURCE"><SourceCard source={entity.source} /></Section>

      <Section title="RECENT EVENTS NEARBY" meta="within 50 km">
        {entity.related_events.length === 0 && <div className="empty-note">No staged events within 50 km.</div>}
        {entity.related_events.map((event) => (
          <ListButton key={event.id} title={event.title} meta={`${formatTime(event.start_time)} · ${formatDistance(event.distance_m)}`} onClick={() => onSelectEvent(event.id)} />
        ))}
      </Section>

      <Section title={`NEAREST ${labelCase(entity.type)}S`}>
        {entity.similar_entities.map((other) => (
          <ListButton key={other.id} title={other.name} meta={formatDistance(other.distance_m)} onClick={() => onSelectEntity(other.id)} />
        ))}
      </Section>

      <Section title="PROVENANCE" info={{ ...INFO.provenance, state: entity.provenance ? `${entity.provenance.kind}, ${entity.provenance.processing_version}` : "no record" }}><ProvenanceBlock record={entity.provenance} /></Section>
    </div>
  );
}
