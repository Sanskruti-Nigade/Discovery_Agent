import { useMemo, useState } from "react";

// Preview of a batch in the shared Neo4j model (graph dev's):
// SourceSystem -OWNS-> Table -HAS_COLUMN-> Column -MAPS_TO-> Column <-HAS_COLUMN- Table <-OWNS- TargetSystem.
// The server builds it (mapping/service.build_graph_update); this view follows the reviewer's ticks.

const NODE_W = 180;
const NODE_H = 44;
const COL_GAP = 48;
const ROW_GAP = 12;
const PAD = 20;

export const LABEL_STYLE = {
  SourceSystem: "#032d42",
  TargetSystem: "#2f8a22",
  Table: "#276c97",
  Column: "#5c8f7c",
};

/** Keep the MAPS_TO rows that are selected (pending) or approved (decided), plus the nodes they need. */
export function filterUpdate(update, keep) {
  const maps = update.relationships.filter((r) => r.type === "MAPS_TO" && keep(r.properties.review_id));
  const cols = new Set(maps.flatMap((r) => [r.from.id, r.to.id]));
  const hasCol = update.relationships.filter((r) => r.type === "HAS_COLUMN" && cols.has(r.to.id));
  const tables = new Set(hasCol.map((r) => r.from.id));
  const owns = update.relationships.filter((r) => r.type === "OWNS" && tables.has(r.to.id));
  const systems = new Set(owns.map((r) => r.from.id));
  const used = new Set([...cols, ...tables, ...systems]);
  return { ...update, nodes: update.nodes.filter((n) => used.has(n.id)), relationships: [...owns, ...hasCol, ...maps] };
}

const truncate = (s, n) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);

function useLayout(update) {
  return useMemo(() => {
    const byId = new Map(update.nodes.map((n) => [n.id, n]));
    const ownerOf = new Map(update.relationships.filter((r) => r.type === "OWNS").map((r) => [r.to.id, r.from.label]));
    const tableOf = new Map(update.relationships.filter((r) => r.type === "HAS_COLUMN").map((r) => [r.to.id, r.from.id]));
    const isSource = (n) => {
      if (n.label === "SourceSystem") return true;
      if (n.label === "TargetSystem") return false;
      const table = n.label === "Table" ? n.id : tableOf.get(n.id);
      return ownerOf.get(table) === "SourceSystem";
    };
    const col = (n) => {
      const side = isSource(n);
      if (n.label.endsWith("System")) return side ? 0 : 5;
      if (n.label === "Table") return side ? 1 : 4;
      return side ? 2 : 3;
    };
    const cols = [[], [], [], [], [], []];
    update.nodes.forEach((n) => cols[col(n)].push(n));
    cols[2].sort((a, b) => a.id.localeCompare(b.id));
    const order = new Map(cols[2].map((n, i) => [n.id, i]));
    const nb = (n) => update.relationships.filter((r) => r.to.id === n.id || r.from.id === n.id)
      .map((r) => (r.to.id === n.id ? r.from.id : r.to.id));
    for (const c of [1, 0, 3, 4, 5]) {
      const bary = (n) => {
        const ys = nb(n).map((id) => order.get(id)).filter((v) => v !== undefined);
        return ys.length ? ys.reduce((a, b) => a + b, 0) / ys.length : 0;
      };
      cols[c].sort((a, b) => bary(a) - bary(b) || a.id.localeCompare(b.id));
      cols[c].forEach((n, i) => order.set(n.id, i));
    }
    const used = cols.map((c, i) => (c.length ? i : null)).filter((i) => i !== null);
    const tallest = Math.max(1, ...cols.map((c) => c.length));
    const height = PAD * 2 + tallest * NODE_H + (tallest - 1) * ROW_GAP;
    const width = PAD * 2 + used.length * NODE_W + (used.length - 1) * COL_GAP;
    const pos = new Map();
    used.forEach((c, slot) => {
      const h = cols[c].length * NODE_H + (cols[c].length - 1) * ROW_GAP;
      const y0 = (height - h) / 2;
      cols[c].forEach((n, i) => pos.set(n.id, { x: PAD + slot * (NODE_W + COL_GAP), y: y0 + i * (NODE_H + ROW_GAP) }));
    });
    const edges = update.relationships.map((r, i) => ({ key: `${r.type}-${i}`, type: r.type, from: r.from.id, to: r.to.id,
      isNew: r.type === "MAPS_TO" }));
    return { nodes: [...byId.values()], edges, pos, width, height };
  }, [update]);
}

