import { useEffect, useState } from "react";
import { getJson, postJson, query } from "../../api";
import { formatTime, labelCase } from "../../utils/format";
import { Freshness, ListButton, PanelState, Section } from "../intel/common";
import { UPCOMING_DAYS } from "../../utils/layers";
import type { Layer, Selection, TimeWindow } from "../../types";
import LayerSwitch from "../LayerSwitch";

// ---------------------------------------------------------------------------
// One OSINT category (Aviation, Weather, Disasters...):
//   - its map layers with on/off switches
//   - the records in the current map view and time window
//   - national/global records that have no location (outages, space weather)
//   - the sources behind the category, with their health
// ---------------------------------------------------------------------------
type Feature = { properties: { id: string; title: string; time: string; type: string; stale?: boolean; scheduled?: boolean; time_precision?: string | null } };
type EventsResponse = { features: Feature[]; total: number; upcoming_total: number };
type Nonspatial = { id: string; title: string; start_time: string; type: string };
type Source = {
  id: string; name: string; category: string; health: string; last_success: string | null; record_count: number; adapter: string | null;
  freshness_status: string; freshness_label: string;
};

type OsintSectionProps = {
  group: string;
  layers: Layer[];
  timeWindow: TimeWindow;
  bbox: number[] | null;
  onToggle: (layerId: string) => void;
  onSelect: (selection: Selection) => void;
};

