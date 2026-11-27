"""Query Neo4j and write graph.html (pyvis): node colors by label, edge colors by type, legend."""
import sys
from pathlib import Path

from neo4j import GraphDatabase
from pyvis.network import Network

sys.path.insert(0, str(Path(__file__).parent.parent / "ai"))
from loader import DB, PASSWORD, URI, USER  # noqa: E402
from query_gen import Q, run  # noqa: E402

ALL_NODES = Q("viz_nodes", "Every node: id = elementId(n), l = labels(n), p = properties(n).", {}, ["id", "l", "p"])
ALL_EDGES = Q("viz_edges", "Every relationship between two nodes: a = elementId of start node, b = elementId of end "
              "node, t = type(r), p = properties(r).", {}, ["a", "b", "t", "p"])

NODE_COLORS = {"SourceSystem": "#1F77B4", "TargetSystem": "#2CA02C", "Table": "#FF7F0E",
               "Column": "#9467BD", "Property": "#7F7F7F", "Entity": "#E6550D", "Field": "#9467BD", "Subfield": "#C5B0D5", "Source": "#6BAED6", "Target": "#74C476",
               "SourceEntity": "#3182BD", "TargetEntity": "#31A354"}
EDGE_COLORS = {"OWNS": "#1F77B4", "HAS_TABLE": "#8C564B", "HAS_COLUMN": "#FF7F0E", "HAS_PROPERTY": "#7F7F7F", "MAPS_TO": "#D62728",
               "RENAMED_TO": "#E377C2", "CHANGED_TO": "#17BECF", "HAS_SOURCE_ENTITY": "#1F77B4", "HAS_TARGET_ENTITY": "#2CA02C", "HAS_SOURCE": "#1F77B4", "HAS_TARGET": "#2CA02C", "HAS_ENTITY": "#E6550D", "HAS_FIELD": "#FF7F0E",
               "VALUE_MAP": "#D62728", "LOOKUP": "#D62728", "DERIVED_FROM": "#D62728"}


PANEL = ('<div id="info" style="position:fixed;top:10px;left:10px;z-index:9;background:#fff;color:#000;'
         'border:1px solid #ccc;padding:10px;font:13px sans-serif;border-radius:6px;max-width:340px;'
         'max-height:70vh;overflow:auto"><b>Click a Source/Target system to expand it.</b></div>')

# Start with only the systems visible; clicking a node shows/hides its children and lists its details.
EXPAND_JS = """<script>
(function () {
  var SYSTEMS = ["SourceSystem", "TargetSystem"], LINKS = ["RENAMED_TO", "CHANGED_TO"];
  function active(id) { var n = nodes.get(id); return n && (n.status || "active") === "active"; }
  function children(id) {   // only the CURRENT (active) structure; old versions appear through history()
    return edges.get().filter(function (e) { return e.from === id && LINKS.indexOf(e.label) < 0 && (e.status || "active") === "active" && active(e.to); })
                      .map(function (e) { return e.to; });
  }
  function history(id) {    // old names, deleted children and removed mappings around this node
    var ns = [], es = [];
    edges.get().forEach(function (e) {
      if (e.to === id && e.label === "RENAMED_TO") { ns.push(e.from); es.push(e.id); }
      if (e.from === id && e.label === "RENAMED_TO") { ns.push(e.to); es.push(e.id); }
      if (e.from === id && LINKS.indexOf(e.label) < 0 && !active(e.to) && nodes.get(e.to).status === "deleted") { ns.push(e.to); es.push(e.id); }
      if ((e.from === id || e.to === id) && e.status === "removed") { ns.push(e.from, e.to); es.push(e.id); }
    });
    return {nodes: ns, edges: es};
  }
  function hideAll(id, seen) {
    seen = seen || {}; if (seen[id]) return; seen[id] = true;
    children(id).forEach(function (c) { nodes.update({id: c, hidden: true}); hideAll(c, seen); });
  }
  nodes.update(nodes.get().map(function (n) { return {id: n.id, hidden: SYSTEMS.indexOf(n.lab) < 0}; }));
  edges.update(edges.get().filter(function (e) { return e.status === "removed" || e.label === "RENAMED_TO"; })
                    .map(function (e) { return {id: e.id, hidden: true}; }));
  network.on("click", function (p) {
    if (!p.nodes.length) return;
    var id = p.nodes[0], n = nodes.get(id), kids = children(id), h = history(id);
    var open = kids.some(function (c) { return !nodes.get(c).hidden; });
    if (open) hideAll(id); else kids.forEach(function (c) { nodes.update({id: c, hidden: false}); });
    var shown = h.nodes.some(function (x) { return !nodes.get(x).hidden; }) && h.edges.some(function (x) { return !edges.get(x).hidden; });
    h.nodes.forEach(function (x) { if (x !== id) nodes.update({id: x, hidden: shown}); });
    h.edges.forEach(function (x) { edges.update({id: x, hidden: shown}); });
    document.getElementById("info").innerHTML = "<pre style='white-space:pre-wrap;margin:0'>" +
      String(n.title).replace(/</g, "&lt;") + "</pre><i>" + kids.length + " child node(s)" +
      (h.edges.length ? ", " + h.edges.length + " history link(s) " + (shown ? "hidden" : "shown") : "") + "</i>";
  });
})();
</script>"""


