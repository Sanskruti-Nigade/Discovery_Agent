"""LLM-written READ queries. No Cypher is hardcoded for reading the lineage graph: each caller describes what it
needs (task, parameters, result columns); the LLM writes the Cypher, it is checked (read-only, EXPLAIN-able,
returns the requested columns) and cached in ai/generated/read_queries.json, so the LLM is called once per spec.

    from query_gen import Q, run
    FIND = Q("find_x", "Return every Table of a system ...", {"system": "snowflake"}, ["name"])
    rows = run(session, FIND, system="snowflake")
"""
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from neo4j.exceptions import Neo4jError

from llm_client import chat

CACHE = Path(__file__).parent / "generated" / "read_queries.json"

GRAPH_MODEL = """GRAPH MODEL (Neo4j 5):
Nodes: (:SourceSystem {name,type}) (:TargetSystem {name,type}) (:Table {system,name,schema,path})
       (:Column {system,table,name,data_type}) (:Property {key,value})
       Column and Table nodes may also carry the label :Embeddable with properties search_text and embedding.
Edges: (System)-[:OWNS]->(Table)  (Table)-[:HAS_TABLE]->(Table) nested  (Table)-[:HAS_COLUMN]->(Column)
       (Column)-[:HAS_PROPERTY]->(Property)   e.g. Property {key:'sample'} or {key:'data_type'}
       (Column)-[:MAPS_TO {mapping_name,transformation,created_at}]->(Column)   lineage flows source -> target
       (Column)-[:RENAMED_TO {detected_at}]->(Column)   (Property)-[:CHANGED_TO {system,table,column,detected_at}]->(Property)
"""

SYSTEM = GRAPH_MODEL + """
You write ONE read-only Cypher query for the task given. Rules:
- Output ONLY the Cypher: no prose, no markdown fences, no trailing semicolon.
- Read-only: never CREATE, MERGE, DELETE, SET, REMOVE, DROP, LOAD, FOREACH. CALL is allowed ONLY for the procedure
  db.index.vector.queryNodes, and only if the task asks for a vector search.
- Use the parameters exactly as named ($name) and do not invent others.
- RETURN exactly the requested columns, with exactly those aliases, in that order.
- Use elementId() (never id()). Backtick-quote reserved words used as aliases (e.g. `table`).
"""

WRITE = re.compile(r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD|FOREACH|APOC|DBMS)\b", re.I)
CALLS = re.compile(r"\bCALL\s+([\w.]+)", re.I)


@dataclass(frozen=True)
class Q:
    name: str
    task: str
    params: dict      # parameter name -> example value (shown to the LLM, used to EXPLAIN the query)
    columns: list     # exact RETURN aliases the caller reads ([] for an admin statement that returns nothing)
    admin: bool = False  # True: index/embedding setup (allows CREATE VECTOR INDEX, SET and the vector/index procedures)

    @property
    def key(self) -> str:
        return hashlib.sha1(json.dumps([self.task, self.params, self.columns, self.admin], sort_keys=True, default=str)
                            .encode()).hexdigest()[:16]


ADMIN_SYSTEM = GRAPH_MODEL + """
You write ONE Cypher statement for index/embedding setup. Rules:
- Output ONLY the Cypher: no prose, no markdown fences, no trailing semicolon.
- Allowed: CREATE VECTOR INDEX ... IF NOT EXISTS, SET on :Embeddable / search_text, and CALL only of
  db.create.setNodeVectorProperty and db.awaitIndexes. Never DELETE, DROP, REMOVE, MERGE or create nodes/relationships.
- Use the parameters exactly as named ($name); do not invent others. Use elementId() (never id()).
"""
ADMIN_CALLS = {"db.create.setnodevectorproperty", "db.awaitindexes"}


def check(cypher: str, admin: bool = False) -> None:
    body = re.sub(r"'[^']*'|\"[^\"]*\"", "''", cypher)  # ignore text inside string literals
    if admin:
        if re.search(r"(DELETE|DETACH|REMOVE|DROP|LOAD|FOREACH|APOC|DBMS|MERGE)", body, re.I):
            raise ValueError("forbidden keyword in admin statement")
        if re.search(r"CREATE", body, re.I) and not re.search(r"CREATE\s+VECTOR\s+INDEX", body, re.I):
            raise ValueError("only CREATE VECTOR INDEX is allowed")
        for proc in CALLS.findall(body):
            if proc.lower() not in ADMIN_CALLS:
                raise ValueError(f"procedure not allowed: {proc}")
        return
    if m := WRITE.search(body):
        raise ValueError(f"not read-only: {m.group(0)}")
    for proc in CALLS.findall(body):
        if proc.lower() != "db.index.vector.querynodes":
            raise ValueError(f"procedure not allowed: {proc}")
    if "RETURN" not in body.upper():
        raise ValueError("no RETURN")


def _load() -> dict:
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(cache: dict) -> None:
    try:
        CACHE.parent.mkdir(exist_ok=True)
        CACHE.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except Exception:
        pass  # the cache is only an optimisation


def _generate(session, q: Q, retries: int = 2) -> str:
    user = (f"TASK: {q.task}\nPARAMETERS (name: example): {json.dumps(q.params, default=str)}\n"
            f"RETURN COLUMNS (exact aliases): {q.columns}")
    err = None
    for _ in range(retries + 1):
        text = chat(ADMIN_SYSTEM if q.admin else SYSTEM, user if not err else f"{user}\n\nYour previous query was rejected: {err}\nFix it.",
                    max_tokens=1500)
        cypher = re.sub(r"^```[a-zA-Z]*\n|\n?```\s*$", "", text.strip()).rstrip(";").strip()
        try:
            check(cypher, q.admin)
            keys = session.run("EXPLAIN " + cypher, **q.params).keys()  # syntax + result columns, nothing executed
            if list(keys) != q.columns:
                raise ValueError(f"must return exactly {q.columns}, got {list(keys)}")
            return cypher
        except (ValueError, Neo4jError) as e:  # a connection problem is not the LLM's fault: let it propagate
            err = str(e)[:400]
    raise ValueError(f"LLM could not write a valid query for '{q.name}': {err}")


def cypher_for(session, q: Q) -> str:
    cache = _load()
    hit = cache.get(q.name)
    if hit and hit.get("key") == q.key:
        return hit["cypher"]
    cypher = _generate(session, q)
    cache[q.name] = {"key": q.key, "cypher": cypher}
    _save(cache)
    return cypher


def run(session, q: Q, **params) -> list[dict]:
    """Run the LLM-written query for q with the given parameters; each row is a dict."""
    res = session.run(cypher_for(session, q), **params)
    if q.columns:
        return [r.data() for r in res]
    res.consume()
    return []
