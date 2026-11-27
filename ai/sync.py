"""Load the JSON data, detect renames / type changes against the graph, have the LLM write the Cypher that
records them, and commit everything in ONE transaction after approval.
Usage:  python ai/sync.py [json file or folder ...] [--yes]   (default folder: lineage/json_files)
The files may be in ANY structure: the LLM normalizes them first."""
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "lineage"))  # reuse the lineage loader

from neo4j import GraphDatabase  # noqa: E402

import changes as chg  # noqa: E402
import cypher_generator  # noqa: E402
import loader  # noqa: E402
import normalizer  # noqa: E402
from apply_query import run_statements  # noqa: E402
from cypher_generator import OUT_DIR, generate, generate_changes  # noqa: E402


def generate_in_parts(docs, batch=40):
    """One LLM call per table, then per batch of mapping entries (one huge reply would be cut off)."""
    parts = []
    for d in docs:
        for side in ("source", "target"):
            head = {k: v for k, v in d[side].items() if k != "tables"}
            for table, cols in d[side]["tables"].items():
                print(f"  Cypher for {side} table {table} ({len(cols)} columns) ...")
                doc = {"source": {**d["source"], "tables": {}}, "target": {**d["target"], "tables": {}}, "mapping": []}
                doc[side] = {**head, "tables": {table: cols}}
                parts.append(generate(json.dumps(doc)))
        maps = d.get("mapping", [])
        for i in range(0, len(maps), batch):
            print(f"  Cypher for mappings {i + 1}-{min(i + batch, len(maps))} of {len(maps)} ...")
            doc = {"source": {k: v for k, v in d["source"].items() if k != "tables"},
                   "target": {k: v for k, v in d["target"].items() if k != "tables"}, "mapping": maps[i:i + batch]}
            parts.append(generate(json.dumps(doc)))
    return "\n".join(p.rstrip().rstrip(";") + ";" for p in parts)


def main(paths, auto_yes):
    # the LLM reads files in ANY structure and rewrites them into the canonical format
    built, docs = normalizer.normalize(paths)
    print(f"LLM normalized the files -> {normalizer.save(docs)}")
    with GraphDatabase.driver(loader.URI, auth=(loader.USER, loader.PASSWORD)) as d, \
            d.session(database=loader.DB) as s:
        found, unpaired = chg.diff(chg.snapshot(s), chg.new_state(built))

        def neo4j_accepts(cypher):
            """Ask Neo4j to EXPLAIN every statement (nothing runs): a syntax error goes back to the LLM to fix."""
            for stmt in (x.strip() for x in cypher.split(";")):
                if stmt and not all(l.strip().startswith("//") for l in stmt.splitlines() if l.strip()):
                    try:
                        s.run("EXPLAIN " + stmt).consume()
                    except Exception as e:  # noqa: BLE001
                        raise ValueError(f"Neo4j rejected this statement: {stmt[:300]} -> {e}") from e

        cypher_generator.CHECK = neo4j_accepts

        for c in found:
            what = f"{c['old']} -> {c['new']}"
            print(f"  {c['kind']:<11} {c['system']}.{c['table']}: {what}" +
                  (f"  (column {c['column']})" if c["kind"] == "type_change" else ""))
        for sysn, table, col in unpaired:
            print(f"  removed?    {sysn}.{table}.{col} is gone from the data (not recorded; old node kept)")
        print(f"{len(found)} change(s) detected." if found else "No renames or type changes detected.")

        OUT_DIR.mkdir(exist_ok=True)
        stamp = f"{datetime.now():%Y%m%d_%H%M%S}"

        # the LLM writes the Cypher that loads the data (no hardcoded queries)
        load_cypher = generate_in_parts(docs)
        out = OUT_DIR / f"load_{stamp}.cypher"
        out.write_text(load_cypher + "\n", encoding="utf-8")
        print(f"Generated load Cypher saved -> {out}")

        cypher = ""
        if found:
            cypher = generate_changes(found)
            out = OUT_DIR / f"changes_{stamp}.cypher"
            out.write_text(cypher, encoding="utf-8")
            print(f"Generated Cypher saved -> {out}")

        tx = s.begin_transaction()
        try:
            totals = {}
            run_statements(tx, load_cypher, totals)  # 1. load the new data (LLM Cypher, MERGE, idempotent)
            run_statements(tx, cypher, totals)  # 2. record the changes
            print("Committing." if auto_yes else "Ready.")
            if auto_yes or input("Commit data + changes? [y/N] ").strip().lower() == "y":
                tx.commit()
                print("Committed.")
            else:
                tx.rollback()
                print("Rolled back, nothing changed.")
        except Exception:
            tx.rollback()
            raise


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    try:
        main(args or [str(ROOT / "lineage" / "json_files")], "--yes" in sys.argv)
    except (loader.LineageError, ValueError, RuntimeError) as e:
        sys.exit(f"ERROR: {e}")
