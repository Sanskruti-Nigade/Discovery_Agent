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
               "Column": "#9467BD", "Property": "#7F7F7F"}
EDGE_COLORS = {"OWNS": "#1F77B4", "HAS_COLUMN": "#FF7F0E", "HAS_PROPERTY": "#7F7F7F", "MAPS_TO": "#D62728",
               "RENAMED_TO": "#E377C2", "CHANGED_TO": "#17BECF"}


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
            net.add_node(r["id"], label=node_label(r["l"], r["p"]), color=NODE_COLORS.get(lab, "#999999"),
                         title=tooltip(lab, r["p"]), font={"color": "#000000"})
        for r in run(s, ALL_EDGES):
            net.add_edge(r["a"], r["b"], label=r["t"], color=EDGE_COLORS.get(r["t"], "#999999"),
                         title=tooltip(r["t"], r["p"]), width=3 if r["t"] == "MAPS_TO" else 1.5)
    net.write_html(out)

    legend = ('<div style="position:fixed;top:10px;right:10px;z-index:9;background:#fff;color:#000;'
              'border:1px solid #ccc;padding:10px;font:13px sans-serif;border-radius:6px"><b>Nodes</b><br>'
              + "".join(f"{swatch(c, 14, 14, '50%')}{k}<br>" for k, c in NODE_COLORS.items())
              + "<b>Edges</b><br>"
              + "".join(f"{swatch(c, 26, 4, '0')}{k}<br>" for k, c in EDGE_COLORS.items())
              + "</div>")
    p = Path(out)
    p.write_text(p.read_text(encoding="utf-8").replace("<body>", "<body>" + legend, 1), encoding="utf-8")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
