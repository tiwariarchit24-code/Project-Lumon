import { useEffect, useState } from "react";
import { getJson } from "../../api";
import { formatDistance, formatTime, labelCase } from "../../utils/format";
import { EvidenceTag, Facts, ListButton, PanelState, ProvenanceBlock, Section, SourceCard } from "./common";
import type { ProvenanceRecord, SourceInfo } from "./common";
import { SCHEDULED_TYPES, TELEMETRY_TYPES } from "../../data/eventKinds";
import { INFO } from "../../data/infoNotes";

// ---------------------------------------------------------------------------
// Details of one OSINT event (earthquake, flood alert, aircraft position...).
// Shows what the source said, where it came from (source card + provenance),
// nearby events, and a link to satellite context when an imagery AOI exists.
// ---------------------------------------------------------------------------
type EventRecord = {
  id: string; type: string; category: string; title: string; description: string | null;
  geometry: GeoJSON.Geometry | null; lon: number | null; lat: number | null;
  start_time: string | null; end_time: string | null; status: string | null; evidence_type: string; updated_at?: string;
  attributes: Record<string, unknown>; raw: unknown;
  source: SourceInfo | null; provenance: ProvenanceRecord | null;
  related_events: { id: string; title: string; type: string; start_time: string; distance_m: number }[];
  nearest_aoi: { id: string; name: string; distance_m: number } | null;
};

type EventDetailProps = {
  id: string;
  onHighlight: (geometry: GeoJSON.Geometry | null) => void;
  onSelectEvent: (id: string) => void;
  onOpenAoi: (aoiId: string) => void;
};

// Attributes worth showing, in a friendly order. Unknown keys are listed after.
const ATTRIBUTE_LABELS: Record<string, string> = {
  magnitude: "MAGNITUDE", magnitude_type: "MAG TYPE", depth_km: "DEPTH (KM)", alert_level: "ALERT LEVEL", alert: "PAGER ALERT",
  callsign: "CALLSIGN", icao24: "ICAO24", origin_country: "REG. COUNTRY", baro_altitude_m: "ALTITUDE (M)", velocity_m_s: "SPEED (M/S)",
  track_deg: "TRACK (°)", temperature_2m: "TEMPERATURE", wind_speed_10m: "WIND", precipitation: "PRECIPITATION", cloud_cover: "CLOUD %",
  frp_mw: "FIRE POWER (MW)", source_confidence: "SOURCE'S OWN FLAG", status: "STATUS", time_precision: "DATE PRECISION",
  pad: "PAD", provider: "PROVIDER", mission: "MISSION", datasource: "SIGNAL", ioda_score: "IODA SCORE", kp: "KP", scope: "SCOPE",
  upstream_source: "UPSTREAM", country: "COUNTRY", eonet_category: "EONET CATEGORY",
};

