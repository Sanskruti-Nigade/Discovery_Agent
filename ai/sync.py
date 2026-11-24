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
import loader  # noqa: E402
import normalizer  # noqa: E402
from apply_query import run_statements  # noqa: E402
from cypher_generator import OUT_DIR, generate, generate_changes  # noqa: E402


def main(paths, auto_yes):
    # the LLM reads files in ANY structure and rewrites them into the canonical format
    built, docs = normalizer.normalize(paths)
    print(f"LLM normalized the files -> {normalizer.save(docs)}")
    with GraphDatabase.driver(loader.URI, auth=(loader.USER, loader.PASSWORD)) as d, \
            d.session(database=loader.DB) as s:
        found, unpaired = chg.diff(chg.snapshot(s), chg.new_state(built))

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
        load_cypher = generate(json.dumps(docs, indent=2))
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
