// Thin client for frontend/app.py (same origin in production; proxied by Vite in dev).
export class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
    this.status = status;
  }
}

export async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  } catch {
    throw new ApiError(0, "Cannot reach the Discovery Agent server (uvicorn app:app --port 8080).");
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = Array.isArray(body.detail)
      ? body.detail.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ")
      : body.detail || response.statusText;
    throw new ApiError(response.status, detail);
  }
  return body;
}

// chat + graph (graph dev)
export const health = () => request("/api/health");
export const ask = (question) => request("/api/ask", { method: "POST", body: JSON.stringify({ question }) });
export const graphRoots = () => request("/api/graph/roots");
export const graphExpand = (id) => request(`/api/graph/expand?id=${encodeURIComponent(id)}`);

// schema-mapping review queue (AI engineer). Empty base = same server (frontend/app.py mounts the router);
// set VITE_MAPPING_API_BASE=http://host:8001 when mapping/ runs as its own service.
const SM = `${(import.meta.env.VITE_MAPPING_API_BASE || "").replace(/\/$/, "")}/api/schema-mapping`;
const qs = (params) => {
  const s = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== ""));
  return s.toString() ? `?${s}` : "";
};
export const reviewSummary = () => request(`${SM}/summary`);
// PostgreSQL, Neo4j, LLM key (mapping/health.py)
export const systemHealth = () => request(`${SM}/health`);
export const listBatches = (status) => request(`${SM}/batches${qs({ status, limit: 200 })}`);
export const getBatch = (id) => request(`${SM}/batches/${encodeURIComponent(id)}`);
export const listReviews = ({ status, q, limit, offset }) => request(`${SM}/reviews${qs({ status, q, limit, offset })}`);
export const decideBatch = (id, { decision, decidedBy, comment, acceptedReviewIds }) =>
  request(`${SM}/batches/${encodeURIComponent(id)}/decision`, {
    method: "POST",
    body: JSON.stringify({ decision, decided_by: decidedBy, comment: comment || null, accepted_review_ids: acceptedReviewIds ?? null }),
  });

export const fmtDate = (iso) => {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
};
