import { useEffect, useState } from "react";
import { fmtDate, listBatches, listReviews } from "../api.js";
import { navigate } from "../router.jsx";
import BatchDetail, { Confidence, Mapping, StatusChip, originLabel } from "../components/BatchDetail.jsx";

const PAGE = 50;

function Kpis({ summary }) {
  const c = summary?.mappings || {};
  const items = [
    ["warn", summary?.pending_batches ?? "–", "Batches awaiting approval"],
    ["warn", c.pending ?? "–", "Mappings pending"],
    ["ok", c.approved ?? "–", "Mappings approved"],
    ["bad", c.rejected ?? "–", "Mappings rejected"],
  ];
  return (
    <div className="kpis">
      {items.map(([tone, n, l]) => <div key={l} className={`card kpi ${tone}`}><div className="n">{n}</div><div className="l">{l}</div></div>)}
    </div>
  );
}

function PendingQueue({ batchId, onChanged, version }) {
  const [batches, setBatches] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    listBatches("pending").then((r) => setBatches(r.items)).catch((e) => setError(e.message));
  }, [version]);

  const active = batchId && batches?.some((b) => b.batch_id === batchId) ? batchId : batchId || batches?.[0]?.batch_id;
  const open = (id) => navigate(`/reviews?tab=pending&batch=${id}`);

  if (error) return <div className="alert error">{error}</div>;
  if (!batches) return <div className="muted">Loading…</div>;
  if (!batches.length && !batchId) {
    return <div className="card empty">Nothing is waiting for approval. New LLM mappings appear here as soon as they are generated.</div>;
  }
  return (
    <div className="split">
      <div className="card">
        <div className="card-h"><h3>Awaiting approval</h3><span className="muted small">{batches.length} batch(es)</span></div>
        <div className="batch-list">
          {batches.map((b) => (
            <div key={b.batch_id} className={`batch-item ${b.batch_id === active ? "active" : ""}`} onClick={() => open(b.batch_id)}>
              <div className="t"><span>{b.source_object_id || "Unknown source"}</span><span className="chip pending">{b.counts.pending || 0} open</span></div>
              <div className="s">{originLabel(b.origin)}</div>
              <div className="s">{b.mapping_count} mapping(s) · avg {Math.round(b.avg_confidence * 100)}% · {b.target_object_ids.length} target table(s)</div>
              <div className="s">{b.requested_by ? `${b.requested_by} · ` : ""}{fmtDate(b.created_at)}</div>
            </div>
          ))}
          {!batches.length && <div className="empty small">No more pending batches.</div>}
        </div>
      </div>
      {active && (
        <BatchDetail batchId={active} onDecided={() => {
          if (!batchId) navigate(`/reviews?tab=pending&batch=${active}`);  // keep the decided batch on screen
          onChanged();
        }} />
      )}
    </div>
  );
}

function AllReviews({ batchId, version, onChanged }) {
  const [status, setStatus] = useState("all");
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => { const t = setTimeout(() => { setQuery(q); setOffset(0); }, 300); return () => clearTimeout(t); }, [q]);
  useEffect(() => {
    setError(null);
    listReviews({ status, q: query, limit: PAGE, offset }).then(setData).catch((e) => setError(e.message));
  }, [status, query, offset, version]);

  if (batchId) {
    return (
      <>
        <button className="btn sm" style={{ marginBottom: 12 }} onClick={() => navigate("/reviews?tab=all")}>← All reviews</button>
        <BatchDetail batchId={batchId} onDecided={onChanged} />
      </>
    );
  }
  return (
    <div className="card">
      <div className="filters">
        <div className="seg">
          {["all", "pending", "approved", "rejected"].map((s) => (
            <button key={s} className={status === s ? "on" : ""} onClick={() => { setStatus(s); setOffset(0); }}>
              {s[0].toUpperCase() + s.slice(1)}
            </button>
          ))}
        </div>
        <input type="search" placeholder="Search mapping, source or target table" value={q} onChange={(e) => setQ(e.target.value)} style={{ minWidth: 300 }} />
        <span className="muted small" style={{ marginLeft: "auto" }}>{data ? `${data.total} review(s)` : ""}</span>
      </div>
      {error && <div className="card-b"><div className="alert error">{error}</div></div>}
      <div style={{ overflowX: "auto" }}>
        <table className="grid">
          <thead>
            <tr><th>#</th><th>Mapping (source → target)</th><th>Source</th><th>From</th><th>Confidence</th><th>Status</th><th>Created</th><th>Decided</th></tr>
          </thead>
          <tbody>
            {data?.items.map((r) => (
              <tr key={r.review_id} className="click" onClick={() => navigate(`/reviews?tab=all&batch=${r.batch_id}`)} title="Open batch">
                <td className="muted">{r.review_id}</td>
                <td className="mapping-cell"><div className="m"><Mapping text={r.mapping} /></div><div className="r">{r.target_object_id || ""}</div></td>
                <td>{r.source_object_id || "—"}</td>
                <td className="small">{originLabel(r.origin)}</td>
                <td><Confidence value={r.confidence} /></td>
                <td><StatusChip status={r.status} /></td>
                <td className="small">{fmtDate(r.created_at)}<div className="muted">{r.requested_by || ""}</div></td>
                <td className="small">{r.decided_by ? <>{fmtDate(r.decided_at)}<div className="muted">{r.decided_by}</div></> : <span className="muted">—</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {data && !data.items.length && <div className="empty">No reviews match.</div>}
      </div>
      {data && data.total > PAGE && (
        <div className="pager">
          <span>{offset + 1}–{Math.min(offset + PAGE, data.total)} of {data.total}</span>
          <span style={{ display: "flex", gap: 8 }}>
            <button className="btn sm" disabled={offset === 0} onClick={() => setOffset(offset - PAGE)}>Previous</button>
            <button className="btn sm" disabled={offset + PAGE >= data.total} onClick={() => setOffset(offset + PAGE)}>Next</button>
          </span>
        </div>
      )}
    </div>
  );
}

export default function ReviewsPage({ search, summary, onChanged }) {
  const tab = search.get("tab") === "all" ? "all" : "pending";
  const batchId = search.get("batch");
  const [version, setVersion] = useState(0);
  const changed = () => { setVersion((v) => v + 1); onChanged?.(); };

  return (
    <div className="container">
      <h1 className="page-title">Mapping reviews</h1>
      <div className="muted">Proposed source → target field mappings from every discovery job (LLM schema mapping, AI matcher, impact reports, user feedback). Nothing is used downstream until a reviewer approves it.</div>
      <Kpis summary={summary} />
      <div className="tabs">
        <button className={tab === "pending" ? "active" : ""} onClick={() => navigate("/reviews")}>
          Needs approval <span className="count">{summary?.pending_batches ?? 0}</span>
        </button>
        <button className={tab === "all" ? "active" : ""} onClick={() => navigate("/reviews?tab=all")}>
          All reviews <span className="count">{summary?.total ?? 0}</span>
        </button>
      </div>
      {tab === "pending"
        ? <PendingQueue batchId={batchId} version={version} onChanged={changed} />
        : <AllReviews batchId={batchId} version={version} onChanged={changed} />}
    </div>
  );
}
