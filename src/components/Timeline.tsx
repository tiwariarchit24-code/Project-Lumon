import { useEffect, useRef, useState } from "react";
import { getJson, query } from "../api";
import { daysBetween, WINDOW_PRESETS } from "../utils/time";
import type { TimeWindow } from "../types";
import InfoTip from "./InfoTip";
import { INFO } from "../data/infoNotes";

// ---------------------------------------------------------------------------
// The bottom timeline.
//
// - bars:      the events the MAP shows, per time bin (same types, viewport
//              and window as the map - so the two always agree)
// - telemetry: a separate thin band for aircraft / satellite / model weather
//              / Kp records and national signals (internet outages). They are
//              snapshots of a moment, so they must not swamp the event bars,
//              but they are kept visible here
// - amber:     dates of staged satellite observations
// - orange:    earliest-supported dates of change events
// - presets:   24H / 7D / 30D / 90D / 1Y / ALL set the time window
// - UPCOMING:  scheduled future events (launches), counted separately
// - play / previous / next: move a playhead through the window; the map
//   then shows only events up to the playhead
//
// Nothing is interpolated: an empty bin means no staged records in it.
// ---------------------------------------------------------------------------
type TimelineData = {
  density: Record<string, Record<string, number>>;
  telemetry: Record<string, number>;
  upcoming: { id: string; title: string; start_time: string }[];
  scenes: string[];
  changes: { id: string; change_class: string; day: string }[];
};

type TimelineProps = {
  window: TimeWindow;
  cursor: string | null; // playhead day "YYYY-MM-DD", or null = whole window
  onPreset: (days: number, label: string) => void;
  onCursor: (day: string | null) => void;
  refreshKey: number;
  types: string; // event types visible on the map (comma-separated)
  bbox: number[] | null; // map viewport
  upcomingDays: number; // 0 = UPCOMING window off
  onToggleUpcoming: () => void;
};

const MAX_BINS = 160;

