// Unit tests for the small frontend helpers.
// Run with: npm run test:ui  (Node's built-in test runner; no extra packages)

import { test } from "node:test";
import assert from "node:assert/strict";
import { formatArea, formatBytes, formatDistance, formatTime, labelCase } from "./format.ts";
import { formatBearing, formatLatitude, formatLongitude } from "./coordinates.ts";
import { daysBetween } from "./time.ts";
import { buildGraticule } from "./graticule.ts";
import { applyLayerChoices, mapLayerIdsFor, toggleLayerChoice, visibilityFor } from "./layerVisibility.ts";
import { readFileSync } from "node:fs";

test("formatting never invents values", () => {
  assert.equal(formatTime(null), "—");
  assert.equal(formatTime("2026-10-04T08:15:30Z"), "2026-10-04 08:15Z");
  assert.equal(formatDistance(850), "850 m");
  assert.equal(formatDistance(12400), "12.4 km");
  assert.equal(formatArea(60000), "6.0 ha");
  assert.equal(formatBytes(1536), "1.5 KB");
  assert.equal(labelCase("fire-detection"), "FIRE DETECTION");
});

test("coordinates use hemispheres and 0-359 bearings", () => {
  assert.equal(formatLatitude(-3.2), "3.2000° S");
  assert.equal(formatLongitude(79), "79.0000° E");
  assert.equal(formatBearing(-45), "315°");
});

test("daysBetween is inclusive", () => {
  assert.deepEqual(daysBetween("2026-02-27T10:00:00Z", "2026-03-01T00:00:00Z"), ["2026-02-27", "2026-02-28", "2026-03-01"]);
});

test("graticule includes the Equator and Tropic of Cancer as reference lines", () => {
  const grid = buildGraticule(10, 2);
  const reference = grid.features.filter((f) => f.properties.kind === "reference");
  const latitudes = new Set(reference.map((f) => f.geometry.coordinates[0][1]));
  assert.ok(latitudes.has(0));
  assert.ok(latitudes.has(23.44));
});

test("frontend event kinds match the backend's event_kinds.py", async () => {
  const { readFileSync } = await import("node:fs");
  const { TELEMETRY_TYPES, SCHEDULED_TYPES } = await import("../data/eventKinds.ts");
  const python = readFileSync(new URL("../../server/lumon/event_kinds.py", import.meta.url), "utf8");
  // Read the Python set literal, e.g. TELEMETRY_TYPES = {"a", "b"}.
  const pythonSet = (name: string) =>
    [...(python.match(new RegExp(`^${name} = \\{([^}]*)\\}`, "m"))?.[1] ?? "").matchAll(/"([^"]+)"/g)].map((m) => m[1]).sort();
  assert.deepEqual([...TELEMETRY_TYPES].sort(), pythonSet("TELEMETRY_TYPES"));
  assert.deepEqual([...SCHEDULED_TYPES].sort(), pythonSet("SCHEDULED_TYPES"));
});

// ----- Layer ON/OFF -----------------------------------------------------------
// TEST FIXTURES: three layers, one not staged.
const LAYERS = [
  { id: "india-boundary", available: true, enabled: true },
  { id: "districts", available: true, enabled: false },
  { id: "vessels", available: false, enabled: false },
];

test("toggling flips a layer from its default and back", () => {
  let choices = toggleLayerChoice({}, LAYERS, "india-boundary");
  assert.equal(applyLayerChoices(LAYERS, choices)[0].enabled, false);
  choices = toggleLayerChoice(choices, LAYERS, "india-boundary");
  assert.equal(applyLayerChoices(LAYERS, choices)[0].enabled, true);
  choices = toggleLayerChoice(choices, LAYERS, "districts");
  assert.equal(applyLayerChoices(LAYERS, choices)[1].enabled, true);
  // Toggling one layer never changes another.
  assert.equal(applyLayerChoices(LAYERS, choices)[0].enabled, true);
});

test("an unstaged layer can never be switched on", () => {
  const choices = toggleLayerChoice({}, LAYERS, "vessels");
  const vessels = applyLayerChoices(LAYERS, choices)[2];
  assert.equal(vessels.enabled, false);
  assert.equal(visibilityFor(vessels), "none");
});

test("map visibility follows the switch", () => {
  const [boundaryOn] = applyLayerChoices(LAYERS, {});
  const [boundaryOff] = applyLayerChoices(LAYERS, { "india-boundary": false });
  assert.equal(visibilityFor(boundaryOn), "visible");
  assert.equal(visibilityFor(boundaryOff), "none");
});

test("every drawable non-event layer maps to real map layer ids", () => {
  const config = JSON.parse(readFileSync(new URL("../../config/layers.json", import.meta.url), "utf8"));
  for (const layer of config.layers) {
    const ids = mapLayerIdsFor(layer);
    if (["boundary", "reference", "pilot", "analysis"].includes(layer.kind) || layer.id === "aoi-footprints") {
      assert.ok(ids.length > 0, `${layer.id} has no map layer ids, so its switch would do nothing`);
    }
  }
});
