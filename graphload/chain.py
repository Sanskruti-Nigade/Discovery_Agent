"""Load a mapping file (ONLY the mapping file) as a lineage chain:

    source system -> source -> field -> subfield   ...MAPS_TO...   target -> target system

For  profile[].communication[].opt_in  (entity "contact") mapped to  followemail:

  (MDM:SourceSystem) -HAS_SOURCE_ENTITY-> (contact:SourceEntity) -HAS_SOURCE-> (profile[]:Source) -HAS_ENTITY-> (communication[]:Field) -HAS_ENTITY-> (opt_in:Subfield)
                                                                                                  |  MAPS_TO {transformation, reason, ...}
  (dynamics-365:TargetSystem) -HAS_TARGET_ENTITY-> (contact:TargetEntity) -HAS_TARGET-> (followemail:Target)

* the entity of the mapping (contact) is a node between the system and the paths;
* the first name of a path (person_id, profile[]) is a Source, hanging off the source entity; on the target side the
  first name is a Target, hanging off the target entity;
* the second name is a Field, anything deeper a Subfield; each is linked to its parent with HAS_ENTITY;
* a path with one name (person_id) IS the Source, so the mapping line starts there;
* "[]" is kept in a name, filters like [type='email'] are reduced to "[]";
* the line between the two ends is named by the mapping's own "relation" (maps_to -> MAPS_TO) and carries the
  transformation / reason / confidence / mapping id;
* a node mentioned by several mappings is ONE node; MERGE only; all values are query parameters (the file's text never
  becomes part of the Cypher); one transaction, so a failure writes nothing.

Usage:  python -m graphload.chain MAPPING.json [--dry-run]
"""
import logging
import re
from datetime import datetime, timezone
import sys

from neo4j import GraphDatabase

from .config import Limits, LoadError, Neo4jConfig
from .from_mapping import _entity_groups
from .reader import read_document

log = logging.getLogger("graphload")
logging.getLogger("neo4j").setLevel(logging.WARNING)  # hide harmless server notices
_REL = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,40}$")


def rel_type(relation) -> str:
    """'maps_to' -> 'MAPS_TO'. A relation that is not a plain word falls back to MAPS_TO (types cannot be parameters)."""
    r = str(relation or "maps_to").strip()
    return r.upper() if _REL.match(r) else "MAPS_TO"


def _segments(entity, ref) -> list:
    """'profile[].communication[type='email'].value' of entity 'contact' ->
    ['contact', 'profile[]', 'communication[]', 'value']."""
    ref = re.sub(r"\[[^\]]*\]", "[]", str(ref))  # a filter is not part of a name; the [] (an array) is kept
    ref = re.sub(r"\s*\(.*$", "", ref).strip()  # a trailing note like "(reference via ...)"
    return [str(entity)] + [x.strip() for x in ref.split(".") if x.strip()]


def read_chains(doc):
    """-> (source_system, target_system, rows, source_nodes, target_nodes).
    rows: one per mapping. *_nodes: {"items": {path: (label, name, parent_path|None, relationship, entity)}}."""
    parts = _entity_groups(doc)
    if not parts:
        raise LoadError("expected a mapping file with 'entity_mapping' (or 'mapping_set.entity_mappings')")
    source_system, target_system, groups = parts
    nodes = {k: {"items": {}} for k in ("s", "t")}
    rows = []

    def register(side, segs):
        """segs = [entity, first, ..., last]. -> the path of the node the mapping line attaches to."""
        items = nodes[side]["items"]
        for i in range(1, len(segs)):
            if i == 1:
                label, rel, parent = ("Source", "HAS_SOURCE", None) if side == "s" else ("Target", "HAS_TARGET", None)
            else:
                label, rel, parent = ("Field" if i == 2 else "Subfield"), "HAS_ENTITY", ".".join(segs[:i])
            items.setdefault(".".join(segs[:i + 1]), (label, segs[i], parent, rel, segs[0]))
        return ".".join(segs)

    for g in groups:
        se, te = g.get("source_entity"), g.get("target_entity")
        if not se or not te:
            raise LoadError("each mapping group needs 'source_entity' and 'target_entity'")
        for m in g.get("mappings", []):
            s, t = _segments(se, m.get("source", "")), _segments(te, m.get("target", ""))
            if len(s) < 2 or len(t) < 2:  # nothing after the entity name: not a field
                continue
            rows.append({
                "s": register("s", s), "t": register("t", t), "type": rel_type(m.get("relation")),
                "props": {k: v for k, v in {"relation": m.get("relation"), "transformation": m.get("transformation"),
                                            "reason": m.get("reason"), "confidence": m.get("confidence"),
                                            "mapping_id": g.get("mapping_id")}.items() if v is not None}})
    return str(source_system or "Source"), str(target_system or "Target"), rows, nodes["s"], nodes["t"]