export default function OsintSection({ group, layers, timeWindow, bbox, onToggle, onSelect }: OsintSectionProps) {
  const groupLayers = layers.filter((l) => l.group === group);
  const eventTypes = groupLayers.filter((l) => l.kind === "events" && !l.nonspatial).flatMap((l) => l.types ?? []);
  const nonspatialTypes = groupLayers.filter((l) => l.nonspatial).flatMap((l) => l.types ?? []);
  const [events, setEvents] = useState<Feature[] | null>(null);
  const [eventTotal, setEventTotal] = useState(0); // matches before the list limit
  const [upcoming, setUpcoming] = useState<Feature[]>([]); // scheduled future events
  const [nonspatial, setNonspatial] = useState<Nonspatial[]>([]);
  const [sources, setSources] = useState<Source[]>([]);
  const [message, setMessage] = useState<string | null>(null);

  const typeKey = eventTypes.join(",");
  const nonspatialKey = nonspatialTypes.join(",");
  const box = bbox ? [Math.max(-180, bbox[0]), Math.max(-90, bbox[1]), Math.min(180, bbox[2]), Math.min(90, bbox[3])].join(",") : null;

  useEffect(() => {
    if (!typeKey) {
      setEvents([]);
      return;
    }
    getJson<EventsResponse>(`/api/events${query({ types: typeKey, start: timeWindow.start, end: timeWindow.end, bbox: box, limit: 200, upcoming_days: UPCOMING_DAYS })}`)
      .then((data) => {
        setEvents(data.features.filter((f) => !f.properties.scheduled));
        setUpcoming(data.features.filter((f) => f.properties.scheduled));
        setEventTotal(data.total);
      })
      .catch(() => setEvents(null));
  }, [typeKey, timeWindow, box]);

  useEffect(() => {
    if (!nonspatialKey) {
      setNonspatial([]);
      return;
    }
    getJson<Nonspatial[]>(`/api/events/nonspatial${query({ types: nonspatialKey, start: timeWindow.start, end: timeWindow.end })}`)
      .then(setNonspatial)
      .catch(() => setNonspatial([]));
  }, [nonspatialKey, timeWindow]);

  useEffect(() => {
    getJson<Source[]>("/api/sources").then((all) => setSources(all.filter((s) => s.category === group))).catch(() => setSources([]));
  }, [group]);

  async function refresh(sourceId: string) {
    try {
      const result = await postJson<{ job_id: number; mode: string }>(`/api/sources/${sourceId}/refresh`, {});
      setMessage(`Refresh queued (job ${result.job_id}, ${result.mode === "connected" ? "live download" : "air-gapped: re-reads last snapshot"}).`);
    } catch {
      setMessage("Refresh not queued: Lumon API unreachable.");
    }
  }

  return (
    <>
      <Section title="LAYERS">
        {groupLayers.length === 0 && <div className="empty-note">No layers in this category.</div>}
        {groupLayers.map((layer) => (
          <div key={layer.id} className={layer.available ? "layer-row" : "layer-row layer-row--unavailable"} title={layer.description}>
            <span className="layer-row__swatch" style={{ background: layer.available ? layer.color ?? "#7d8896" : "transparent" }} aria-hidden="true" />
            <span className="layer-row__name">{layer.name}<span className="layer-row__meta">{layer.available ? `${layer.count} records` : layer.status_text}</span></span>
            {layer.available ? (
              <LayerSwitch name={layer.name} on={layer.enabled} onToggle={() => onToggle(layer.id)} />
            ) : <span className="layer-row__status">N/A</span>}
          </div>
        ))}
      </Section>

      {eventTypes.length > 0 && (
        <Section title="IN VIEW" meta={events === null ? "—" : `${events.length < eventTotal ? `${events.length} of ${eventTotal}` : eventTotal} · ${timeWindow.label}`}>
          {events === null && <PanelState state="error" text="EVENTS UNAVAILABLE" />}
          {events && events.length === 0 && <div className="empty-note">NO EVENTS IN VIEW for this time window.</div>}
          {events?.slice(0, 60).map((feature) => (
            <ListButton key={feature.properties.id}
              title={<>{feature.properties.title}{feature.properties.stale && <span className="result-kind result-kind--stale">STALE</span>}</>}
              meta={`${formatTime(feature.properties.time)} · ${labelCase(feature.properties.type)}`}
              onClick={() => onSelect({ kind: "event", id: feature.properties.id })} />
          ))}
          {events && eventTotal > 60 && <div className="empty-note">Showing 60 of {eventTotal} in view; all {eventTotal} are on the map.</div>}
        </Section>
      )}

      {upcoming.length > 0 && (
        <Section title="UPCOMING · SCHEDULED" meta={`${upcoming.length} · next ${UPCOMING_DAYS} d`}>
          <div className="empty-note">Planned, not observed. Dates are the provider's current schedule and may be estimates.</div>
          {upcoming.map((feature) => (
            <ListButton key={feature.properties.id}
              title={<><span className="result-kind result-kind--scheduled">SCHEDULED</span>{feature.properties.title}</>}
              meta={`planned ${formatTime(feature.properties.time)}${feature.properties.time_precision ? ` · date known to the ${feature.properties.time_precision.toLowerCase()}` : ""}`}
              onClick={() => onSelect({ kind: "event", id: feature.properties.id })} />
          ))}
        </Section>
      )}

      {nonspatialTypes.length > 0 && (
        <Section title="NATIONAL / GLOBAL" meta={`${nonspatial.length} · no map location`}>
          {nonspatial.length === 0 && <div className="empty-note">No records in this time window.</div>}
          {nonspatial.slice(0, 40).map((item) => (
            <ListButton key={item.id} title={item.title} meta={formatTime(item.start_time)} onClick={() => onSelect({ kind: "event", id: item.id })} />
          ))}
        </Section>
      )}

      <Section title="SOURCES">
        {sources.map((source) => (
          <div key={source.id} className="source-row">
            <div className="source-row__name">{source.name}</div>
            <div className="source-row__meta">
              <Freshness status={source.freshness_status} label={source.freshness_label} /> · {source.record_count} records · downloaded {formatTime(source.last_success)}
            </div>
            {source.adapter && <button type="button" className="text-button" onClick={() => refresh(source.id)}>Refresh</button>}
          </div>
        ))}
        {message && <div className="notice">{message}</div>}
      </Section>
    </>
  );
}
