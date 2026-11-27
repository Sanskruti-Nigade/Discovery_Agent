"""Map a source table to the target tables with the LLM and queue the result for human review
(owner: AI Engineer; moved here from discovery-service/discovery/schema_mapping.py).

1. Schemas come only from PostgreSQL metadata_snapshot + schema_field: the latest snapshot of the source
   table (object_type table/api) and of every target table (object_type target_object).
2. The LLM (mapping.llm.map_schemas) proposes source field -> target field mappings.
3. Every mapping is stored as one pending review_queue row (link_type MAPS_TO). Rows of one run share
   evidence.batch_id; the evidence also holds the mapping text ("table.column -> table.column"), the LLM
   reason, the transformation and the source/target field metadata.
4. A reviewer approves or rejects the batch (optionally only some rows). Each decided row gets a
   review_decision row and its review_queue status becomes approved or rejected. Nothing is written to
   Neo4j here: the graph team reads approved mappings from list_approved() (GET /api/schema-mapping/approved).
"""
import json
import logging
import os
import uuid

from mapping import db as store
from mapping import llm as llm_client
from mapping.db import ENVIRONMENT

log = logging.getLogger("mapping.service")

CREATED_BY = "svc-schema-mapping"
EVIDENCE_SOURCE = "schema_mapping"
# Prompts stay bounded: above this many target fields the caller must pick target tables.
MAX_TARGET_FIELDS_FOR_LLM = int(os.environ.get("MAX_TARGET_FIELDS_FOR_LLM", 600))
# Rows still open for a decision. 'approving'/'failed' only exist in rows written by the older
# discovery-service flow (which also wrote Neo4j); they are treated like 'pending'.
OPEN_STATUSES = ("pending", "approving", "failed")


class SourceNotFoundError(LookupError):
    """The source table name matches no table/API snapshot."""


class SchemaMappingError(ValueError):
    """The request cannot be served as asked (ambiguous name, too many target fields, ...)."""


class BatchNotFoundError(LookupError):
    """No review_queue rows carry this batch id."""


class BatchAlreadyDecidedError(RuntimeError):
    """Nothing in the batch is left to decide."""


class BatchSelectionError(ValueError):
    """The reviewer accepted review ids that are not open in the batch."""


# ---------------------------------------------------------------- schemas (metadata_snapshot + schema_field)
LATEST_OBJECTS = """
WITH latest AS (
    SELECT DISTINCT ON (object_id) snapshot_id, object_id, object_type, version, schema_json, captured_at
    FROM metadata_snapshot
    WHERE environment=%s AND object_type = ANY(%s)
    ORDER BY object_id, version DESC, captured_at DESC
)
SELECT l.object_id, l.object_type, l.version, l.captured_at,
       l.schema_json->'extra'->>'system' AS system,
       l.schema_json->'extra'->>'system_name' AS system_name,
       l.schema_json->'extra'->>'entity' AS entity,
       l.schema_json->'extra'->>'table' AS table_name,
       l.schema_json->'extra'->>'api_id' AS api_id,
       (SELECT count(*) FROM schema_field sf WHERE sf.snapshot_id=l.snapshot_id) AS field_count,
       l.snapshot_id
FROM latest l
ORDER BY l.object_id
"""

OBJECT_FIELDS = """
SELECT snapshot_id, field_path, graph_field_key, data_type, description, max_length
FROM schema_field WHERE snapshot_id = ANY(%s)
ORDER BY snapshot_id, field_path
"""


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def _object_row(row: dict) -> dict:
    return {
        "object_id": row["object_id"],
        "object_type": row["object_type"],
        "version": row["version"],
        "system": row.get("system") or ("sys." + row["object_id"].split(".")[0]),
        "system_name": row.get("system_name"),
        "entity": row.get("entity"),
        "table_name": row.get("table_name"),
        "api_id": row.get("api_id"),
        "field_count": row.get("field_count"),
        "captured_at": _iso(row.get("captured_at")),
    }