export default function Timeline({ window: timeWindow, cursor, onPreset, onCursor, refreshKey, types, bbox, upcomingDays, onToggleUpcoming }: TimelineProps) {
  const [data, setData] = useState<TimelineData | null>(null);
  const [error, setError] = useState(false);
  const [playing, setPlaying] = useState(false);
  const cursorRef = useRef(cursor);
  cursorRef.current = cursor;

  // The viewport, clamped like the map's own request (globe views can pass +/-180).
  const box = bbox ? [Math.max(-180, bbox[0]), Math.max(-90, bbox[1]), Math.min(180, bbox[2]), Math.min(90, bbox[3])].join(",") : null;

  useEffect(() => {
    getJson<TimelineData>(`/api/timeline${query({ start: timeWindow.start, end: timeWindow.end, types: types || "none", bbox: box, upcoming_days: upcomingDays || null })}`)
      .then((result) => {
        setData(result);
        setError(false);
      })
      .catch(() => setError(true));
  }, [timeWindow, refreshKey, types, box, upcomingDays]);

  // Group the window's days into at most MAX_BINS bins so long windows
  // (years) still draw quickly.
  const days = daysBetween(timeWindow.start, timeWindow.end);
  const perBin = Math.max(1, Math.ceil(days.length / MAX_BINS));
  const bins: { first: string; last: string; count: number; telemetry: number; scenes: number; changes: number }[] = [];
  for (let i = 0; i < days.length; i += perBin) {
    const slice = days.slice(i, i + perBin);
    let count = 0;
    let telemetry = 0;
    for (const day of slice) {
      for (const n of Object.values(data?.density[day] ?? {})) count += n;
      telemetry += data?.telemetry?.[day] ?? 0;
    }
    bins.push({
      first: slice[0], last: slice[slice.length - 1], count, telemetry,
      scenes: data ? data.scenes.filter((d) => d >= slice[0] && d <= slice[slice.length - 1]).length : 0,
      changes: data ? data.changes.filter((c) => c.day >= slice[0] && c.day <= slice[slice.length - 1]).length : 0,
    });
  }
  const maxCount = Math.max(1, ...bins.map((b) => b.count));
  const maxTelemetry = Math.max(1, ...bins.map((b) => b.telemetry));
  const eventTotal = bins.reduce((sum, b) => sum + b.count, 0);
  const telemetryTotal = bins.reduce((sum, b) => sum + b.telemetry, 0);
  const activeDays = bins.filter((b) => b.count > 0).map((b) => b.last);

  // Playback: advance the playhead one bin every 400 ms.
  useEffect(() => {
    if (!playing) return;
    const timer = window.setInterval(() => {
      const current = cursorRef.current;
      const index = current ? bins.findIndex((b) => b.last >= current) : -1;
      const next = bins[index + 1];
      if (!next) {
        setPlaying(false);
        return;
      }
      onCursor(next.last);
    }, 400);
    return () => window.clearInterval(timer);
  }, [playing, bins, onCursor]);

  // Jump to the previous/next bin that has events.
  function step(direction: -1 | 1) {
    setPlaying(false);
    const current = cursor ?? (direction === 1 ? "" : "9999");
    const candidates = direction === 1 ? activeDays.filter((d) => d > current) : activeDays.filter((d) => d < current).reverse();
    onCursor(candidates[0] ?? null);
  }

  const cursorIndex = cursor ? bins.findIndex((b) => b.last >= cursor) : -1;

  return (
    <div className="timeline" aria-label="Timeline">
      <div className="timeline__controls">
        <button type="button" className="timeline__button" onClick={() => step(-1)} aria-label="Previous event day" title="Previous">‹</button>
        <button type="button" className="timeline__button" onClick={() => {
          if (!playing && !cursor && bins.length) onCursor(bins[0].last);
          setPlaying(!playing);
        }} aria-label={playing ? "Pause" : "Play"} title={playing ? "Pause" : "Play"}>{playing ? "❚❚" : "▶"}</button>
        <button type="button" className="timeline__button" onClick={() => step(1)} aria-label="Next event day" title="Next">›</button>
      </div>

      <div className="timeline__presets" role="group" aria-label="Time window">
        {WINDOW_PRESETS.map((preset) => (
          <button key={preset.label} type="button"
            className={timeWindow.label === preset.label ? "timeline__preset timeline__preset--active" : "timeline__preset"}
            onClick={() => {
              setPlaying(false);
              onCursor(null);
              onPreset(preset.days, preset.label);
            }}>
            {preset.label}
          </button>
        ))}
      </div>

      <div className="timeline__track" onMouseLeave={() => undefined}>
        {error && <div className="timeline__empty">TIMELINE UNAVAILABLE — Lumon API unreachable</div>}
        {!error && (
          <div className="timeline__bars">
            {bins.map((bin, index) => (
              <button
                key={bin.first}
                type="button"
                className={[
                  "timeline__bin",
                  cursorIndex >= 0 && index > cursorIndex ? "timeline__bin--future" : "",
                  index === cursorIndex ? "timeline__bin--cursor" : "",
                ].join(" ")}
                onClick={() => onCursor(bin.last)}
                title={`${bin.first}${bin.first !== bin.last ? ` – ${bin.last}` : ""}: ${bin.count} events on map${bin.telemetry ? `, ${bin.telemetry} telemetry/national records` : ""}${bin.scenes ? `, ${bin.scenes} satellite scenes` : ""}${bin.changes ? `, ${bin.changes} change events first supported` : ""}`}
              >
                <span className="timeline__bar" style={{ height: `${bin.count ? 12 + 76 * Math.sqrt(bin.count / maxCount) : 0}%` }} />
                {/* Separate thin telemetry band under the event bars. */}
                <span className="timeline__telemetry" style={{ opacity: bin.telemetry ? 0.35 + 0.65 * Math.sqrt(bin.telemetry / maxTelemetry) : 0 }} />
                {bin.scenes > 0 && <span className="timeline__scene" />}
                {bin.changes > 0 && <span className="timeline__change" />}
              </button>
            ))}
          </div>
        )}
        <div className="timeline__axis">
          <span>{timeWindow.start.slice(0, 10)}</span>
          <span>{cursor ? `PLAYHEAD ${cursor}` : data && eventTotal === 0 ? (types ? "NO MAP EVENTS IN WINDOW" : "NO EVENT LAYERS ON") : ""}</span>
          <span>{timeWindow.end.slice(0, 10)}</span>
        </div>
      </div>

      <div className="timeline__legend">
        <span><i className="legend-bar" />EVENTS {eventTotal}
          <InfoTip {...INFO.timeline} state={`${eventTotal} map events from ${timeWindow.start.slice(0, 10)} to ${timeWindow.end.slice(0, 10)}${cursor ? `, playhead at ${cursor}` : ""}`} />
        </span>
        <span><i className="legend-telemetry" />TELEMETRY {telemetryTotal}
          <InfoTip {...INFO.telemetry} state={`${telemetryTotal} telemetry / national records in this window`} />
        </span>
        <span><i className="legend-scene" />SCENES {data?.scenes.length ?? 0}</span>
        <span><i className="legend-change" />CHANGES {data?.changes.length ?? 0}</span>
        <button
          type="button"
          className={upcomingDays ? "timeline__upcoming timeline__upcoming--on" : "timeline__upcoming"}
          onClick={onToggleUpcoming}
          aria-pressed={upcomingDays > 0}
          title={upcomingDays ? (data?.upcoming ?? []).map((u) => `${u.start_time.slice(0, 10)} ${u.title}`).join("\n") || "No scheduled events" : "Show scheduled future events"}
        >
          <i className="legend-upcoming" />UPCOMING {upcomingDays ? `${data?.upcoming.length ?? 0} · ${upcomingDays} D` : "OFF"}
        </button>
        <InfoTip {...INFO.upcoming} state={upcomingDays ? `${data?.upcoming.length ?? 0} scheduled in the next ${upcomingDays} days in this view` : "Upcoming window is off"} />
        {data && data.scenes.length === 0 && <span className="timeline__note">NO IMAGERY STAGED IN WINDOW</span>}
        {cursor && <button type="button" className="text-button" onClick={() => onCursor(null)}>Clear playhead</button>}
      </div>
    </div>
  );
}
