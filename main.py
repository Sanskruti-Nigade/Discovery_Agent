import json
import os
import sys
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
import psycopg2
import psycopg2.extras
from neo4j import GraphDatabase
from pydantic import BaseModel
from psycopg2 import sql

load_dotenv()

NEO4J_URI = os.getenv("NEO4J_URI", "neo4j://127.0.0.1:7687")
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME") or os.getenv("NEO4J_USER")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE", "neo4j")
YAML_DIR = os.getenv("YAML_DIR", "yml_files")

PG_DSN = os.getenv("PG_DSN")  # e.g. postgresql://user:pass@localhost:5432/dbname
if PG_DSN:
    PG_CONFIG = dict(dsn=PG_DSN, connect_timeout=5)
else:
    PG_CONFIG = dict(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", "5433")),
        user=os.getenv("POSTGRES_USER", "discovery"),
        password=os.getenv("POSTGRES_PASSWORD", "discovery123"),
        dbname=os.getenv("POSTGRES_DB", "discovery"),
        connect_timeout=5,
    )

app = FastAPI(title="YAML -> Neo4j loader")


def list_yaml_files(file: str | None) -> list[Path]:
    """Only source*.yml and mapping*.yml are loaded (other files such as data.yml are ignored)."""
    folder = Path(YAML_DIR)
    if file:
        p = folder / Path(file).name  # stay inside the folder
        if not p.is_file():
            raise HTTPException(404, f"YAML file not found: {p}")
        return [p]
    if not folder.is_dir():
        raise HTTPException(404, f"YAML folder not found: {folder}")
    files = sorted([*folder.glob("*.yml"), *folder.glob("*.yaml")])
    files = [f for f in files if f.stem.startswith(("source", "mapping"))]
    if not files:
        raise HTTPException(404, f"No source*/mapping* .yml files in {folder}")
    return files


def read_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise HTTPException(400, f"{path.name}: root must be a mapping")
    return data


def get_driver():
    if not NEO4J_USERNAME or not NEO4J_PASSWORD:
        raise HTTPException(500, "NEO4J_USERNAME / NEO4J_PASSWORD not set in environment")
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))


def _sample(value):
    return value if isinstance(value, (str, int, float, bool)) or value is None else json.dumps(value)


def parse_source(path: Path, data: dict):
    """{system: {product: {field: sample_value}}}  ->  System -> Product -> Field."""
    nodes, rels = [], []
    for system, products in data.items():
        if not isinstance(products, dict):
            raise HTTPException(400, f"{path.name}: '{system}' must contain products")
        nodes.append(("System", system, {"name": system, "color": "blue"}))
        for product, fields in products.items():
            if not isinstance(fields, dict):
                raise HTTPException(400, f"{path.name}: '{system}.{product}' must contain fields")
            pid = f"{system}.{product}"
            nodes.append(("Product", pid, {"name": product, "system": system, "color": "red"}))
            rels.append((system, pid, "HAS_PRODUCT"))
            for field, value in fields.items():
                fid = f"{pid}.{field}"
                props = {"name": field, "product": product, "system": system}
                if value is not None:
                    props["sample"] = _sample(value)
                nodes.append(("Field", fid, props))
                rels.append((pid, fid, "HAS_FIELD"))
    return nodes, rels


def parse_mapping(path: Path, data: dict):
    """mapping: [{from: sys.product, to: sys.product, fields: {src_field: tgt_field}}]"""
    rels = []
    for m in data.get("mapping", []):
        if not {"from", "to"} <= m.keys():
            raise HTTPException(400, f"{path.name}: each mapping needs 'from' and 'to': {m}")
        rels.append((m["from"], m["to"], "MAPS_TO"))
        for src, tgt in (m.get("fields") or {}).items():
            rels.append((f"{m['from']}.{src}", f"{m['to']}.{tgt}", "MAPS_TO"))
    return [], rels


