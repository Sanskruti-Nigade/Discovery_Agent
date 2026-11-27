"""Neo4j writer. Cypher comes from fixed templates; the ONLY interpolated text is label / relationship names that
GraphModel already validated against a strict regex and that are backtick-quoted. All data values are parameters."""
import logging
import uuid
from urllib.parse import urlparse

from neo4j import GraphDatabase

from .config import Limits, LoadError, Neo4jConfig
from .model import GraphModel
from .schema import Analysis

log = logging.getLogger("graphload")


def _q(name: str) -> str:
    return f"`{name}`"


def build_queries(m: GraphModel) -> dict:
    """All Cypher used by the loader, in one place so it can be reviewed and unit-tested."""
    t, c = _q(m.table_label), _q(m.column_label)

    def link(rel, a, b):
        return (f"UNWIND $rows AS r MATCH (a:{a} {{id: r.a}}) MATCH (b:{b} {{id: r.b}}) "
                f"MERGE (a)-[x:{_q(rel)}]->(b) SET x.load_id = $load_id")

    return {
        # Identity = the project's natural keys: System(name), Table(system, name), Column(system, table, name).
        # `id` is stored too and used to wire relationships after every node exists.
        "constraints": [f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{_q(m.source_system_label)}) REQUIRE n.name IS UNIQUE",
                        f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{_q(m.target_system_label)}) REQUIRE n.name IS UNIQUE",
                        f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{t}) REQUIRE (n.system, n.name) IS UNIQUE",
                        f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{c}) REQUIRE (n.system, n.table, n.name) IS UNIQUE"],
        "system": lambda label: f"MERGE (s:{_q(label)} {{name: $name}}) SET s += $props, s.load_id = $load_id",
        "tables": f"UNWIND $rows AS r MERGE (n:{t} {{system: r.system, name: r.name}}) SET n += r, n.load_id = $load_id",
        "columns": (f"UNWIND $rows AS r MERGE (n:{c} {{system: r.system, table: r.table, name: r.name}}) "
                    f"SET n += r, n.load_id = $load_id"),
        "owns": lambda label: link(m.owns, _q(label), t),
        "has_table": link(m.has_table, t, t),
        "has_column": link(m.has_column, t, c),
        "maps_to": (f"UNWIND $rows AS r MATCH (a:{c} {{id: r.a}}) MATCH (b:{c} {{id: r.b}}) "
                    f"MERGE (a)-[x:{_q(m.maps_to)}]->(b) SET x += r.props, x.load_id = $load_id"),
        # pruning: remove what an EARLIER load of this system created but this load no longer contains
        "prune_nodes": [f"MATCH (n:{lbl}) WHERE n.system = $system AND n.load_id <> $load_id DETACH DELETE n"
                        for lbl in (t, c)],
        # decision-driven removal (works for nodes written by any earlier loader, not just ones carrying a load_id)
        "drop_columns": f"UNWIND $rows AS r MATCH (n:{c} {{system: $system, table: r.t, name: r.n}}) DETACH DELETE n",
        "drop_tables": f"UNWIND $rows AS r MATCH (n:{t} {{system: $system, name: r.n}}) DETACH DELETE n",
        "clear_maps": f"MATCH (:{c} {{system: $source}})-[x:{_q(m.maps_to)}]->(:{c} {{system: $target}}) DELETE x",
        "prune_maps": (f"MATCH (:{c} {{system: $source}})-[x:{_q(m.maps_to)}]->(:{c} {{system: $target}}) "
                       f"WHERE x.load_id <> $load_id DELETE x"),
    }


def _chunks(rows: list, size: int):
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


def _driver(cfg: Neo4jConfig):
    host = urlparse(cfg.uri).hostname or ""
    if cfg.uri.split(":")[0] in ("bolt", "neo4j") and host not in ("localhost", "127.0.0.1", "::1"):
        log.warning("Neo4j connection is not encrypted; use neo4j+s:// (or bolt+s://) for remote servers")
    return GraphDatabase.driver(cfg.uri, auth=(cfg.user, cfg.password),
                                connection_timeout=15, max_transaction_retry_time=30)


def write_graph(analyses: list[Analysis], mappings: list[dict], model: GraphModel, cfg: Neo4jConfig,
                limits: Limits, *, dry_run=False, prune=False, constraints=True, plan=None, decision=None) -> dict:
    """Write everything in ONE transaction (all or nothing). Transient errors are retried by the driver."""
    q = build_queries(model)
    load_id = uuid.uuid4().hex
    size = limits.batch_size
    source = next(a for a in analyses if a.role == "source")
    target = next(a for a in analyses if a.role == "target")
    labels = {"source": model.source_system_label, "target": model.target_system_label}

    def apply(tx) -> dict:
        totals = {"nodes_created": 0, "relationships_created": 0, "nodes_deleted": 0, "relationships_deleted": 0}

        def run(query, **params):
            counters = tx.run(query, load_id=load_id, **params).consume().counters
            for key in totals:
                totals[key] += getattr(counters, key)

        if decision == "remap":  # the old mappings no longer describe this data: clear them first
            run(q["clear_maps"], source=source.system, target=target.system)
        for a in analyses:
            run(q["system"](labels[a.role]), name=a.system, props={"id": a.system_id, "name": a.system, "role": a.role})
            for rows in _chunks(a.tables, size):
                run(q["tables"], rows=rows)
            for rows in _chunks(a.columns, size):
                run(q["columns"], rows=rows)
        for a in analyses:  # relationships after ALL nodes exist, so cross-system links always resolve
            for key, pairs in (("owns", a.owns), ("has_table", a.has_table), ("has_column", a.has_column)):
                query = q[key](labels[a.role]) if key == "owns" else q[key]
                for rows in _chunks([{"a": x, "b": y} for x, y in pairs], size):
                    run(query, rows=rows)
        for rows in _chunks(mappings, size):
            run(q["maps_to"], rows=rows)
        if plan is not None and decision in ("apply", "remap") and not dry_run:
            for d in plan.systems:  # remove what the user has just agreed is gone
                for rows, key in (([{"t": t, "n": n} for t, n in d.removed_columns], "drop_columns"),
                                  ([{"n": n} for n in d.removed_tables], "drop_tables")):
                    for chunk in _chunks(rows, size):
                        run(q[key], rows=chunk, system=d.system)
        if prune and not dry_run:
            for a in analyses:
                for query in q["prune_nodes"]:
                    run(query, system=a.system)
            if mappings:
                run(q["prune_maps"], source=source.system, target=target.system)
        return totals

    driver = _driver(cfg)
    try:
        with driver.session(database=cfg.database) as session:
            if constraints and not dry_run:  # schema changes cannot share a data transaction
                for stmt in q["constraints"]:
                    session.run(stmt).consume()
            if dry_run:
                with session.begin_transaction() as tx:
                    result = apply(tx)
                    tx.rollback()
            else:
                result = session.execute_write(apply)  # managed transaction: automatic retry on transient errors
    except LoadError:
        raise
    except Exception as e:
        log.exception("Neo4j write failed")
        raise LoadError(f"Neo4j write failed ({type(e).__name__}); see the server log") from None
    finally:
        driver.close()
    return {"load_id": load_id, "dry_run": dry_run, "tables": sum(len(a.tables) for a in analyses),
            "columns": sum(len(a.columns) for a in analyses), "mappings": len(mappings), **result}