_PARENT = "(p:Source OR p:Field OR p:Subfield)"


def _write_side(tx, system, label, nodes):
    tx.run(f"MERGE (:{label} {{name: $n}})", n=system).consume()
    ent_label, ent_rel = ("SourceEntity", "HAS_SOURCE_ENTITY") if label == "SourceSystem" else ("TargetEntity", "HAS_TARGET_ENTITY")
    entities = sorted({v[4] for v in nodes["items"].values()})  # system -> entity (e.g. MDM -> contact)
    tx.run(f"UNWIND $ents AS e MERGE (n:{ent_label} {{system: $sys, name: e}}) WITH n "
           f"MATCH (s:{label} {{name: $sys}}) MERGE (s)-[:{ent_rel}]->(n)", sys=system, ents=entities).consume()
    by_kind = {}  # (label, relationship) are fixed words chosen in the code above, never taken from the file
    for path, (lab, name, parent, rel, entity) in nodes["items"].items():
        by_kind.setdefault((lab, rel), []).append({"path": path, "name": name, "parent": parent, "entity": entity})
    for depth_first in ("Source", "Target", "Field", "Subfield"):  # parents are written before their children
        for (lab, rel), rows in by_kind.items():
            if lab != depth_first:
                continue
            tx.run(f"UNWIND $rows AS r MERGE (n:{lab} {{system: $sys, path: r.path}}) "
                   "SET n.name = r.name, n.entity = r.entity, n.status = 'active'", sys=system, rows=rows).consume()
            tx.run(f"UNWIND $rows AS r WITH r WHERE r.parent IS NOT NULL "
                   f"MATCH (p) WHERE {_PARENT} AND p.system = $sys AND p.path = r.parent "
                   f"MATCH (n:{lab} {{system: $sys, path: r.path}}) MERGE (p)-[:{rel}]->(n)",
                   sys=system, rows=rows).consume()
            tx.run(f"UNWIND $rows AS r WITH r WHERE r.parent IS NULL MATCH (s:{ent_label} {{system: $sys, name: r.entity}}) "
                   f"MATCH (n:{lab} {{system: $sys, path: r.path}}) MERGE (s)-[:{rel}]->(n)",
                   sys=system, rows=rows).consume()


_NODE = "(o:Source OR o:Target OR o:Field OR o:Subfield)"
_STRUCT = ["HAS_SOURCE", "HAS_TARGET", "HAS_ENTITY", "HAS_SOURCE_ENTITY", "HAS_TARGET_ENTITY", "RENAMED_TO", "CHANGED_TO"]


def _parent(path, entity):
    segs = path[len(entity) + 1:].split(".")
    return None if len(segs) < 2 else entity + "." + ".".join(segs[:-1])


def _renames(old_nodes, new_items, old_partners, new_partners) -> dict:
    """{old_path: new_path}. A node that disappeared is a RENAME of a new node when it is mapped to the same things
    (same mapping partners); then its parents are paired the same way (profile[] -> profile_m[])."""
    gone = [p for p in old_nodes if p not in new_items]
    added = [p for p in new_items if p not in old_nodes]
    pairs, taken = {}, set()
    for o in gone:
        po = old_partners.get(o)
        if not po:
            continue
        best, score = None, 0
        for n in added:
            sc = 0 if n in taken else len(po & new_partners.get(n, set()))
            if sc > score:
                best, score = n, sc
        if best:
            pairs[o] = best
            taken.add(best)
    for o, n in list(pairs.items()):
        eo, en = old_nodes[o], new_items[n][4]
        while True:
            po, pn = _parent(o, eo), _parent(n, en)
            if not po or not pn or po == pn or po in new_items or pn in old_nodes or po in pairs or pn in taken:
                break
            pairs[po] = pn
            taken.add(pn)
            o, n = po, pn
    return pairs


