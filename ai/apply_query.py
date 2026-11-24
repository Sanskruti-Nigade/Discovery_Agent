"""Run a saved, generated Cypher file against Neo4j: dry-run, show changes, commit only if approved.
Usage:  python ai/apply_query.py ai/generated/query_xxx.cypher [--yes]"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase

from cypher_generator import validate

load_dotenv(Path(__file__).parent.parent / ".env")
URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
USER = os.getenv("NEO4J_USERNAME") or os.getenv("NEO4J_USER", "neo4j")
PASSWORD = os.getenv("NEO4J_PASSWORD", "")
DB = os.getenv("NEO4J_DATABASE", "neo4j")


def run_statements(tx, cypher: str, totals: dict) -> dict:
    """Run each ;-separated statement in tx and add up what changed (nodes/relationships/properties)."""
    for stmt in (x.strip() for x in cypher.split(";")):
        if stmt and not all(l.strip().startswith("//") for l in stmt.splitlines() if l.strip()):
            for k, v in vars(tx.run(stmt).consume().counters).items():
                if isinstance(v, int) and not isinstance(v, bool) and v:
                    totals[k] = totals.get(k, 0) + v
    return totals


def main(path: str, auto_yes: bool):
    cypher = Path(path).read_text(encoding="utf-8")
    validate(cypher)  # re-check: the file may have been edited after generation
    totals = {}
    with GraphDatabase.driver(URI, auth=(USER, PASSWORD)) as d, d.session(database=DB) as s:
        tx = s.begin_transaction()
        try:
            run_statements(tx, cypher, totals)
            print("Pending changes:", totals or "none")
            if auto_yes or input("Commit? [y/N] ").strip().lower() == "y":
                tx.commit(); print("Committed.")
            else:
                tx.rollback(); print("Rolled back, nothing changed.")
        except Exception:
            tx.rollback()
            raise


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python ai/apply_query.py <file.cypher> [--yes]")
    try:
        main(sys.argv[1], "--yes" in sys.argv)
    except ValueError as e:
        sys.exit(f"REJECTED: {e}")