def list_objects(kind: str) -> list[dict]:
    """kind 'sources' = table/api snapshots, 'targets' = target_object snapshots."""
    types = ["table", "api"] if kind == "sources" else ["target_object"]
    with store.connect() as conn:
        rows = conn.execute(LATEST_OBJECTS, (ENVIRONMENT, types)).fetchall()
    return [_object_row(row) for row in rows]


def _load(types: list[str]) -> list[dict]:
    with store.connect() as conn:
        objects = conn.execute(LATEST_OBJECTS, (ENVIRONMENT, types)).fetchall()
        fields = conn.execute(OBJECT_FIELDS, ([o["snapshot_id"] for o in objects],)).fetchall() if objects else []
    by_snapshot: dict[int, list[dict]] = {}
    for f in fields:
        by_snapshot.setdefault(f["snapshot_id"], []).append({
            "field_path": f["field_path"], "field_key": f["graph_field_key"], "data_type": f["data_type"],
            "description": f["description"], "max_length": f["max_length"]})
    return [{**_object_row(o), "fields": by_snapshot.get(o["snapshot_id"], [])} for o in objects]


def _resolve_source(name: str, sources: list[dict]) -> dict:
    """Accept the object id (mdm.product_api), its short name (product_api), the table name
    (x_mdm_product) or the API id - case-insensitive."""
    wanted = name.strip().lower()
    matches = [s for s in sources if wanted in {
        (s["object_id"] or "").lower(), s["object_id"].split(".", 1)[-1].lower(),
        (s["table_name"] or "").lower(), (s["api_id"] or "").lower()} - {""}]
    if not matches:
        raise SourceNotFoundError(
            f"No source table or API named {name!r} in metadata_snapshot. "
            f"Known: {', '.join(s['object_id'] for s in sources) or 'none'}")
    if len(matches) > 1:
        raise SchemaMappingError(
            f"{name!r} matches several sources ({', '.join(m['object_id'] for m in matches)}); "
            "pass the full object id")
    return matches[0]


# ---------------------------------------------------------------- display names ("table.column")
def source_label(source: dict, field_path: str) -> str:
    """'MODEL_PRODUCT.ID' -> 'model_product.id'; 'META.VERSION' -> 'product_meta.version';
    'ENTITLEMENT_PRODUCT.LIFECYCLE[].GEOGRAPHY' -> 'entitlement_product_lifecycle.geography';
    a top-level API field -> '<entity>_response.<field>'; a table column -> '<table>.<column>'.
    Same naming as lineage/json_files/mapping.json, so the graph team can load the strings as they are."""
    parts = [p.replace("[]", "").lower() for p in field_path.split(".")]
    entity = (source.get("entity") or "").split(".")[-1].lower()
    if len(parts) == 1:
        if source["object_type"] == "api":
            table = f"{entity}_response" if entity else "response"
        else:
            table = (source.get("table_name") or source["object_id"].split(".", 1)[-1]).lower()
    else:
        table = "_".join(parts[:-1])
        if entity and entity not in table.split("_"):
            table = f"{entity}_{table}"
    return f"{table}.{parts[-1]}"


def target_label(target: dict, field_path: str, ambiguous: set[str] = frozenset()) -> str:
    """'snowflake.dim_model' + 'model_id' -> 'dim_model.model_id'. When two target systems have a
    table with the same name, the system stays in front: 'sales_crm.contact.Email'."""
    table = target["object_id"].split(".", 1)[-1]
    return f"{target['object_id'] if table in ambiguous else table}.{field_path}"


# ---------------------------------------------------------------- generate
def _llm_view(obj: dict) -> dict:
    return {"object_id": obj["object_id"], "system": obj["system"],
            "fields": [{k: f[k] for k in ("field_key", "field_path", "data_type", "description")}
                       for f in obj["fields"]]}


