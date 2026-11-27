import { useEffect, useMemo, useState } from "react";
import { decideBatch, fmtDate, getBatch } from "../api.js";
import GraphPreview from "./GraphPreview.jsx";

const ORIGINS = {
  schema_mapping: "LLM schema mapping",
  "svc-ai-matcher": "AI matcher",
  "on-demand-ai": "Impact report",
  "svc-impact-report": "Impact report",
  "svc-mapping-discovery": "Mapping discovery",
  feedback: "User feedback",
};
export const originLabel = (o) => ORIGINS[o] || o || "—";

export const StatusChip = ({ status }) => <span className={`chip ${status}`}>{status}</span>;

export function Confidence({ value }) {
  const pct = Math.round((value || 0) * 100);
  return (
    <div className="conf" title={`${pct}% confidence`}>
      <div className="bar"><i className={value >= 0.85 ? "hi" : value < 0.7 ? "lo" : ""} style={{ width: `${pct}%` }} /></div>
      <b>{pct}%</b>
    </div>
  );
}

export function Mapping({ text }) {
  const [from, to] = (text || "").split(" -> ");
  return <span className="mono">{from}<span className="arrow">→</span>{to}</span>;
}

function DecisionSummary({ batch }) {
  // one line per (reviewer, time, comment) - a batch decision is saved per row in review_decision
  const groups = new Map();
  batch.mappings.filter((m) => m.decided_by).forEach((m) => {
    const k = `${m.decided_by}|${m.decided_at}|${m.comment || ""}`;
    if (!groups.has(k)) groups.set(k, { ...m, approved: 0, rejected: 0 });
    groups.get(k)[m.status === "approved" ? "approved" : "rejected"] += 1;
  });
  return (
    <>
      {[...groups.values()].map((g) => (
        <div key={`${g.decided_by}${g.decided_at}`} className="decision-summary">
          <div><b>{g.decided_by}</b> <span className="muted">· {fmtDate(g.decided_at)}</span></div>
          <div className="small">{g.approved} approved · {g.rejected} rejected</div>
          <div className="comment">{g.comment ? `“${g.comment}”` : <span className="muted">No comment</span>}</div>
        </div>
      ))}
      <div className="small muted" style={{ marginTop: 8 }}>
        Saved in PostgreSQL (review_decision). Approved mappings are available to the graph team at
        <code> GET /api/schema-mapping/approved</code>.
      </div>
    </>
  );
}

const savedReviewer = () => { try { return localStorage.getItem("reviewer") || ""; } catch { return ""; } };

