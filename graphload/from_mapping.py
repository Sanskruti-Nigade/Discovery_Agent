"""Load a lineage graph from the MAPPING FILE ALONE.

The mapping file already names the systems, lists every source and target table with its columns, and holds the
column-to-column mappings, so no separate source / target data files are needed:

  {"source_system": "MDM", "target_system": "dynamics-365-sales",
   "source_columns": {"<table>": ["col", ...], ...},
   "target_columns": {"<table>": ["col", ...], ...},
   "mapping_groups": [{"mapping_id": ..., "mappings": [{"source": "table.col", "target": "table.col", "rule": ...}]}]}

The LLM is used for ONE small judgement call: grouping the source tables into the logical sources they belong to
(e.g. mdm_contact_* -> "contact", mdm_account* -> "account"), so the graph reads System -> source -> table -> field.
If the LLM is unavailable the tables are grouped by their name prefix instead, so the load never depends on it.
"""
import json
import logging
import re
import sys
from pathlib import Path

from .config import LoadError

log = logging.getLogger("graphload")

_PROMPT = ("You group database table names into the logical sources (business entities) they belong to. "
           "Input: a JSON list of table names. Output ONLY JSON: {\"groups\": {\"<source name>\": [\"<table>\", ...]}}. "
           "Every table must appear in exactly one group. Use short lowercase names taken from the table names "
           "(for example mdm_contact_profile and mdm_contact_note -> \"contact\"). Do not invent tables.")


def is_mapping_only(doc) -> bool:
    return (isinstance(doc, dict) and isinstance(doc.get("source_columns"), dict)
            and isinstance(doc.get("target_columns"), dict) and isinstance(doc.get("mapping_groups"), list))


def _heuristic_groups(tables: list[str]) -> dict:
    """Group by the first two name tokens (mdm_contact_x, mdm_contact_y -> 'contact'); a lone table stays alone."""
    by_prefix: dict = {}
    for t in tables:
        parts = t.split("_")
        by_prefix.setdefault("_".join(parts[:2]) if len(parts) > 2 else t, []).append(t)
    groups = {}
    for prefix, members in by_prefix.items():
        name = prefix.split("_", 1)[1] if "_" in prefix else prefix
        groups.setdefault(name, []).extend(members)
    return groups


def _llm_groups(tables: list[str]) -> dict | None:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ai"))
        from llm_client import chat  # the project's provider-agnostic client
        reply = chat(_PROMPT, json.dumps(tables), max_tokens=2000)
        match = re.search(r"\{.*\}", reply, re.S)
        groups = json.loads(match.group(0))["groups"]
        flat = [t for members in groups.values() for t in members]
        if sorted(flat) != sorted(tables):  # must be an exact partition: no invented, dropped or repeated tables
            raise ValueError("the LLM grouping did not cover the tables exactly")
        return {str(k): list(v) for k, v in groups.items()}
    except Exception as e:  # deliberately broad: the LLM step is optional
        log.warning("LLM grouping unavailable (%s: %s); grouping by table-name prefix", type(e).__name__, str(e)[:120])
        return None


_RESTATE = ("You restate a data-mapping file in ONE fixed shape. Output ONLY JSON: "
            "{\"source_system\": \"...\", \"target_system\": \"...\", \"entity_mappings\": [{\"mapping_id\": \"...\", "
            "\"source_entity\": \"...\", \"target_entity\": \"...\", \"mappings\": [{\"source\": \"<field path>\", "
            "\"target\": \"<field path>\", \"transformation\": \"<if stated>\"}]}]}. Copy every field mapping exactly "
            "as written; never invent, merge or drop any.")