def generate(source_name: str, *, target_object_ids: list[str] | None = None, min_conf: float = 0.6,
             requested_by: str | None = None) -> dict:
    source = _resolve_source(source_name, _load(["table", "api"]))
    if not source["fields"]:
        raise SchemaMappingError(f"Source {source['object_id']!r} has no fields in schema_field.")
    targets = _load(["target_object"])
    if target_object_ids:
        unknown = sorted(set(target_object_ids) - {t["object_id"] for t in targets})
        if unknown:
            raise SchemaMappingError(f"Unknown target tables: {', '.join(unknown)}")
        targets = [t for t in targets if t["object_id"] in set(target_object_ids)]
    targets = [t for t in targets if t["fields"]]
    if not targets:
        raise SchemaMappingError("No target tables found in metadata_snapshot (object_type target_object).")
    field_count = sum(len(t["fields"]) for t in targets)
    if field_count > MAX_TARGET_FIELDS_FOR_LLM:
        raise SchemaMappingError(
            f"{field_count} target fields is more than {MAX_TARGET_FIELDS_FOR_LLM}; "
            "pass target_object_ids to choose the target tables.")

    suggestions = llm_client.map_schemas(_llm_view(source), [_llm_view(t) for t in targets])

    short_names = [t["object_id"].split(".", 1)[-1] for t in targets]
    ambiguous = {n for n in short_names if short_names.count(n) > 1}
    source_fields = {f["field_key"]: f for f in source["fields"]}
    target_fields = {f["field_key"]: (t, f) for t in targets for f in t["fields"]}
    batch_id = str(uuid.uuid4())
    mappings: dict[tuple[str, str], dict] = {}
    dropped = 0
    for s in suggestions:
        pair = (s["source_field_key"], s["target_field_key"])
        if pair[0] not in source_fields or pair[1] not in target_fields:
            log.warning("LLM proposed a field that is not in the schemas source=%s target=%s", *pair)
            dropped += 1
            continue
        if s["confidence"] < min_conf or pair in mappings:
            continue
        src = source_fields[pair[0]]
        tgt_obj, tgt = target_fields[pair[1]]
        mappings[pair] = {
            "batch_id": batch_id,
            "mapping": f"{source_label(source, src['field_path'])} -> "
                       f"{target_label(tgt_obj, tgt['field_path'], ambiguous)}",
            "source_object_id": source["object_id"],
            "target_object_id": tgt_obj["object_id"],
            "target_system": tgt_obj["system"],
            "target_system_name": tgt_obj["system_name"],
            "source_field": src,
            "target_field": tgt,
            "confidence": s["confidence"],
            "transformation": s["transformation"],
            "reason": s["reason"],
        }
    ordered = sorted(mappings.values(), key=lambda m: (m["source_field"]["field_path"], m["target_object_id"]))
    already_pending = _store_batch(batch_id, source, ordered, requested_by, min_conf)
    stored = [m for m in ordered if m.get("review_id")]
    log.info("Schema mapping generated batch_id=%s source=%s targets=%d suggestions=%d stored=%d "
             "already_pending=%d dropped=%d", batch_id, source["object_id"], len(targets),
             len(suggestions), len(stored), len(already_pending), dropped)
    return {
        "batch_id": batch_id,
        "status": "pending",
        "source": {k: source[k] for k in ("object_id", "object_type", "version", "system", "entity",
                                          "table_name", "field_count")},
        "target_object_ids": [t["object_id"] for t in targets],
        "target_fields_considered": field_count,
        "mapping": [m["mapping"] for m in stored],
        "mappings": [_public_mapping(m) for m in stored],
        "already_pending": already_pending,
    }


def _public_mapping(m: dict) -> dict:
    return {
        "review_id": m.get("review_id"),
        "status": m.get("status", "pending"),
        "mapping": m["mapping"],
        "source_field_key": m["source_field"]["field_key"],
        "source_field_path": m["source_field"]["field_path"],
        "source_data_type": m["source_field"].get("data_type"),
        "target_object_id": m["target_object_id"],
        "target_system": m["target_system"],
        "target_field_key": m["target_field"]["field_key"],
        "target_field_path": m["target_field"]["field_path"],
        "target_data_type": m["target_field"].get("data_type"),
        "target_description": m["target_field"].get("description"),
        "confidence": m["confidence"],
        "transformation": m.get("transformation"),
        "reason": m["reason"],
        **{k: m[k] for k in ("decision", "decided_by", "decided_at", "comment") if m.get(k) is not None},
    }