export default function EventDetail({ id, onHighlight, onSelectEvent, onOpenAoi }: EventDetailProps) {
  const [event, setEvent] = useState<EventRecord | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showRaw, setShowRaw] = useState(false);

  useEffect(() => {
    setEvent(null);
    setError(null);
    getJson<EventRecord>(`/api/events/${encodeURIComponent(id)}`)
      .then((data) => {
        setEvent(data);
        onHighlight(data.geometry);
      })
      .catch((e) => setError(e.offline ? "LUMON API UNREACHABLE" : "EVENT NOT FOUND"));
  }, [id, onHighlight]);

  if (error) return <PanelState state="error" text={error} />;
  if (!event) return <PanelState state="loading" />;

  // Honesty flags (same rules as the map markers):
  //   scheduled - a planned future event (e.g. an upcoming launch)
  //   stale     - telemetry whose source data is older than its freshness limit
  const ageHours = event.start_time ? (Date.now() - Date.parse(event.start_time)) / 3_600_000 : null;
  const scheduled = ageHours !== null && ageHours < 0 && SCHEDULED_TYPES.includes(event.type);
  const stale = TELEMETRY_TYPES.includes(event.type) && event.source?.freshness_status === "STALE";

  const attributeRows: [string, React.ReactNode][] = Object.entries(event.attributes)
    .filter(([key, value]) => value !== null && value !== "" && key !== "source_url" && key !== "units" && typeof value !== "object")
    .map(([key, value]) => [ATTRIBUTE_LABELS[key] ?? labelCase(key), String(value)]);

  return (
    <div className="detail fade-in">
      <div className="detail__kind">EVENT · {labelCase(event.type)}</div>
      <h2 className="detail__title">{event.title}</h2>
      <div className="detail__tags">
        <EvidenceTag type={event.evidence_type} />
        <span className="plain-tag">{event.category}</span>
        {scheduled && <span className="plain-tag plain-tag--scheduled">SCHEDULED</span>}
        {stale && <span className="plain-tag plain-tag--stale">STALE</span>}
      </div>
      {scheduled && (
        <div className="notice notice--partial detail__banner">
          SCHEDULED — this has NOT happened. Planned for {formatTime(event.start_time)}
          {event.attributes.time_precision ? ` (date known to the ${String(event.attributes.time_precision).toLowerCase()})` : ""}; the provider may change it.
        </div>
      )}
      {stale && ageHours !== null && (
        <div className="notice detail__banner">
          STALE — this position was reported {ageHours < 1 ? `${Math.round(ageHours * 60)} min` : `${ageHours.toFixed(1)} h`} ago.
          The object has moved since; refresh the source in CONNECTED mode for a current position.
        </div>
      )}

      <Section title="WHEN / WHERE">
        <Facts rows={[
          [scheduled ? "PLANNED" : TELEMETRY_TYPES.includes(event.type) ? "OBSERVED" : "TIME", formatTime(event.start_time)],
          // When Lumon stored it, kept apart from when it was observed.
          ["INGESTED", event.updated_at ? formatTime(event.updated_at) : null],
          ["UNTIL", event.end_time ? formatTime(event.end_time) : null],
          ["LOCATION", event.lat !== null ? `${event.lat.toFixed(4)}, ${event.lon!.toFixed(4)}` : "National / global scope (no location)"],
          ["STATUS", event.status],
          ["CONFIDENCE", "Not computed by Lumon — see evidence type"],
        ]} />
      </Section>

      {attributeRows.length > 0 && <Section title="REPORTED VALUES"><Facts rows={attributeRows} /></Section>}
      {event.description && <Section title="DESCRIPTION"><p className="detail__text">{event.description.replace(/<[^>]+>/g, "")}</p></Section>}

      <Section title="SOURCE"><SourceCard source={event.source} /></Section>

      {event.nearest_aoi && (
        <Section title="SATELLITE CONTEXT">
          {event.nearest_aoi.distance_m < 50_000 ? (
            <button type="button" className="action-button" onClick={() => onOpenAoi(event.nearest_aoi!.id)}>
              OPEN AOI {event.nearest_aoi.name} ({formatDistance(event.nearest_aoi.distance_m)})
            </button>
          ) : (
            <div className="empty-note">
              NO IMAGERY STAGED near this event. Nearest AOI: {event.nearest_aoi.name}, {formatDistance(event.nearest_aoi.distance_m)} away.
              Add an AOI in config/aois.json to analyse this area.
            </div>
          )}
        </Section>
      )}

      <Section title="RELATED EVENTS" meta={`${event.related_events.length} within 100 km`}>
        {event.related_events.length === 0 && <div className="empty-note">No other staged events within 100 km.</div>}
        {event.related_events.map((related) => (
          <ListButton key={related.id} title={related.title} meta={`${formatTime(related.start_time)} · ${formatDistance(related.distance_m)}`} onClick={() => onSelectEvent(related.id)} />
        ))}
      </Section>

      <Section title="PROVENANCE" info={{ ...INFO.provenance, state: event.provenance ? `${event.provenance.kind}, ${event.provenance.processing_version}` : "no record" }}><ProvenanceBlock record={event.provenance} /></Section>

      <Section title="RAW SOURCE RECORD">
        <button type="button" className="text-button" onClick={() => setShowRaw(!showRaw)}>{showRaw ? "Hide" : "Show"} original record</button>
        {showRaw && <pre className="raw">{JSON.stringify(event.raw, null, 2)}</pre>}
      </Section>
    </div>
  );
}
