// Small helpers for showing positions to the analyst.

// Formats a latitude as text, e.g. 22.5 -> "22.5000° N", -3.2 -> "3.2000° S".
export function formatLatitude(latitude: number): string {
  const hemisphere = latitude >= 0 ? "N" : "S";
  return `${Math.abs(latitude).toFixed(4)}° ${hemisphere}`;
}

// Formats a longitude as text, e.g. 79 -> "79.0000° E", -10 -> "10.0000° W".
export function formatLongitude(longitude: number): string {
  const hemisphere = longitude >= 0 ? "E" : "W";
  return `${Math.abs(longitude).toFixed(4)}° ${hemisphere}`;
}

// Formats a map bearing (rotation) as a three-digit compass heading,
// e.g. 0 -> "000°", -45 -> "315°". MapLibre reports bearings from -180 to 180,
// while analysts expect 0-359, so we convert negative values.
export function formatBearing(bearing: number): string {
  const heading = Math.round((bearing + 360) % 360);
  return `${String(heading).padStart(3, "0")}°`;
}