@app.get("/neo4j/status")
def neo4j_status():
    """Check whether the API can connect to Neo4j with the credentials in .env."""
    info = {"uri": NEO4J_URI, "database": NEO4J_DATABASE, "username": NEO4J_USERNAME}
    if not NEO4J_USERNAME or not NEO4J_PASSWORD:
        return {"connected": False, "error": "NEO4J_USERNAME / NEO4J_PASSWORD not set", **info}
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))
    try:
        driver.verify_connectivity()
        with driver.session(database=NEO4J_DATABASE) as session:
            session.run("RETURN 1").consume()
        return {"connected": True, **info}
    except Exception as e:
        return {"connected": False, "error": str(e), **info}
    finally:
        driver.close()


@app.post("/load")
def load_yaml_to_neo4j(file: str | None = None):
    """Load source*.yml (System/Product/Field) and mapping*.yml (MAPS_TO links) into Neo4j (idempotent MERGE)."""
    paths = list_yaml_files(file)
    parsed = []
    for p in paths:
        d = read_yaml(p)
        nodes, rels = parse_mapping(p, d) if "mapping" in d else parse_source(p, d)
        parsed.append((p, nodes, rels))

    per_file = write_graph([(p.name, nodes, rels) for p, nodes, rels in parsed])
    return {"files_loaded": len(per_file), "details": per_file}


def write_graph(parsed: list[tuple[str, list, list]]) -> dict:
    """parsed = [(source_name, nodes, rels)]. Idempotent MERGE into Neo4j."""
    driver = get_driver()
    per_source = {}
    try:
        with driver.session(database=NEO4J_DATABASE) as session:
            # Pass 1: all nodes from all sources, so cross-source links resolve.
            for name, nodes, rels in parsed:
                for label, nid, props in nodes:
                    session.run(f"MERGE (x:{label} {{id: $id}}) SET x += $props", id=nid, props=props)
                per_source[name] = {"nodes": len(nodes), "relationships": len(rels)}
            # Pass 2: relationships.
            for name, nodes, rels in parsed:
                for a, b, rtype, *rest in rels:  # optional 4th item = relationship properties
                    res = session.run(
                        f"MATCH (a {{id: $a}}), (b {{id: $b}}) MERGE (a)-[r:{rtype}]->(b) "
                        "SET r += $props RETURN count(*) AS c",
                        a=a, b=b, props=(rest[0] if rest else {}),
                    ).single()
                    if not res or res["c"] == 0:
                        raise HTTPException(400, f"{name}: cannot link {a!r} -> {b!r}; node not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Neo4j error: {e}")
    finally:
        driver.close()
    return per_source


PIPELINE_HINT = """INTERPRETATION RULES for the three JSON files below:
- SOURCE file = one sample response, kept as a NESTED hierarchy. SourceSystem name = "Source". Every JSON object
  (or array of objects) is a Table named ONLY by its own key (array "[]" suffix dropped). The top-level keys
  ("Product_Response", "Contact_Response") are top-level Tables and are the only ones the system OWNS. A Table
  nested inside another is linked with (parent)-[:HAS_TABLE]->(child), e.g.
  Product_Response -HAS_TABLE-> META, MODEL_PRODUCT, ENTITLEMENT_PRODUCT; ENTITLEMENT_PRODUCT -HAS_TABLE-> LIFECYCLE;
  Contact_Response -HAS_TABLE-> profile, metadata. Set `path` on every Table (e.g. "Product_Response.ENTITLEMENT_PRODUCT").
  A scalar value is a Column (named by its own key, e.g. "ID", "person_id") of the Table that directly contains it,
  linked by HAS_COLUMN; Column.table = that Table's name. Do not set any type such as 'api' on the system.
- TARGET file = one sample response. TargetSystem name = "Target". Each top-level key (e.g. dim_model) is a Table
  and its keys are Columns.
- Column data_type is inferred from the sample value: string | boolean | integer | number | date (YYYY-MM-DD) |
  timestamp (ISO date-time). A null value has no stated type (skip data_type).
- MAPPING file = list of "<source path> -> <target table>.<target column>". The source path is
  <path to table>.<column>; the table is the LAST object key before the column, e.g.
  "Product_Response.MODEL_PRODUCT.ID" = table MODEL_PRODUCT, column ID;
  "Product_Response.ENTITLEMENT_PRODUCT.LIFECYCLE[].GEOGRAPHY" = table LIFECYCLE, column GEOGRAPHY;
  "Contact_Response.profile[].number" = table profile, column number;
  "Contact_Response.person_id" = table Contact_Response, column person_id.
  Use mapping_name "Source_to_Target". Every mapping endpoint must exist as a Column in the files, otherwise
  output the // ERROR line.
"""


