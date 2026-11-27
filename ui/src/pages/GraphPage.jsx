import { useEffect, useRef, useState } from "react";
import { graphExpand, graphRoots } from "../api.js";

// Ported from frontend/static/graph.html. vis-network is the copy the server already serves at /lib.
const GROUP_COLORS = {
  SourceSystem: "#032d42",
  TargetSystem: "#2f8a22",
  Table: "#276c97",
  Column: "#81b5a1",
  Property: "#8c9ba5",
};

function loadVis() {
  if (window.vis) return Promise.resolve(window.vis);
  if (!document.querySelector('link[href="/lib/vis-network.css"]')) {
    const css = document.createElement("link");
    css.rel = "stylesheet";
    css.href = "/lib/vis-network.css";
    document.head.appendChild(css);
  }
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "/lib/vis-network.min.js";
    s.onload = () => resolve(window.vis);
    s.onerror = () => reject(new Error("Could not load /lib/vis-network.min.js"));
    document.body.appendChild(s);
  });
}

const recolor = (n) => ({ ...n, color: GROUP_COLORS[n.group] || n.color, font: { color: "#1d2b33" } });

export default function GraphPage() {
  const box = useRef(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let net;
    let cancelled = false;
    loadVis().then(async (vis) => {
      if (cancelled) return;
      const nodes = new vis.DataSet();
      const edges = new vis.DataSet();
      const opened = new Set();
      const kids = {};
      const add = (g) => {
        g.nodes.forEach((n) => { if (!nodes.get(n.id)) nodes.add(recolor(n)); });
        g.edges.forEach((e) => { const id = `${e.from}>${e.label}>${e.to}`; if (!edges.get(id)) edges.add({ ...e, id }); });
      };
      net = new vis.Network(box.current, { nodes, edges }, {
        nodes: { shape: "dot", size: 18 },
        edges: { arrows: "to", color: { color: "#9fb0ba" }, font: { size: 9, color: "#6b7b84", strokeWidth: 0 }, smooth: { type: "dynamic" } },
        physics: { barnesHut: { springLength: 110 } },
      });
      net.on("click", async (p) => {
        if (!p.nodes.length) return;
        const id = p.nodes[0];
        if (opened.has(id)) return;
        opened.add(id);
        try {
          const g = await graphExpand(id);
          kids[id] = g.nodes.map((n) => n.id);
          add(g);
        } catch (e) { setError(e.message); }
      });
      net.on("doubleClick", (p) => {
        if (!p.nodes.length) return;
        const id = p.nodes[0];
        (function drop(i) {
          (kids[i] || []).forEach((k) => {
            drop(k);
            opened.delete(k);
            if (nodes.get(k) && !edges.get({ filter: (e) => e.to === k && e.from !== i }).length) nodes.remove(k);
          });
          delete kids[i];
        })(id);
        opened.delete(id);
      });
      add(await graphRoots());
    }).catch((e) => setError(`Could not load the graph from Neo4j (${e.message}). Check that Neo4j is running and loaded.`));
    return () => { cancelled = true; net?.destroy(); };
  }, []);

  return (
    <div className="graph-page">
      <div className="graph-bar">
        <span className="muted small">
          Click a node to open what is inside it (system → tables → columns → data type). Double-click to collapse.
        </span>
        <div className="spacer" style={{ flex: 1 }} />
        <div className="legend">
          {Object.entries(GROUP_COLORS).map(([k, c]) => <span key={k}><i style={{ background: c }} />{k}</span>)}
        </div>
      </div>
      {error && <div className="container" style={{ paddingBottom: 0 }}><div className="alert error">{error}</div></div>}
      <div id="net" ref={box} />
    </div>
  );
}