export default function BatchDetail({ batchId, onDecided }) {
  const [batch, setBatch] = useState(null);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [selected, setSelected] = useState(new Set());
  const [reviewer, setReviewer] = useState(savedReviewer);
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);

  const openIds = useMemo(() => (batch?.mappings || []).filter((m) => m.status === "pending").map((m) => m.review_id), [batch]);

  async function load() {
    setError(null);
    try {
      const b = await getBatch(batchId);
      setBatch(b);
      setSelected(new Set(b.mappings.filter((m) => m.status === "pending").map((m) => m.review_id)));
    } catch (e) { setError(e.message); }
  }

  useEffect(() => { setBatch(null); setNotice(null); setComment(""); load(); }, [batchId]); // eslint-disable-line react-hooks/exhaustive-deps

  const toggle = (id) => setSelected((prev) => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n; });

  async function decide(decision) {
    if (!reviewer.trim()) { setError("Enter your name as the reviewer."); return; }
    if (decision === "approved" && selected.size === 0) { setError("Tick at least one mapping to approve, or reject the batch."); return; }
    setBusy(true);
    setError(null);
    try { localStorage.setItem("reviewer", reviewer.trim()); } catch { /* private mode */ }
    try {
      const r = await decideBatch(batchId, {
        decision, decidedBy: reviewer.trim(), comment,
        acceptedReviewIds: decision === "approved" ? [...selected] : null,
      });
      setNotice(decision === "approved"
        ? `Approved ${r.approved_review_ids.length} mapping(s)${r.rejected_review_ids.length ? `, rejected ${r.rejected_review_ids.length}` : ""}.`
        : `Rejected ${r.rejected_review_ids.length} mapping(s).`);
      await load();
      onDecided?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  if (!batch) {
    return <div className="card"><div className="card-b">{error ? <div className="alert error">{error}</div> : <span className="muted">Loading…</span>}</div></div>;
  }

  const allOn = openIds.length > 0 && openIds.every((id) => selected.has(id));
  return (
    <div className="stack">
    <div className="card">
      <div className="card-h">
        <div style={{ flex: 1, minWidth: 0 }}>
          <h3>{batch.source_object_id || batch.mappings[0].source_field_key} <span className="muted" style={{ fontWeight: 400 }}>→ {batch.mappings.length} mapping(s)</span></h3>
          <div className="meta" style={{ marginTop: 4 }}>
            <span>From <b>{originLabel(batch.origin)}</b></span>
            <span>Requested by <b>{batch.requested_by || "—"}</b></span>
            <span>Created <b>{fmtDate(batch.created_at)}</b></span>
            <span>Source version <b>{batch.source_object_version ?? "—"}</b></span>
            <span className="mono small" title="batch id">{batch.batch_id}</span>
          </div>
        </div>
        <StatusChip status={batch.status} />
      </div>

      {notice && <div className="card-b" style={{ paddingBottom: 0 }}><div className="alert success">{notice}</div></div>}

      <div style={{ overflowX: "auto" }}>
        <table className="grid">
          <thead>
            <tr>
              <th style={{ width: 34 }}>
                {openIds.length > 0 && (
                  <input type="checkbox" checked={allOn} aria-label="Select all"
                         onChange={(e) => setSelected(new Set(e.target.checked ? openIds : []))} />
                )}
              </th>
              <th>Mapping (source → target)</th>
              <th>Target table</th>
              <th>Confidence</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {batch.mappings.map((m) => {
              const open = m.status === "pending";
              return (
                <tr key={m.review_id} className={open ? `click ${selected.has(m.review_id) ? "sel" : ""}` : ""}
                    onClick={open ? () => toggle(m.review_id) : undefined}>
                  <td>{open && <input type="checkbox" checked={selected.has(m.review_id)} onChange={() => toggle(m.review_id)} onClick={(e) => e.stopPropagation()} />}</td>
                  <td className="mapping-cell">
                    <div className="m"><Mapping text={m.mapping} /></div>
                    <div className="r">{m.reason}</div>
                    {m.transformation && <code className="tx">{m.transformation}</code>}
                  </td>
                  <td>
                    <div>{m.target_object_id || "—"}</div>
                    <div className="small muted">{m.source_data_type || "?"} → {m.target_data_type || "?"}</div>
                  </td>
                  <td><Confidence value={m.confidence} /></td>
                  <td>
                    <StatusChip status={m.status} />
                    {m.decided_by && <div className="small muted" style={{ marginTop: 4 }}>{m.decided_by}, {fmtDate(m.decided_at)}</div>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

    </div>

    {batch.graph_update && <GraphPreview batch={batch} selected={selected} />}

    <div className="card">
      <div className="card-h"><h3>{openIds.length > 0 ? "Your review" : "Decision"}</h3></div>
      {error && <div className="card-b" style={{ paddingBottom: 0 }}><div className="alert error">{error}</div></div>}

      {openIds.length > 0 ? (
        <div className="decision">
          <label>Reviewer
            <input type="text" value={reviewer} onChange={(e) => setReviewer(e.target.value)} placeholder="Your name" />
          </label>
          <label>Comment (optional)
            <input type="text" value={comment} onChange={(e) => setComment(e.target.value)} placeholder="Why approve / reject" />
          </label>
          <div className="actions">
            <button className="btn danger" disabled={busy} onClick={() => decide("rejected")}>Reject all</button>
            <button className="btn approve" disabled={busy || selected.size === 0} onClick={() => decide("approved")}>
              Approve selected ({selected.size}/{openIds.length})
            </button>
          </div>
        </div>
      ) : (
        <div className="card-b">
          <DecisionSummary batch={batch} />
        </div>
      )}
    </div>
    </div>
  );
}