def tooltip(title, props):
    return "\n".join([title] + [f"{k}: {v}" for k, v in props.items()])


def node_label(labels, p):
    return f"{p['key']}={p['value']}" if "Property" in labels else p.get("name", "")


def swatch(color, w, h, radius):
    return (f'<span style="display:inline-block;width:{w}px;height:{h}px;background:{color};'
            f'border-radius:{radius};margin-right:6px;vertical-align:middle"></span>')


def main(out="graph.html"):
    net = Network(height="800px", width="100%", directed=True, bgcolor="#ffffff")
    with GraphDatabase.driver(URI, auth=(USER, PASSWORD)) as d, d.session(database=DB) as s:
        for r in run(s, ALL_NODES):
            lab = next((x for x in r["l"] if x in NODE_COLORS), r["l"][0])
            status = r["p"].get("status", "active")  # renamed / deleted nodes are the OLD versions: grey, shown on click
            old = status != "active"
            net.add_node(r["id"], label=node_label(r["l"], r["p"]) + (f" ({status})" if old else ""),
                         color="#BDBDBD" if old else NODE_COLORS.get(lab, "#999999"),
                         title=tooltip(lab, r["p"]), font={"color": "#000000"}, lab=lab, status=status)
        for r in run(s, ALL_EDGES):
            net.add_edge(r["a"], r["b"], label=r["t"], color=EDGE_COLORS.get(r["t"], "#999999"),
                         title=tooltip(r["t"], r["p"]), status=r["p"].get("status", "active"),
                         dashes=r["p"].get("status") == "removed", width=3 if r["t"] not in ("HAS_SOURCE_ENTITY", "HAS_TARGET_ENTITY", "HAS_SOURCE", "HAS_TARGET", "HAS_ENTITY", "HAS_FIELD", "OWNS", "HAS_TABLE", "HAS_COLUMN", "HAS_PROPERTY") else 1.5)
    net.write_html(out)

    legend = ('<div style="position:fixed;top:10px;right:10px;z-index:9;background:#fff;color:#000;'
              'border:1px solid #ccc;padding:10px;font:13px sans-serif;border-radius:6px"><b>Nodes</b><br>'
              + "".join(f"{swatch(c, 14, 14, '50%')}{k}<br>" for k, c in NODE_COLORS.items())
              + "<b>Edges</b><br>"
              + "".join(f"{swatch(c, 26, 4, '0')}{k}<br>" for k, c in EDGE_COLORS.items())
              + "</div>")
    p = Path(out)
    html = p.read_text(encoding="utf-8").replace("<body>", "<body>" + legend + PANEL, 1)
    p.write_text(html.replace("</body>", EXPAND_JS + "</body>", 1), encoding="utf-8")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
