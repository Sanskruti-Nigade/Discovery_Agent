"""Ask the LLM to turn ANY input data into Cypher for the lineage graph, validate it, and save it
to ai/generated/ for review.   Usage:  python ai/cypher_generator.py <data file> [<data file> ...]"""
import re
import sys
from datetime import datetime
from pathlib import Path

from llm_client import chat

OUT_DIR = Path(__file__).parent / "generated"
LABELS = {"SourceSystem", "TargetSystem", "Table", "Column", "Property"}
REL_TYPES = {"OWNS", "HAS_TABLE", "HAS_COLUMN", "HAS_PROPERTY", "MAPS_TO", "RENAMED_TO", "CHANGED_TO"}
FORBIDDEN = re.compile(r"\b(DELETE|DETACH|REMOVE|DROP|CALL|LOAD|FOREACH|CREATE|APOC|DBMS)\b", re.I)  # MERGE only

SYSTEM_PROMPT = """You convert arbitrary source/target/mapping metadata into Cypher for a data-lineage graph.

GRAPH MODEL (do not change it):
Nodes: (:SourceSystem {name,type}) (:TargetSystem {name,type}) (:Table {system,name,schema})
       (:Column {system,table,name,data_type}) (:Property {key,value})
Edges: (System)-[:OWNS]->(Table)  (Table)-[:HAS_COLUMN]->(Column)  (Column)-[:HAS_PROPERTY]->(Property)
       (Table)-[:HAS_TABLE]->(Table)  only for NESTED tables: a parent table points to the child object inside it.
       The system OWNS only top-level tables; nested tables are reached through HAS_TABLE. A nested Table may also
       carry a `path` property (its dotted path from the root).
       (Column)-[:MAPS_TO {mapping_name,transformation,created_at}]->(Column)   lineage flows source -> target.

RULES:
- Output ONLY Cypher, statements separated by semicolons. No prose, no markdown fences.
- Use MERGE only (never CREATE/DELETE/REMOVE/DROP/CALL). Re-running must not duplicate anything.
- MERGE nodes on their identity keys: System(name), Table(system,name), Column(system,table,name), Property(key,value).
- Put non-identity properties in SET. A MAPS_TO edge is MERGEd on (from, to, mapping_name).
- Every MAPS_TO endpoint must be a Column declared in the data. If a mapping references a table or column that
  does not exist in the data, output exactly one line: // ERROR: <what is missing>  and nothing else.
- For every Column that has a data type, also set Column.data_type AND MERGE a (:Property {key:'data_type', value:<type>})
  linked with (Column)-[:HAS_PROPERTY]->(Property), so the type is visible in the graph. If a column's type is not
  stated in the data, assume 'string' only when the values are all strings.
- Do not invent data. Fix nothing silently: typos must trigger the // ERROR line.
- Keep the output SHORT: use UNWIND over a list of map literals (one UNWIND per table or per group of rows) instead of
  one statement per column/edge. Never use CREATE, including "ON CREATE"; use SET with coalesce() for created_at.
"""


CHANGE_PROMPT = """You record schema changes in a data-lineage graph. Input: a JSON list of detected changes, each
{kind: 'rename'|'type_change', system, table, column, old, new}. The new Column nodes already exist in the graph.

Output ONLY Cypher (statements separated by semicolons, no prose, no fences), MERGE only, using the graph model above.
Record every change as a RELATIONSHIP between existing nodes, never as an extra node:
- 'rename': find the existing old Column and new Column (identified by system, table and name; they must already
  exist, so look them up rather than creating them) and link old to new with a RENAMED_TO relationship, stamped with
  the time it was detected (an ISO-8601 string), keeping the original time if the relationship already exists.
- 'type_change': a data type is a Property node identified by its key (always 'data_type') and its value. Find or
  create the Property for the old value and the one for the new value, and link old to new with a CHANGED_TO
  relationship that records system, table and column (as relationship properties), stamped with the detection time
  the same way.
- Never use CREATE, DELETE, REMOVE, DROP, CALL. Do not touch anything not listed in the input.
"""


def generate_changes(changes: list[dict]) -> str:
    """Cypher recording the detected changes, written by the LLM (no templates). Needs a real provider."""
    import json
    return generate_with(CHANGE_PROMPT, json.dumps(changes, indent=2))


def strip_fences(text: str) -> str:
    return re.sub(r"^```[a-zA-Z]*\n|\n?```\s*$", "", text.strip())


def validate(cypher: str) -> None:
    """Raise ValueError unless the Cypher is MERGE-only and uses only the lineage model."""
    if cypher.lstrip().startswith("// ERROR"):
        raise ValueError(cypher.strip())
    if FORBIDDEN.search(cypher):
        raise ValueError(f"forbidden keyword: {FORBIDDEN.search(cypher).group(0)}")
    for label in re.findall(r"\(\s*\w*\s*:\s*(\w+)", cypher):
        if label not in LABELS:
            raise ValueError(f"unknown node label: {label}")
    for rel in re.findall(r"\[\s*\w*\s*:\s*(\w+)", cypher):
        if rel not in REL_TYPES:
            raise ValueError(f"unknown relationship type: {rel}")
    if "MERGE" not in cypher.upper():
        raise ValueError("no MERGE statement generated")


def generate(data_text: str, retries: int = 1) -> str:
    return generate_with(SYSTEM_PROMPT, f"DATA:\n{data_text}", retries)


def generate_with(system_prompt: str, user_text: str, retries: int = 1) -> str:
    """Return validated Cypher; on a validation failure, feed the error back to the LLM once."""
    prompt, err = user_text, None
    for _ in range(retries + 1):
        cypher = strip_fences(chat(system_prompt, prompt if not err else
                                   f"{prompt}\n\nYour previous output was rejected: {err}\nFix it.", max_tokens=12000))
        try:
            validate(cypher)
            return cypher
        except ValueError as e:
            err = str(e)
            if err.startswith("// ERROR"):
                break  # the LLM found a bad reference in the data: surface it, don't retry
    raise ValueError(err)


def main(paths):
    data = "\n\n".join(f"--- {p} ---\n{Path(p).read_text(encoding='utf-8')}" for p in paths)
    cypher = generate(data)
    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / f"query_{datetime.now():%Y%m%d_%H%M%S}.cypher"
    out.write_text(cypher + "\n", encoding="utf-8")
    print(f"Saved generated Cypher -> {out}\nReview it, then: python ai/apply_query.py {out}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python ai/cypher_generator.py <data file> [...]")
    try:
        main(sys.argv[1:])
    except (ValueError, RuntimeError) as e:
        sys.exit(f"ERROR: {e}")
