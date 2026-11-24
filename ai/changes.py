"""Detect renames and data-type changes by comparing the new JSON data with what is already in Neo4j."""

from query_gen import Q, run

SNAPSHOT_QUERY = Q(
    "changes_snapshot",
    "One row per Column that belongs to a Table (via HAS_COLUMN): the table's system, the table's name as `table`, the "
    "column's name, its data_type, sample = the value of its HAS_PROPERTY Property whose key is 'sample' (null if none; "
    "one value only), and already_renamed = true if the column has an outgoing RENAMED_TO relationship.",
    {}, ["system", "table", "name", "data_type", "sample", "already_renamed"])


def snapshot(session) -> dict:
    """{(system, table): {column: {data_type, sample, already_renamed}}} as currently stored."""
    snap = {}
    for r in run(session, SNAPSHOT_QUERY):
        snap.setdefault((r["system"], r["table"]), {})[r["name"]] = {
            "data_type": r["data_type"], "sample": r["sample"], "already_renamed": r["already_renamed"]}
    return snap


def new_state(built: list) -> dict:
    """Same shape as snapshot(), from the validated JSON documents."""
    state = {}
    for b in built:
        for side in ("source", "target"):
            system, _, tables = b[side]
            for tname, t in tables.items():
                state[(system, tname)] = {
                    c: {"data_type": d["data_type"], "sample": d["properties"].get("sample")}
                    for c, d in t["columns"].items()}
    return state


def diff(snap: dict, new: dict):
    """-> (changes, unpaired_removed). Compares only tables that already exist in the graph."""
    changes, unpaired = [], []
    for key, cols in new.items():
        if key not in snap:
            continue  # brand-new table: nothing to compare
        old = snap[key]
        system, table = key
        removed = {c: v for c, v in old.items() if c not in cols and not v["already_renamed"]}
        added = {c: v for c, v in cols.items() if c not in old}
        for oname, ov in list(removed.items()):  # pair a removed + an added column = rename
            cand = [a for a, av in added.items() if ov["sample"] is not None and av["sample"] == ov["sample"]]
            if not cand and len(removed) == 1 and len(added) == 1:
                cand = list(added)
            if len(cand) == 1:
                changes.append({"kind": "rename", "system": system, "table": table,
                                "old": oname, "new": cand[0], "column": cand[0]})
                removed.pop(oname); added.pop(cand[0])
        unpaired += [(system, table, c) for c in removed]
        for c, v in cols.items():  # same name, different type
            o = old.get(c)
            if o and o["data_type"] and v["data_type"] and o["data_type"] != v["data_type"]:
                changes.append({"kind": "type_change", "system": system, "table": table,
                                "column": c, "old": o["data_type"], "new": v["data_type"]})
    return changes, unpaired