def _store_batch(batch_id: str, source: dict, mappings: list[dict], requested_by: str | None,
                 min_conf: float) -> list[dict]:
    """One pending review_queue row per mapping. A pair already pending elsewhere is reported."""
    already = []
    with store.connect() as conn:
        for m in mappings:
            evidence = {
                "source": EVIDENCE_SOURCE,
                "batch_id": batch_id,
                "source_object_id": source["object_id"],
                "source_object_version": source["version"],
                "requested_by": requested_by,
                "min_conf": min_conf,
                **{k: m[k] for k in ("mapping", "target_object_id", "target_system", "target_system_name",
                                     "source_field", "target_field", "transformation", "reason")},
            }
            row = conn.execute(
                """INSERT INTO review_queue (from_field_key, to_field_key, link_type, confidence,
                                             evidence, created_by)
                   VALUES (%s,%s,'MAPS_TO',%s,%s,%s)
                   ON CONFLICT (from_field_key, to_field_key, link_type) WHERE status='pending'
                   DO NOTHING RETURNING review_id""",
                (m["source_field"]["field_key"], m["target_field"]["field_key"], round(m["confidence"], 2),
                 json.dumps(evidence), CREATED_BY),
            ).fetchone()
            if row:
                m["review_id"] = row["review_id"]
            else:
                existing = conn.execute(
                    """SELECT review_id, evidence->>'batch_id' AS batch_id, created_by FROM review_queue
                       WHERE from_field_key=%s AND to_field_key=%s AND link_type='MAPS_TO' AND status='pending'""",
                    (m["source_field"]["field_key"], m["target_field"]["field_key"]),
                ).fetchone()
                already.append({"mapping": m["mapping"], **(dict(existing) if existing else {})})
        conn.commit()
    return already


# ---------------------------------------------------------------- reading the queue
# Every review_queue row is shown, whoever wrote it. Rows from this module share evidence.batch_id;
# rows written by other discovery-service jobs (svc-ai-matcher, svc-mapping-discovery, svc-impact-report,
# feedback) have no batch and become a one-row batch "review-<review_id>". Their source/target table comes
# from the evidence, or else from schema_field by field key.
BATCH_KEY = "COALESCE(q.evidence->>'batch_id', 'review-' || q.review_id)"

REVIEW_SELECT = f"""
SELECT q.review_id, q.from_field_key, q.to_field_key, q.link_type, q.confidence, q.status, q.evidence,
       q.created_at, q.created_by,
       {BATCH_KEY} AS batch_id,
       COALESCE(q.evidence->>'source', q.created_by) AS origin,
       COALESCE(q.evidence->>'mapping', q.from_field_key || ' -> ' || q.to_field_key) AS mapping,
       COALESCE(q.evidence->>'source_object_id', src.object_id) AS source_object_id,
       COALESCE(q.evidence->>'target_object_id', tgt.object_id) AS target_object_id,
       src.field_path AS src_field_path, src.data_type AS src_data_type,
       tgt.field_path AS tgt_field_path, tgt.data_type AS tgt_data_type,
       d.decision, d.decided_by, d.decided_at, d.comment
FROM review_queue q
LEFT JOIN LATERAL (
    SELECT decision, decided_by, decided_at, comment FROM review_decision
    WHERE review_id = q.review_id ORDER BY decided_at DESC, decision_id DESC LIMIT 1
) d ON TRUE
LEFT JOIN LATERAL (
    SELECT sf.object_id, sf.field_path, sf.data_type FROM schema_field sf
    JOIN metadata_snapshot ms ON ms.snapshot_id = sf.snapshot_id
    WHERE sf.graph_field_key = q.from_field_key AND ms.environment = %(env)s
    ORDER BY ms.version DESC, ms.captured_at DESC LIMIT 1
) src ON TRUE
LEFT JOIN LATERAL (
    SELECT sf.object_id, sf.field_path, sf.data_type FROM schema_field sf
    JOIN metadata_snapshot ms ON ms.snapshot_id = sf.snapshot_id
    WHERE sf.graph_field_key = q.to_field_key AND ms.environment = %(env)s
    ORDER BY ms.version DESC, ms.captured_at DESC LIMIT 1
) tgt ON TRUE
"""

