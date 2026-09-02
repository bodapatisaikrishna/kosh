// Thin client for Kosh's FastAPI layer (../../../api/main.py). No data-fetching
// library needed - see Next.js's own client-side-data-fetching guide: "Many
// apps can provide responsive interactions without a client data-fetching
// library" - this dashboard's fetches are each triggered by one user action
// (submit the run form, pick a historical benchmark), never revalidated in
// the background, so plain fetch + useState is the right-sized tool.

export const API_BASE = "http://localhost:8000";

async function getJSON(path) {
  const response = await fetch(`${API_BASE}${path}`);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `${response.status} ${response.statusText}`);
  }
  return response.json();
}

export async function runLive({ engine, records, seed, months }) {
  const response = await fetch(`${API_BASE}/api/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ engine, records, seed, months }),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `${response.status} ${response.statusText}`);
  }
  return response.json();
}

export function listBenchmarks() {
  return getJSON("/api/benchmarks");
}

export function getBenchmark(name) {
  return getJSON(`/api/benchmarks/${name}`);
}

// A trace_file value from exceptions_detail (e.g. "sample_traces_live/pay_x.json")
// is served directly by api/main.py's StaticFiles mounts at the API's own
// origin - never re-derived or path-joined on the client.
export function traceUrl(traceFile) {
  return `${API_BASE}/${traceFile}`;
}