function edgePath(a, b) {
  const ltr = a.x <= b.x;
  const sx = ltr ? a.x + NODE_W : a.x;
  const ex = ltr ? b.x : b.x + NODE_W;
  const sy = a.y + NODE_H / 2;
  const ey = b.y + NODE_H / 2;
  const dx = Math.max(24, Math.abs(ex - sx) / 2) * (ltr ? 1 : -1);
  return `M ${sx} ${sy} C ${sx + dx} ${sy}, ${ex - dx} ${ey}, ${ex} ${ey}`;
}

function Diagram({ update }) {
  const { nodes, edges, pos, width, height } = useLayout(update);
  const [hover, setHover] = useState(null);
  const [selected, setSelected] = useState(null);
  const focus = hover || selected;
  const touches = (e) => focus && (e.from === focus || e.to === focus);
  const sel = nodes.find((n) => n.id === selected);
  if (!nodes.length) return <div className="empty">No mapping selected, so nothing would be added to the graph.</div>;
  return (
    <div className="diagram">
      <div className="diagram-scroll">
        <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Mapping graph">
          <defs>
            {["new", "old", "dim"].map((k) => (
              <marker key={k} id={`gp-arrow-${k}`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                <path d="M0,0 L10,5 L0,10 z" className={`arrowhead ${k}`} />
              </marker>
            ))}
          </defs>
          {edges.map((e) => {
            const a = pos.get(e.from);
            const b = pos.get(e.to);
            if (!a || !b) return null;
            const dim = focus && !touches(e);
            return (
              <path key={e.key} d={edgePath(a, b)} className={`edge ${e.isNew ? "new" : "old"} ${dim ? "dim" : ""}`}
                    markerEnd={`url(#gp-arrow-${dim ? "dim" : e.isNew ? "new" : "old"})`} />
            );
          })}
          {nodes.map((n) => {
            const p = pos.get(n.id);
            const color = LABEL_STYLE[n.label] || "#5f6d75";
            const dim = focus && focus !== n.id && !edges.some((e) => touches(e) && (e.from === n.id || e.to === n.id));
            const name = n.label === "Column" ? `${n.properties.name}${n.properties.data_type ? ` : ${n.properties.data_type}` : ""}` : n.properties.name;
            return (
              <g key={n.id} transform={`translate(${p.x},${p.y})`} className={`gnode ${dim ? "dim" : ""} ${selected === n.id ? "selected" : ""}`}
                 onMouseEnter={() => setHover(n.id)} onMouseLeave={() => setHover(null)} onClick={() => setSelected(n.id === selected ? null : n.id)}>
                <title>{n.id}</title>
                <rect width={NODE_W} height={NODE_H} rx="8" fill="#fff" stroke={color} />
                <rect width="5" height={NODE_H} rx="2" fill={color} />
                <text x="13" y="17" className="gnode-label" fill={color}>{n.label.toUpperCase()}</text>
                <text x="13" y="34" className="gnode-name">{truncate(name || n.id, 24)}</text>
              </g>
            );
          })}
        </svg>
      </div>
      <div className="diagram-legend">
        <span><i className="swatch new" /> MAPS_TO: the mapping under review</span>
        <span><i className="swatch old" /> OWNS / HAS_COLUMN: existing structure (created only if missing)</span>
        <span className="muted">Hover to trace · click a node for details</span>
      </div>
      {sel && (
        <div className="node-detail">
          <div className="node-detail-head">
            <span className="tag" style={{ color: LABEL_STYLE[sel.label] }}>{sel.label}</span>
            <code>{sel.id}</code>
          </div>
          <dl>
            {Object.entries(sel.properties).map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{String(v)}</dd></div>)}
          </dl>
        </div>
      )}
    </div>
  );
}