BATCH_ROWS = REVIEW_SELECT + f" WHERE {BATCH_KEY} = %(batch_id)s ORDER BY q.review_id"


def _norm(status: str) -> str:
    return "pending" if status in OPEN_STATUSES else status


def _row_mapping(row: dict) -> dict:
    """One review row in the shape the UI and the approved feed use, whatever job wrote it."""
    ev = row["evidence"]
    note = None
    if not isinstance(ev, dict):  # some jobs (svc-mapping-discovery) store plain text as evidence
        note, ev = (str(ev) if ev is not None else None), {}
    sf, tf = ev.get("source_field") or {}, ev.get("target_field") or {}
    return {
        "review_id": row["review_id"],
        "batch_id": row["batch_id"],
        "origin": row["origin"],
        "status": _norm(row["status"]),
        "mapping": row["mapping"],
        "link_type": row.get("link_type"),
        "source_object_id": row["source_object_id"],
        "source_object_version": ev.get("source_object_version"),
        "source_field_key": row["from_field_key"],
        "source_field_path": sf.get("field_path") or row.get("src_field_path"),
        "source_data_type": sf.get("data_type") or row.get("src_data_type"),
        "target_object_id": row["target_object_id"],
        "target_system": ev.get("target_system"),
        "target_field_key": row["to_field_key"],
        "target_field_path": tf.get("field_path") or row.get("tgt_field_path"),
        "target_data_type": tf.get("data_type") or row.get("tgt_data_type"),
        "target_description": tf.get("description"),
        "confidence": float(row["confidence"]) if row["confidence"] is not None else None,
        "transformation": ev.get("transformation"),
        "reason": ev.get("reason") or ev.get("feedback") or note,
        "requested_by": ev.get("requested_by") or row["created_by"],
        "created_by": row["created_by"],
        "created_at": _iso(row.get("created_at")),
        "decision": row.get("decision"),
        "decided_by": row.get("decided_by"),
        "decided_at": _iso(row.get("decided_at")),
        "comment": row.get("comment"),
    }


def _batch_status(statuses: list[str]) -> str:
    """pending while anything is open; otherwise approved if any row was approved, else rejected."""
    if any(s in OPEN_STATUSES for s in statuses):
        return "pending"
    return "approved" if "approved" in statuses else "rejected"


def _counts(statuses: list[str]) -> dict:
    norm = [_norm(s) for s in statuses]
    return {s: norm.count(s) for s in sorted(set(norm))}


def _graph_names(m: dict) -> tuple[tuple[str, str, str], tuple[str, str, str]]:
    """(system, table, column) for both ends, named like lineage/json_files: the source system is the
    source object id with '_' (mdm.product_api -> mdm_product_api), the target system is the first part of
    the target object id (snowflake.dim_model -> snowflake), tables/columns come from the mapping string."""
    src_ref, _, tgt_ref = m["mapping"].partition(" -> ")
    s_table, _, s_col = src_ref.rpartition(".")
    t_table, _, t_col = tgt_ref.rpartition(".")
    src_obj = m.get("source_object_id") or s_table.split(".")[0]
    tgt_obj = m.get("target_object_id") or t_table
    s_sys = src_obj.replace(".", "_")
    t_sys = tgt_obj.split(".")[0] if tgt_obj else "target"
    if t_table.startswith(f"{t_sys}."):  # 'sales_crm.contact.Email' when two systems share a table name
        t_table = t_table[len(t_sys) + 1:]
    return (s_sys, s_table, s_col), (t_sys, t_table, t_col)