@app.post("/load/pipeline")
def load_pipeline(
    source_file: str = "Source.json",
    target_file: str = "Target.json",
    mapping_file: str = "mappping.json",
    dry_run: bool = False,
):
    """Source / Target / Mapping = DummyData/<file>. The LLM generates ALL the Cypher (nothing hardcoded);
    ai/cypher_generator validates it (MERGE-only, lineage labels only), it is saved in ai/generated/ and run
    in one transaction. dry_run=true rolls back. Graph: SourceSystem/TargetSystem -OWNS-> Table -HAS_COLUMN->
    Column -HAS_PROPERTY-> Property, and Column -MAPS_TO-> Column."""
    folder = Path(os.getenv("DUMMY_DIR", "DummyData"))

    def read(name: str) -> str:
        p = folder / Path(name).name
        if not p.is_file():
            raise HTTPException(404, f"File not found: {p}")
        return p.read_text(encoding="utf-8")

    data = (f"{PIPELINE_HINT}\n--- SOURCE ({source_file}) ---\n{read(source_file)}\n"
            f"--- TARGET ({target_file}) ---\n{read(target_file)}\n--- MAPPING ({mapping_file}) ---\n{read(mapping_file)}")

    sys.path.insert(0, str(Path(__file__).parent / "ai"))
    import cypher_generator
    from apply_query import run_statements

    cypher_generator.OUT_DIR.mkdir(exist_ok=True)
    prompt, neo_err = data, None
    for attempt in range(2):  # if Neo4j rejects the LLM's Cypher, feed the error back once
        try:
            cypher = cypher_generator.generate(
                prompt if not neo_err else f"{data}\n\nYour previous Cypher failed in Neo4j with: {neo_err}\n"
                                           f"Fix it and output the full corrected Cypher.")
        except ValueError as e:
            raise HTTPException(422, f"LLM Cypher rejected: {e}")
        except Exception as e:
            raise HTTPException(502, f"LLM error: {e}")
        out = cypher_generator.OUT_DIR / f"pipeline_{datetime.now():%Y%m%d_%H%M%S}.cypher"
        out.write_text(cypher + "\n", encoding="utf-8")

        totals: dict = {}
        driver = get_driver()
        try:
            with driver.session(database=NEO4J_DATABASE) as session:
                tx = session.begin_transaction()
                try:
                    run_statements(tx, cypher, totals)
                    tx.rollback() if dry_run else tx.commit()
                except Exception:
                    tx.rollback()
                    raise
            break
        except Exception as e:
            neo_err = str(e)
            if attempt == 1:
                raise HTTPException(502, f"Neo4j error running generated Cypher ({out.name}): {e}")
        finally:
            driver.close()
    return {"dry_run": dry_run, "cypher_file": str(out), "changes": totals, "attempts": attempt + 1,
            "statements": len([s for s in cypher.split(";") if s.strip()])}


# ---------------------------- PostgreSQL (read-only) ----------------------------

def pg_connect():
    try:
        return psycopg2.connect(**PG_CONFIG)
    except Exception as e:
        raise HTTPException(502, f"Postgres connection error: {e}")


