import type { TimeWindow } from "../types";

// Time-window presets for the timeline. Each one ends "now".
export const WINDOW_PRESETS: { label: string; days: number }[] = [
  { label: "24H", days: 1 },
  { label: "7D", days: 7 },
  { label: "30D", days: 30 },
  { label: "90D", days: 90 },
  { label: "1Y", days: 365 },
  { label: "ALL", days: 365 * 12 },
];

// ISO string without milliseconds: "2026-10-04T08:15:00Z".
export function toIso(date: Date): string {
  return date.toISOString().slice(0, 19) + "Z";
}

// A window covering the last `days` days up to now.
export function windowForDays(days: number, label: string): TimeWindow {
  const end = new Date();
  const start = new Date(end.getTime() - days * 86_400_000);
  return { start: toIso(start), end: toIso(end), label };
}

// Every calendar day ("YYYY-MM-DD") from start to end, inclusive.
// Used to draw the timeline's density bars.
export function daysBetween(startIso: string, endIso: string): string[] {
  const days: string[] = [];
  const cursor = new Date(startIso.slice(0, 10) + "T00:00:00Z");
  const last = new Date(endIso.slice(0, 10) + "T00:00:00Z");
  // A safety limit keeps very long windows from creating huge arrays.
  while (cursor <= last && days.length < 5000) {
    days.push(cursor.toISOString().slice(0, 10));
    cursor.setUTCDate(cursor.getUTCDate() + 1);
  }
  return days;
}