def build_graph_update(mappings: list[dict]) -> dict:
    """What the mappings mean in the shared Neo4j (graph dev's model):
    (:SourceSystem)-[:OWNS]->(:Table)-[:HAS_COLUMN]->(:Column)-[:MAPS_TO]->(:Column)<-[:HAS_COLUMN]-(:Table)
    <-[:OWNS]-(:TargetSystem). Systems, tables and columns usually exist already (on_create_only);
    MAPS_TO is the mapping itself and carries review_id so a UI can follow the reviewer's selection.
    This is a preview / hand-off payload: this module does not write Neo4j."""
    nodes: dict[str, dict] = {}
    rels: dict[tuple, dict] = {}

    def node(label, nid, props):
        nodes.setdefault(nid, {"label": label, "id": nid, "on_create_only": True,
                               "properties": {k: v for k, v in props.items() if v is not None}})

    def rel(rtype, a, b, props=None, create_only=True):
        rels[(rtype, a[1], b[1])] = {"type": rtype, "from": {"label": a[0], "id": a[1]},
                                     "to": {"label": b[0], "id": b[1]}, "properties": props or {},
                                     **({"on_create_only": True} if create_only else {})}

    for m in mappings:
        (s_sys, s_tab, s_col), (t_sys, t_tab, t_col) = _graph_names(m)
        for role, (sysn, tab, col), dtype in (("SourceSystem", (s_sys, s_tab, s_col), m.get("source_data_type")),
                                              ("TargetSystem", (t_sys, t_tab, t_col), m.get("target_data_type"))):
            node(role, sysn, {"name": sysn})
            node("Table", f"{sysn}.{tab}", {"system": sysn, "name": tab})
            node("Column", f"{sysn}.{tab}.{col}", {"system": sysn, "table": tab, "name": col, "data_type": dtype})
            rel("OWNS", (role, sysn), ("Table", f"{sysn}.{tab}"))
            rel("HAS_COLUMN", ("Table", f"{sysn}.{tab}"), ("Column", f"{sysn}.{tab}.{col}"))
        props = {"mapping_name": f"{s_sys}_to_{t_sys}", "transformation": m.get("transformation"),
                 "confidence": m.get("confidence"), "status": m.get("status"), "review_id": m.get("review_id"),
                 "batch_id": m.get("batch_id"), "reason": m.get("reason")}
        rel("MAPS_TO", ("Column", f"{s_sys}.{s_tab}.{s_col}"), ("Column", f"{t_sys}.{t_tab}.{t_col}"),
            {k: v for k, v in props.items() if v is not None}, create_only=False)
    return {"model": "SourceSystem/TargetSystem -OWNS-> Table -HAS_COLUMN-> Column -MAPS_TO-> Column",
            "nodes": list(nodes.values()), "relationships": list(rels.values())}


def list_batches(status: str | None = None, source_object_id: str | None = None, limit: int = 50) -> list[dict]:
    """One item per batch, newest first. status = pending | approved | rejected | None (all)."""
    with store.connect() as conn:
        rows = conn.execute(
            f"""WITH r AS ({REVIEW_SELECT})
               SELECT batch_id,
                      min(source_object_id) AS source_object_id,
                      min(origin) AS origin,
                      (array_agg(COALESCE(evidence->>'requested_by', created_by)))[1] AS requested_by,
                      min(created_at) AS created_at,
                      count(*) AS mapping_count,
                      avg(confidence) AS avg_confidence,
                      array_agg(status) AS statuses,
                      array_agg(DISTINCT target_object_id) AS target_object_ids,
                      max(decided_at) AS decided_at,
                      (array_agg(decided_by ORDER BY decided_at DESC NULLS LAST))[1] AS decided_by
               FROM r
               WHERE (%(src)s::varchar IS NULL OR source_object_id = %(src)s)
               GROUP BY batch_id
               ORDER BY min(created_at) DESC, batch_id""",
            {"env": ENVIRONMENT, "src": source_object_id},
        ).fetchall()
    out = []
    for row in rows:
        statuses = list(row["statuses"])
        item = {
            "batch_id": row["batch_id"],
            "status": _batch_status(statuses),
            "origin": row["origin"],
            "source_object_id": row["source_object_id"],
            "target_object_ids": sorted(t for t in row["target_object_ids"] if t),
            "requested_by": row["requested_by"],
            "created_at": _iso(row["created_at"]),
            "mapping_count": row["mapping_count"],
            "avg_confidence": round(float(row["avg_confidence"] or 0), 2),
            "counts": _counts(statuses),
            "decided_by": row["decided_by"],
            "decided_at": _iso(row["decided_at"]),
        }
        if status in (None, "", "all") or item["status"] == status:
            out.append(item)
    return out[:limit]


