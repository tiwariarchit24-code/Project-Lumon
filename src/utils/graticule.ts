// ---------------------------------------------------------------------------
// Graticule (latitude / longitude grid) builder.
//
// A graticule is the familiar grid of lines of equal latitude and longitude.
// We compute it in the browser instead of downloading map tiles, so the map
// workspace works with no internet connection and no external provider.
// ---------------------------------------------------------------------------

// A minimal GeoJSON line feature. We write the type out by hand to keep this
// file free of extra dependencies.
type GridLine = {
  type: "Feature";
  properties: { kind: "major" | "minor" | "reference" };
  geometry: { type: "LineString"; coordinates: number[][] };
};

export type GridCollection = {
  type: "FeatureCollection";
  features: GridLine[];
};

// Web maps cannot show the poles in the flat (Mercator) projection,
// so latitude lines stop at roughly +/- 85 degrees.
const MAX_LATITUDE = 85;

// The Tropic of Cancer runs through central India, so we draw it as a
// helpful reference line together with the Equator.
const TROPIC_OF_CANCER = 23.44;

// Builds one line of constant longitude (running north-south).
// We add a point every 5 degrees so the line curves correctly on the globe.
function meridian(longitude: number, kind: GridLine["properties"]["kind"]): GridLine {
  const coordinates: number[][] = [];
  for (let latitude = -MAX_LATITUDE; latitude <= MAX_LATITUDE; latitude += 5) {
    coordinates.push([longitude, latitude]);
  }
  return { type: "Feature", properties: { kind }, geometry: { type: "LineString", coordinates } };
}

// Builds one line of constant latitude (running east-west).
//
// The globe view does not draw a single line that runs the whole way from
// -180 to +180 degrees, so we split each latitude line into four 90-degree
// pieces. Each piece has a point every 5 degrees so it curves correctly on
// the globe.
function parallel(latitude: number, kind: GridLine["properties"]["kind"]): GridLine[] {
  const pieces: GridLine[] = [];
  for (let start = -180; start < 180; start += 90) {
    const coordinates: number[][] = [];
    for (let longitude = start; longitude <= start + 90; longitude += 5) {
      coordinates.push([longitude, latitude]);
    }
    pieces.push({ type: "Feature", properties: { kind }, geometry: { type: "LineString", coordinates } });
  }
  return pieces;
}

// Builds the whole grid.
//  - stepDegrees: spacing of the minor (thin) lines, e.g. 2.5
//  - majorEvery:  every Nth minor line is drawn as a major (brighter) line
// Returns a GeoJSON FeatureCollection that MapLibre can draw directly.
export function buildGraticule(stepDegrees: number, majorEvery: number): GridCollection {
  const features: GridLine[] = [];

  // Lines of longitude from -180 to +180.
  let index = 0;
  for (let longitude = -180; longitude <= 180; longitude += stepDegrees) {
    const kind = index % majorEvery === 0 ? "major" : "minor";
    features.push(meridian(longitude, kind));
    index += 1;
  }

  // Lines of latitude. We start at 0 (the Equator) and walk outwards in both
  // directions so that the Equator is always a major line.
  index = 1;
  for (let latitude = stepDegrees; latitude <= MAX_LATITUDE; latitude += stepDegrees) {
    const kind = index % majorEvery === 0 ? "major" : "minor";
    features.push(...parallel(latitude, kind));
    features.push(...parallel(-latitude, kind));
    index += 1;
  }

  // Reference lines drawn on top in a distinct colour.
  features.push(...parallel(0, "reference"));
  features.push(...parallel(TROPIC_OF_CANCER, "reference"));

  return { type: "FeatureCollection", features };
}