def _llm_entity_groups(doc):
    """Ask the LLM to restate an unrecognised mapping file; None if it cannot (the caller then raises a clear error)."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ai"))
        from llm_client import chat
        reply = chat(_RESTATE, json.dumps(doc), max_tokens=16000)
        data = json.loads(re.search(r"\{.*\}", reply, re.S).group(0))
        parts = (data.get("source_system"), data.get("target_system"), data["entity_mappings"])
        n = sum(len(g.get("mappings", [])) for g in parts[2])
        log.info("LLM restated the mapping file: %d mapping(s)", n)
        return parts if n else None
    except Exception as e:  # deliberately broad: fall back to a clear error
        log.warning("LLM could not restate the mapping file (%s: %s)", type(e).__name__, str(e)[:120])
        return None


def _entity_groups(doc):
    """The mapping shapes that carry ONLY mappings: {"entity_mapping": {...}} or {"mapping_set": {"entity_mappings":
    [...]}}. -> (source_system, target_system, [group]) or None."""
    if isinstance(doc, dict) and isinstance(doc.get("entity_mapping"), dict):
        e = doc["entity_mapping"]
        return e.get("source_system"), e.get("target_system"), [e]
    ms = doc.get("mapping_set") if isinstance(doc, dict) else None
    if isinstance(ms, dict) and isinstance(ms.get("entity_mappings"), list):
        return ms.get("source_system"), ms.get("target_system"), ms["entity_mappings"]
    return None


def _clean_path(ref) -> str:
    """'profile[].communication[type='email'].value' -> 'profile.communication.value'; notes in (...) are dropped."""
    ref = re.sub(r"\[[^\]]*\]", "", str(ref))
    return re.sub(r"\s*\(.*$", "", ref).strip()


def _put(tree: dict, segments: list) -> None:
    """Record a field path in a nested dict: inner segments are (nested) tables, the last one is a column."""
    node = tree
    for seg in segments[:-1]:
        if not isinstance(node.get(seg), dict):
            node[seg] = {}
        node = node[seg]
    node.setdefault(segments[-1], None)


def _to_records(node):
    """{'col': None, 'child': {...}} -> one sample record, with child tables as arrays (graphload's table rule)."""
    return {k: ([_to_records(v)] if isinstance(v, dict) else None) for k, v in node.items()}


def build_from_entities(parts):
    """Derive source tables/columns, target tables/columns and the mappings from the mapping paths alone."""
    source_system, target_system, groups = parts
    src_tree, tgt_tree, entries = {}, {}, []
    for g in groups:
        se, te = g.get("source_entity"), g.get("target_entity")
        if not se or not te:
            raise LoadError("each mapping group needs 'source_entity' and 'target_entity'")
        for m in g.get("mappings", []):
            s, t = _clean_path(m.get("source", "")), _clean_path(m.get("target", ""))
            if not s or not t:
                continue
            _put(src_tree.setdefault(se, {}), s.split("."))
            tgt_tree.setdefault(te, {}).setdefault(t, None)  # target names stay whole, e.g. 'x.accountnumber'
            entries.append({"from": f"{se}.{s}", "to": f"{te}.{t}", "name": g.get("mapping_id") or f"{se}_to_{te}",
                            "transformation": m.get("transformation") or m.get("rule"), "soft": True})
    source = {entity: [_to_records(tree)] for entity, tree in src_tree.items()}
    target = {entity: [{c: None for c in cols}] for entity, cols in tgt_tree.items()}
    return source, target, entries, str(source_system or "Source"), str(target_system or "Target")


def build(doc: dict, use_llm: bool = True):
    """-> (source_document, target_document, mapping_document, source_name, target_name) ready for graphload.load."""
    parts = _entity_groups(doc)
    if parts:
        return build_from_entities(parts)
    if not is_mapping_only(doc) and use_llm:
        parts = _llm_entity_groups(doc)  # unknown shape: let the LLM restate it in the known one
        if parts:
            return build_from_entities(parts)
    if not is_mapping_only(doc):
        raise LoadError("this is not a mapping-only file: it needs 'source_columns', 'target_columns' and "
                        "'mapping_groups'")
    src_tables = {t: list(cols) for t, cols in doc["source_columns"].items()}
    groups = (_llm_groups(list(src_tables)) if use_llm else None) or _heuristic_groups(list(src_tables))
    log.info("source groups: %s", {g: len(m) for g, m in groups.items()})

    def table_doc(cols):
        return [{c: None for c in cols}]  # one sample record: every column present, no values

    source = {g: {t: table_doc(src_tables[t]) for t in members} for g, members in groups.items()}
    target = {t: table_doc(cols) for t, cols in doc["target_columns"].items()}
    return (source, target, {"mapping_groups": doc["mapping_groups"]},
            str(doc.get("source_system") or "Source"), str(doc.get("target_system") or "Target"))