def list_reviews(status: str | None = None, q: str | None = None, limit: int = 200, offset: int = 0) -> dict:
    """Every review_queue row (flat), newest first, with its latest decision."""
    statuses = list(OPEN_STATUSES) if status == "pending" else [status] if status not in (None, "", "all") else None
    like = f"%{q.strip().lower()}%" if q and q.strip() else None
    with store.connect() as conn:
        rows = conn.execute(
            f"""WITH r AS ({REVIEW_SELECT})
               SELECT *, count(*) OVER () AS total FROM r
               WHERE (%(st)s::varchar[] IS NULL OR status = ANY(%(st)s::varchar[]))
                 AND (%(like)s::varchar IS NULL OR lower(mapping) LIKE %(like)s
                      OR lower(COALESCE(source_object_id, '')) LIKE %(like)s
                      OR lower(COALESCE(target_object_id, '')) LIKE %(like)s
                      OR lower(origin) LIKE %(like)s)
               ORDER BY created_at DESC, review_id DESC
               LIMIT %(limit)s OFFSET %(offset)s""",
            {"env": ENVIRONMENT, "st": statuses, "like": like, "limit": limit, "offset": offset},
        ).fetchall()
    return {"total": rows[0]["total"] if rows else 0, "items": [_row_mapping(r) for r in rows]}


def summary() -> dict:
    """Counts for the navigation badge and the Reviews page header."""
    with store.connect() as conn:
        rows = conn.execute("SELECT status, count(*) AS n FROM review_queue GROUP BY status").fetchall()
        pending_batches = conn.execute(
            f"SELECT count(DISTINCT {BATCH_KEY}) AS n FROM review_queue q WHERE q.status = ANY(%s)",
            (list(OPEN_STATUSES),)).fetchone()["n"]
    counts = {"pending": 0, "approved": 0, "rejected": 0}
    for r in rows:
        key = _norm(r["status"])
        counts[key] = counts.get(key, 0) + r["n"]
    return {"mappings": counts, "total": sum(counts.values()), "pending_batches": pending_batches}


def get_batch(batch_id: str) -> dict:
    with store.connect() as conn:
        rows = conn.execute(BATCH_ROWS, {"env": ENVIRONMENT, "batch_id": batch_id}).fetchall()
    if not rows:
        raise BatchNotFoundError(f"review batch {batch_id} not found")
    mappings = [_row_mapping(r) for r in rows]
    first = mappings[0]
    return {
        "batch_id": batch_id,
        "origin": first["origin"],
        "status": _batch_status([r["status"] for r in rows]),
        "counts": _counts([r["status"] for r in rows]),
        "source_object_id": first["source_object_id"],
        "source_object_version": first["source_object_version"],
        "requested_by": first["requested_by"],
        "created_at": first["created_at"],
        "mapping": [m["mapping"] for m in mappings],
        "mappings": mappings,
        "graph_update": build_graph_update(mappings),
    }