function NodesTable({ update }) {
  return (
    <table className="grid">
      <thead><tr><th>Label</th><th>ID</th><th>Properties</th><th>Write</th></tr></thead>
      <tbody>
        {update.nodes.map((n) => (
          <tr key={n.id}>
            <td><b style={{ color: LABEL_STYLE[n.label] }}>{n.label}</b></td>
            <td><code>{n.id}</code></td>
            <td className="small">{Object.entries(n.properties).map(([k, v]) => `${k}=${v}`).join(", ")}</td>
            <td className="small muted">if missing</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function RelationshipsTable({ update }) {
  return (
    <table className="grid">
      <thead><tr><th>From</th><th>Relationship</th><th>To</th><th>Properties</th></tr></thead>
      <tbody>
        {update.relationships.map((r, i) => (
          <tr key={i}>
            <td><code>{r.from.id}</code></td>
            <td><span className={`rel ${r.type === "MAPS_TO" ? "new" : ""}`}>{r.type}</span></td>
            <td><code>{r.to.id}</code></td>
            <td className="small">
              {Object.entries(r.properties).filter(([k]) => !["batch_id", "reason"].includes(k)).map(([k, v]) => `${k}=${v}`).join(", ") || <span className="muted">only if missing</span>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function GraphPreview({ batch, selected }) {
  const pending = batch.mappings.some((m) => m.status === "pending");
  const keepIds = useMemo(() => new Set(pending
    ? [...selected]
    : batch.mappings.filter((m) => m.status === "approved").map((m) => m.review_id)), [batch, selected, pending]);
  const update = useMemo(() => filterUpdate(batch.graph_update, (id) => keepIds.has(id)), [batch, keepIds]);
  const chosen = batch.mappings.filter((m) => keepIds.has(m.review_id)).map((m) => m.mapping);
  const [tab, setTab] = useState("diagram");
  const tabs = [
    ["diagram", "Diagram"],
    ["mapping", "Mapping JSON"],
    ["nodes", `Nodes (${update.nodes.length})`],
    ["rels", `Relationships (${update.relationships.length})`],
    ["json", "Graph JSON"],
  ];
  return (
    <div className="card graph-preview">
      <div className="card-h" style={{ flexWrap: "wrap" }}>
        <div style={{ flex: 1, minWidth: 260 }}>
          <h3>{pending ? "Proposed lineage" : "Approved lineage"}</h3>
          <div className="small muted">
            {pending
              ? "Follows the ticked mappings. Approving saves the decision in PostgreSQL; the graph team loads approved mappings into Neo4j."
              : "Approved mappings, as the graph team will load them into Neo4j (GET /api/schema-mapping/approved)."}
          </div>
        </div>
        <div className="seg">
          {tabs.map(([k, label]) => <button key={k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}>{label}</button>)}
        </div>
      </div>
      <div className="card-b">
        {tab === "diagram" && <Diagram update={update} />}
        {tab === "mapping" && <pre className="json">{JSON.stringify({ mapping: chosen }, null, 2)}</pre>}
        {tab === "nodes" && <div style={{ overflowX: "auto" }}><NodesTable update={update} /></div>}
        {tab === "rels" && <div style={{ overflowX: "auto" }}><RelationshipsTable update={update} /></div>}
        {tab === "json" && <pre className="json">{JSON.stringify(update, null, 2)}</pre>}
      </div>
    </div>
  );
}