@app.get("/postgres/status")
def postgres_status():
    """Check whether the API can connect to Postgres."""
    info = {k: v for k, v in psycopg2.extensions.parse_dsn(PG_DSN).items() if k != "password"} if PG_DSN else         {k: v for k, v in PG_CONFIG.items() if k not in ("password", "connect_timeout")}
    try:
        conn = psycopg2.connect(**PG_CONFIG)
        conn.close()
        return {"connected": True, **info}
    except Exception as e:
        return {"connected": False, "error": str(e), **info}


@app.get("/postgres/tables")
def postgres_tables(schema: str = "public"):
    """List tables in a schema."""
    with pg_connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s AND table_type = 'BASE TABLE' ORDER BY table_name",
            (schema,),
        )
        return {"schema": schema, "tables": [r[0] for r in cur.fetchall()]}


@app.get("/postgres/tables/{table}")
def postgres_table_rows(table: str, schema: str = "public", limit: int = 100, offset: int = 0):
    """Return rows from a table, e.g. GET /postgres/tables/customers?limit=10&offset=0"""
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    with pg_connect() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND table_name = %s",
            (schema, table),
        )
        if not cur.fetchone():  # also guards against SQL injection via table name
            raise HTTPException(404, f"Table not found: {schema}.{table}")
        cur.execute(
            sql.SQL("SELECT * FROM {}.{} LIMIT %s OFFSET %s").format(sql.Identifier(schema), sql.Identifier(table)),
            (limit, offset),
        )
        rows = cur.fetchall()
    return {"table": f"{schema}.{table}", "limit": limit, "offset": offset, "count": len(rows), "rows": rows}


@app.get("/metadata")
def get_metadata(
    object_id: str | None = None,
    source_id: str | None = None,
    environment: str | None = None,
    object_type: str | None = None,
    run_id: int | None = None,
    latest_only: bool = True,
    limit: int = 100,
):
    """Return metadata_snapshot rows incl. schema_json. Filter by object_id/source_id/environment/object_type/run_id.
    latest_only=true (default) returns only the highest version per (environment, source_id, object_id)."""
    limit = max(1, min(limit, 1000))
    where, params = [], []
    for col, val in (("object_id", object_id), ("source_id", source_id), ("environment", environment),
                     ("object_type", object_type), ("run_id", run_id)):
        if val is not None:
            where.append(f"{col} = %s")
            params.append(val)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    query = f"SELECT * FROM metadata_snapshot {clause} ORDER BY captured_at DESC"
    with pg_connect() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    if latest_only:
        seen, latest = set(), []
        for r in rows:  # newest first; keep highest version per object
            key = (r["environment"], r["source_id"], r["object_id"])
            if key not in seen:
                seen.add(key)
                latest.append(r)
        rows = latest
    rows = rows[:limit]
    return {"count": len(rows), "rows": rows}


# ---------------------------- PostgreSQL test endpoints (write + read) ----------------------------

class TestItem(BaseModel):
    name: str
    data: dict | None = None


_TEST_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS api_test_items ("
    "id SERIAL PRIMARY KEY, name TEXT NOT NULL, data JSONB, created_at TIMESTAMPTZ DEFAULT now())"
)


@app.post("/postgres/test/items")
def postgres_test_post(item: TestItem):
    """Insert a row into api_test_items (table is auto-created)."""
    with pg_connect() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(_TEST_TABLE_DDL)
        cur.execute(
            "INSERT INTO api_test_items (name, data) VALUES (%s, %s) RETURNING id, name, data, created_at",
            (item.name, psycopg2.extras.Json(item.data)),
        )
        row = cur.fetchone()
    return {"inserted": row}


@app.get("/postgres/test/items")
def postgres_test_get(limit: int = 100):
    """Read rows back from api_test_items (newest first)."""
    limit = max(1, min(limit, 1000))
    with pg_connect() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(_TEST_TABLE_DDL)
        cur.execute("SELECT id, name, data, created_at FROM api_test_items ORDER BY id DESC LIMIT %s", (limit,))
        rows = cur.fetchall()
    return {"count": len(rows), "rows": rows}