def list_approved(since: str | None = None, source_object_id: str | None = None, limit: int = 100) -> list[dict]:
    """Approved mappings per batch, for the graph team. For schema-mapping batches `mapping` holds
    "table.column -> table.column" strings (the format of lineage/json_files/mapping.json); for rows from
    other jobs it is "<source field key> -> <target field key>". `origin` says which job wrote the row."""
    with store.connect() as conn:
        batch_ids = [r["batch_id"] for r in conn.execute(
            f"""WITH r AS ({REVIEW_SELECT})
               SELECT batch_id FROM r
               WHERE status = 'approved' AND decided_at IS NOT NULL
                 AND (%(src)s::varchar IS NULL OR source_object_id = %(src)s)
               GROUP BY batch_id
               HAVING (%(since)s::timestamptz IS NULL OR max(decided_at) >= %(since)s::timestamptz)
               ORDER BY max(decided_at) DESC LIMIT %(limit)s""",
            {"env": ENVIRONMENT, "src": source_object_id, "since": since, "limit": limit},
        ).fetchall()]
    out = []
    for batch_id in batch_ids:
        batch = get_batch(batch_id)
        approved = [m for m in batch["mappings"] if m["status"] == "approved"]
        out.append({
            "batch_id": batch_id,
            "origin": batch["origin"],
            "source_object_id": batch["source_object_id"],
            "source_object_version": batch["source_object_version"],
            "requested_by": batch["requested_by"],
            "approved_by": approved[0].get("decided_by") if approved else None,
            "approved_at": max((m.get("decided_at") or "") for m in approved) if approved else None,
            "mapping": [m["mapping"] for m in approved],
            "mappings": approved,
            "graph_update": build_graph_update(approved),
        })
    return out


# ---------------------------------------------------------------- decide
def decide_batch(batch_id: str, decision: str, decided_by: str, comment: str | None = None,
                 accepted_review_ids: list[int] | None = None) -> dict:
    """Record the reviewer's decision in PostgreSQL, in one transaction. Works for schema-mapping batches
    and for single rows from other jobs ("review-<id>").

    approved: the accepted rows (default: every open row) -> 'approved', the other open rows -> 'rejected'.
    rejected: every open row -> 'rejected'. Each decided row gets a review_decision row.
    """
    if decision not in ("approved", "rejected"):
        raise ValueError("decision must be approved or rejected")
    if not (decided_by or "").strip():
        raise ValueError("decided_by is required")
    with store.connect() as conn:
        rows = conn.execute(BATCH_ROWS + " FOR UPDATE OF q", {"env": ENVIRONMENT, "batch_id": batch_id}).fetchall()
        if not rows:
            raise BatchNotFoundError(f"review batch {batch_id} not found")
        open_rows = [r for r in rows if r["status"] in OPEN_STATUSES]
        if not open_rows:
            raise BatchAlreadyDecidedError(f"review batch {batch_id} has nothing left to decide")
        open_ids = [r["review_id"] for r in open_rows]
        if accepted_review_ids is not None:
            unknown = sorted(set(accepted_review_ids) - set(open_ids))
            if unknown:
                raise BatchSelectionError(f"review ids not open in batch {batch_id}: {unknown}")
        if decision == "rejected":
            accepted: set[int] = set()
        else:
            accepted = set(open_ids if accepted_review_ids is None else accepted_review_ids)
        for r in open_rows:
            row_decision = "approved" if r["review_id"] in accepted else "rejected"
            conn.execute("INSERT INTO review_decision (review_id, decision, decided_by, comment) VALUES (%s,%s,%s,%s)",
                         (r["review_id"], row_decision, decided_by, comment))
            conn.execute("UPDATE review_queue SET status=%s WHERE review_id=%s", (row_decision, r["review_id"]))
        conn.commit()
    accepted_rows = [_row_mapping(r) for r in open_rows if r["review_id"] in accepted]
    log.info("Review decision recorded batch_id=%s decision=%s accepted=%d of %d",
             batch_id, decision, len(accepted_rows), len(open_rows))
    return {"ok": True, "batch_id": batch_id, "decision": decision,
            "status": "approved" if accepted_rows else "rejected",
            "approved_review_ids": sorted(accepted), "rejected_review_ids": sorted(set(open_ids) - accepted),
            "mapping": [m["mapping"] for m in accepted_rows]}
