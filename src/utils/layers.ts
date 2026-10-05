import type { Layer } from "../types";

// The event types that are currently drawn on the map: switched-on event
// layers that have a location (national/global records such as internet
// outages have no map position). The map AND the timeline both use this,
// so the timeline's main band always counts exactly what the map shows.
export function visibleEventTypes(layers: Layer[]): string {
  return layers
    .filter((layer) => layer.kind === "events" && layer.enabled && !layer.nonspatial)
    .flatMap((layer) => layer.types ?? [])
    .join(",");
}

// How far ahead the UPCOMING window looks for scheduled events (launches).
// 90 days, because launch dates are often only known to the month.
export const UPCOMING_DAYS = 90;