def _read_history(tx, system, side_label_check):
    return {r["path"]: r["ent"] for r in tx.run(
        f"MATCH (o) WHERE {_NODE} AND o.system = $sys AND o.entity IS NOT NULL AND coalesce(o.status,'active') = 'active' "
        "RETURN o.path AS path, o.entity AS ent", sys=system)}


def _snapshot(tx, ss, ts):
    """What the graph holds NOW (read before anything is written): active source nodes, target nodes, mapping lines."""
    old_s, old_t = _read_history(tx, ss, None), _read_history(tx, ts, None)
    old_edges = {}
    for r in tx.run("MATCH (a)-[m]->(b) WHERE a.system = $ss AND b.system = $ts AND coalesce(m.status,'active') = 'active' "
                    "AND NOT type(m) IN $skip RETURN a.path AS s, b.path AS t, type(m) AS ty, properties(m) AS p",
                    ss=ss, ts=ts, skip=_STRUCT):
        old_edges[(r["s"], r["t"], r["ty"])] = r["p"]
    return old_s, old_t, old_edges


def _record_history(tx, ss, ts, rows, src, tgt, snapshot) -> dict:
    """Compare the file with what is already in the graph. Nothing old is deleted: a renamed node is kept and linked
    (old)-[:RENAMED_TO]->(new); a node or mapping that is gone is kept and marked status = deleted / removed; a mapping
    whose transformation / reason changed keeps the old values as previous_*."""
    old_s, old_t, old_edges = snapshot
    new_edges = {(r["s"], r["t"], r["type"]): r for r in rows}

    def partners(pairs, i, j):
        d = {}
        for k in pairs:
            d.setdefault(k[i], set()).add(k[j])
        return d

    ren = {}
    for side, old, items, (a, b) in (("s", old_s, src["items"], (0, 1)), ("t", old_t, tgt["items"], (1, 0))):
        old_p = partners(old_edges, a, b)
        if side == "t":  # a source that was renamed is the same source: compare through the renames found so far
            old_p = {k: {ren["s"].get(x, x) for x in v} for k, v in old_p.items()}
        pr = _renames(old, items, old_p, partners(new_edges, a, b))
        ren[side] = pr
        tx.run(f"UNWIND $pairs AS r MATCH (o) WHERE {_NODE} AND o.system = $sys AND o.path = r.old "
                        f"MATCH (n) WHERE (n:Source OR n:Target OR n:Field OR n:Subfield) AND n.system = $sys AND n.path = r.new "
                        "MERGE (o)-[x:RENAMED_TO]->(n) SET x.at = toString(datetime()), o.status = 'renamed', o.renamed_to = n.name",
                        sys=ss if side == "s" else ts, pairs=[{"old": k, "new": v} for k, v in pr.items()]).consume()
        gone = [p for p in old if p not in items and p not in pr]
        tx.run(f"UNWIND $paths AS p MATCH (o) WHERE {_NODE} AND o.system = $sys AND o.path = p "
               "SET o.status = 'deleted', o.deleted_at = toString(datetime())",
               sys=ss if side == "s" else ts, paths=gone).consume()
        ren[side + "_deleted"] = gone
    removed = [{"s": k[0], "t": k[1], "ty": k[2]} for k in old_edges if k not in new_edges]
    tx.run("UNWIND $rm AS r MATCH (a {system: $ss, path: r.s})-[m]->(b {system: $ts, path: r.t}) WHERE type(m) = r.ty "
           "SET m.status = 'removed', m.removed_at = toString(datetime())", rm=removed, ss=ss, ts=ts).consume()
    changed = 0
    for k, r in new_edges.items():
        old = old_edges.get(k)
        diff = [f for f in ("transformation", "reason", "confidence") if old is not None and old.get(f) != r["props"].get(f)]
        if diff:
            changed += 1
            for f in diff:
                if old.get(f) is not None:
                    r["props"]["previous_" + f] = old[f]
                r["props"].setdefault(f, None)  # null removes the old value from the edge
            r["props"]["changed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {"renamed": {"source": ren["s"], "target": ren["t"]}, "deleted_nodes": len(ren["s_deleted"]) + len(ren["t_deleted"]),
            "mappings_removed": len(removed), "mappings_changed": changed,
            "deleted": ren["s_deleted"] + ren["t_deleted"],
            "new_nodes": [p for p in list(src["items"]) + list(tgt["items"]) if p not in old_s and p not in old_t
                          and p not in ren["s"].values() and p not in ren["t"].values()],
            "new_mappings": [f"{k[0]} -> {k[1]}" for k in new_edges if k not in old_edges and old_edges],
            "removed_mappings": [f"{k[0]} -> {k[1]}" for k in old_edges if k not in new_edges],
            "changed_mappings": [f"{k[0]} -> {k[1]}" for k, r in new_edges.items() if "changed_at" in r["props"]]}


