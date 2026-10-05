// Small formatting helpers so every panel shows values the same way.

// "2026-10-04T08:15:00Z" -> "2026-10-04 08:15Z". Returns "—" for empty values.
export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  if (iso.length <= 10) return iso; // a plain date
  return `${iso.slice(0, 10)} ${iso.slice(11, 16)}Z`;
}

// Just the date part: "2026-10-04".
export function formatDate(iso: string | null | undefined): string {
  return iso ? iso.slice(0, 10) : "—";
}

// Metres -> "850 m" or "12.4 km".
export function formatDistance(metres: number | null | undefined): string {
  if (metres === null || metres === undefined) return "—";
  return metres < 1000 ? `${Math.round(metres)} m` : `${(metres / 1000).toFixed(1)} km`;
}

// Square metres -> hectares, e.g. 60000 -> "6.0 ha".
export function formatArea(squareMetres: number | null | undefined): string {
  if (squareMetres === null || squareMetres === undefined) return "—";
  return `${(squareMetres / 10000).toFixed(1)} ha`;
}

// Bytes -> "12.3 MB".
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

// How long ago an ISO time was, e.g. "3 h ago". Used for data age.
export function formatAge(iso: string | null | undefined): string {
  if (!iso) return "—";
  const hours = (Date.now() - Date.parse(iso)) / 3_600_000;
  if (Number.isNaN(hours)) return "—";
  if (hours < 1) return `${Math.max(1, Math.round(hours * 60))} min ago`;
  if (hours < 48) return `${Math.round(hours)} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}

// "fire-detection" -> "FIRE DETECTION" for labels.
export function labelCase(text: string | null | undefined): string {
  return text ? text.replace(/[-_]/g, " ").toUpperCase() : "—";
}
