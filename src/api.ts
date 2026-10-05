// ---------------------------------------------------------------------------
// Talking to the local Lumon API.
//
// The browser ONLY talks to our own backend under /api (Vite forwards it to
// the FastAPI server in development; in production the FastAPI server serves
// both). It never calls third-party services, so the user interface keeps
// working in air-gapped mode as long as the local backend runs.
// ---------------------------------------------------------------------------

// Thrown when the request fails. `offline` is true when the backend itself
// could not be reached (as opposed to the backend answering with an error).
export class ApiError extends Error {
  status: number;
  offline: boolean;
  detail: unknown; // the backend's structured "detail", when it is not plain text
  constructor(message: string, status: number, offline: boolean, detail: unknown = null) {
    super(message);
    this.status = status;
    this.offline = offline;
    this.detail = detail;
  }
}

// Shared logic for GET and POST: send the request, turn failures into ApiError.
async function request<T>(path: string, options?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, options);
  } catch {
    // fetch only throws when no response arrived at all.
    throw new ApiError("Lumon API unreachable", 0, true);
  }
  if (!response.ok) {
    // The backend explains errors as {"detail": "..."}; show that text.
    let detail: unknown = response.statusText;
    try {
      const body = await response.json();
      detail = body.detail ?? detail;
    } catch {
      // the body was not JSON; keep the status text
    }
    // A proxy error (502/504) also means the backend is not running.
    const message = typeof detail === "string" ? detail : JSON.stringify(detail);
    throw new ApiError(message, response.status, response.status === 502 || response.status === 504, detail);
  }
  return (await response.json()) as T;
}

// GET a JSON document, e.g. getJson<SystemInfo>("/api/system").
export function getJson<T>(path: string): Promise<T> {
  return request<T>(path);
}

// POST a JSON body and read the JSON answer.
export function postJson<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// Build a query string, skipping empty values: query({a: 1, b: null}) -> "?a=1".
export function query(params: Record<string, string | number | boolean | null | undefined>): string {
  const parts: string[] = [];
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === "") continue;
    parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`);
  }
  return parts.length ? `?${parts.join("&")}` : "";
}