def load_chains(path, dry_run=False, confirm=None) -> dict:
    ss, ts, rows, src, tgt = read_chains(read_document(path, Limits()))
    cfg = Neo4jConfig.from_env()
    by_type = {}
    for r in rows:
        by_type.setdefault(r["type"], []).append(r)
    driver = GraphDatabase.driver(cfg.uri, auth=(cfg.user, cfg.password))
    try:
        with driver.session(database=cfg.database) as session, session.begin_transaction() as tx:
            snapshot = _snapshot(tx, ss, ts)  # BEFORE writing, so the new file can be compared with the old state
            _write_side(tx, ss, "SourceSystem", src)
            _write_side(tx, ts, "TargetSystem", tgt)
            history = _record_history(tx, ss, ts, rows, src, tgt, snapshot)
            for rtype, batch in by_type.items():  # the type is validated above, so it is safe to place in the query
                tx.run("UNWIND $rows AS r "
                       "MATCH (a) WHERE (a:Source OR a:Field OR a:Subfield) AND a.system = $ss AND a.path = r.s MATCH (b) WHERE (b:Target OR b:Field OR b:Subfield) AND b.system = $ts AND b.path = r.t "
                       f"MERGE (a)-[m:{rtype}]->(b) SET m += r.props, m.status = 'active', m.removed_at = null", ss=ss, ts=ts, rows=batch).consume()
            written = tx.run("MATCH ()-[m]->() WHERE type(m) IN $types RETURN count(m) AS c",
                             types=list(by_type)).single()["c"]
            result = {"mapping_lines_in_graph": written, "source_system": ss, "target_system": ts, "mappings": len(rows),
                      "source_nodes": len(src["items"]),
                      "target_nodes": len(tgt["items"]),
                      "relationship_types": {k: len(v) for k, v in by_type.items()}, "history": history, "dry_run": dry_run}
            if confirm is not None and not dry_run:
                dry_run = result["dry_run"] = not confirm(result)  # no -> nothing is written
            if dry_run:
                tx.rollback()
            else:
                tx.commit()
        return result
    finally:
        driver.close()


def _show(result):
    """Print what WOULD change, then ask. Returns True only for 'yes'."""
    h = result["history"]
    def block(title, items, limit=15):
        if items:
            print(f"\n{title} ({len(items)})")
            for x in items[:limit]:
                print("   ", x)
            if len(items) > limit:
                print(f"    ... and {len(items) - limit} more")
    print(f"\nChanges for {result['source_system']} -> {result['target_system']}")
    renamed = {**h["renamed"]["source"], **h["renamed"]["target"]}
    block("RENAMED (old kept, linked with RENAMED_TO)", [f"{o}  =>  {n}" for o, n in renamed.items()])
    block("DELETED from the file (old kept, marked deleted)", h["deleted"])
    block("NEW nodes", h["new_nodes"])
    block("NEW mappings", h["new_mappings"])
    block("REMOVED mappings (old kept, marked removed)", h["removed_mappings"])
    block("CHANGED mappings (old values kept as previous_*)", h["changed_mappings"])
    if not (renamed or h["deleted"] or h["new_nodes"] or h["new_mappings"] or h["removed_mappings"] or h["changed_mappings"]):
        print("\nNo changes.")
        return False
    return input("\nApply these changes? (yes/no): ").strip().lower() in ("yes", "y")


if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(message)s")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        sys.exit("usage: python -m graphload.chain MAPPING.json [--dry-run] [--yes]")
    try:
        res = load_chains(args[0], "--dry-run" in sys.argv, None if "--yes" in sys.argv else _show)
        print("\nApplied." if not res["dry_run"] else "\nNothing was changed.")
    except LoadError as e:
        sys.exit(f"ERROR: {e}")
