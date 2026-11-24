"""Embed the graph's Column/Table nodes and store the vectors in Neo4j (vector index).

Run after loading lineage:  python ai/index_graph.py        (re-run any time; it re-embeds everything)
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase

sys.path.insert(0, str(Path(__file__).parent))
from embeddings import embed  # noqa: E402
from query_gen import Q, run  # noqa: E402

load_dotenv(Path(__file__).parent.parent / ".env")
URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
AUTH = (os.getenv("NEO4J_USERNAME") or os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", ""))
DB = os.getenv("NEO4J_DATABASE", "neo4j")
INDEX = "lineage_embeddings"

# One searchable text block per node, including its properties and direct lineage (queries are LLM-written).
COLUMN_Q = Q(
    "index_columns",
    "One row per Column node: id = elementId(c), system, tbl = its table property, name, dt = its data_type, "
    "props = list of DISTINCT 'key=value' strings of its HAS_PROPERTY Properties, outs = list of DISTINCT "
    "'system.table.name' of the Columns it MAPS_TO, with ' (transformation)' appended when the relationship has a "
    "transformation, ins = list of DISTINCT 'system.table.name' of the Columns that MAP_TO it. Lists must be empty "
    "(never contain null) when there is nothing.",
    {}, ["id", "system", "tbl", "name", "dt", "props", "outs", "ins"])
TABLE_Q = Q(
    "index_tables",
    "One row per Table node: id = elementId(t), system, name, cols = list of the names of its HAS_COLUMN Columns "
    "(empty list if none, never null entries).",
    {}, ["id", "system", "name", "cols"])


def make_index_q(dim: int) -> Q:
    return Q(
        "index_create_vector_index",
        f"Create the vector index named {INDEX} IF NOT EXISTS on nodes labelled Embeddable, property embedding, with "
        f"vector.dimensions = {dim} and vector.similarity_function = 'cosine'.",
        {}, [], admin=True)


STORE_Q = Q(
    "index_store_embeddings",
    "$rows is a list of maps {id, text, vec}. For each, MATCH the node whose elementId equals id, add the label "
    "Embeddable, set its search_text property to text, and store vec as its 'embedding' vector property using "
    "db.create.setNodeVectorProperty.",
    {"rows": [{"id": "4:x:1", "text": "t", "vec": [0.1, 0.2]}]}, [], admin=True)
WAIT_Q = Q("index_wait", "Wait up to 60 seconds for all indexes to come online (db.awaitIndexes).", {}, [], admin=True)


def column_text(r) -> str:
    s = f"Column {r['name']} of table {r['tbl']} in system {r['system']}."
    if r["dt"]:
        s += f" Type {r['dt']}."
    if r["props"]:
        s += " Properties: " + ", ".join(r["props"]) + "."
    if r["outs"]:
        s += " Maps to: " + "; ".join(r["outs"]) + "."
    if r["ins"]:
        s += " Mapped from: " + "; ".join(r["ins"]) + "."
    return s


def table_text(r) -> str:
    return f"Table {r['name']} in system {r['system']} with columns: {', '.join(r['cols'])}."


def main():
    with GraphDatabase.driver(URI, auth=AUTH) as driver, driver.session(database=DB) as s:
        items = [(r["id"], column_text(r)) for r in run(s, COLUMN_Q)]
        items += [(r["id"], table_text(r)) for r in run(s, TABLE_Q)]
        if not items:
            sys.exit("No Column/Table nodes found - load the lineage first.")
        vecs = embed([t for _, t in items])
        dim = len(vecs[0])
        rows = [{"id": i, "text": t, "vec": v} for (i, t), v in zip(items, vecs)]
        run(s, make_index_q(dim))   # index setup + embedding writes are LLM-written too (admin statements)
        run(s, STORE_Q, rows=rows)
        run(s, WAIT_Q)
    print(f"Embedded {len(items)} nodes (dim={dim}) into index '{INDEX}'.")


if __name__ == "__main__":
    main()
