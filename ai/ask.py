"""Ask a question about the lineage graph.

question -> embedding -> vector search in Neo4j -> expand lineage around hits -> LLM answers from that context.

    python ai/ask.py "Where does customer email come from?" [--show-context]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from neo4j import GraphDatabase  # noqa: E402

from embeddings import embed  # noqa: E402
from index_graph import AUTH, DB, INDEX, URI  # noqa: E402
from llm_client import chat  # noqa: E402
from query_gen import Q, run  # noqa: E402

TOP_K = 8

SEARCH_Q = Q(
    "ask_vector_search",
    f"Vector similarity search: CALL db.index.vector.queryNodes('{INDEX}', $k, $vec) YIELD node, score. For each hit "
    "return its labels, its search_text property, the score, and col_id = elementId(node) when the node is a Column "
    "(otherwise null).",
    {"k": 8, "vec": [0.1, 0.2, 0.3]}, ["labels", "text", "score", "col_id"])

# Lineage up to 3 hops either way from the matched columns (the "graph" part of graph-RAG).
LINEAGE_Q = Q(
    "ask_lineage_around_columns",
    "For the Column nodes whose elementId is in $ids, find every MAPS_TO path of 1 to 3 hops ending at the column "
    "(upstream) and every MAPS_TO path of 1 to 3 hops starting at it (downstream). Return each DISTINCT MAPS_TO "
    "relationship on those paths once: src and dst as 'system.table.name' strings of its start and end Column, plus the "
    "relationship's mapping_name as mapping and transformation.",
    {"ids": ["4:x:1"]}, ["src", "dst", "mapping", "transformation"])

SYSTEM = ("You answer questions about a data-lineage graph (systems, tables, columns, MAPS_TO mappings). "
          "Use ONLY the provided context. If it does not contain the answer, say so. "
          "Cite columns as system.table.column and show lineage as A -> B.")


def retrieve(session, question: str, k: int = TOP_K) -> str:
    hits = run(session, SEARCH_Q, k=k, vec=embed([question])[0])
    ids = [h["col_id"] for h in hits if h["col_id"]]
    edges = run(session, LINEAGE_Q, ids=ids) if ids else []
    ctx = ["## Matching graph nodes (most relevant first)"]
    ctx += [f"- [{h['labels'][0]}, score {h['score']:.2f}] {h['text']}" for h in hits]
    if edges:
        ctx.append("\n## Lineage around those columns")
        ctx += [f"- {e['src']} -> {e['dst']}"
                + (f" [mapping: {e['mapping']}]" if e["mapping"] else "")
                + (f" [transformation: {e['transformation']}]" if e["transformation"] else "") for e in edges]
    return "\n".join(ctx)


def answer(question: str) -> tuple[str, str]:
    with GraphDatabase.driver(URI, auth=AUTH) as driver, driver.session(database=DB) as s:
        context = retrieve(s, question)
    return chat(SYSTEM, f"Context:\n{context}\n\nQuestion: {question}"), context


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit('usage: python ai/ask.py "your question" [--show-context]')
    ans, ctx = answer(args[0])
    if "--show-context" in sys.argv:
        print(ctx, "\n" + "-" * 60)
    print(ans)
