"""Read and validate lineage JSON (one object or a list of objects). Contains no Cypher: the LLM writes it (ai/sync.py)."""
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")  # reuse the project's .env
load_dotenv()

URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
USER = os.getenv("NEO4J_USERNAME") or os.getenv("NEO4J_USER", "neo4j")
PASSWORD = os.getenv("NEO4J_PASSWORD", "")
DB = os.getenv("NEO4J_DATABASE", "neo4j")


class LineageError(ValueError):
    pass


def norm_tables(side: dict, role: str) -> dict:
    """-> {table: {"schema": str|None, "columns": {col: {"data_type":..., "properties": {...}}}}}"""
    if not side.get("system") or not isinstance(side.get("tables"), dict):
        raise LineageError(f"'{role}' needs 'system' and a 'tables' object")
    out = {}
    for tname, tdef in side["tables"].items():
        schema, cols = None, tdef
        if isinstance(tdef, dict):  # optional form: {"schema": "...", "columns": [...]}
            schema, cols = tdef.get("schema"), tdef.get("columns", [])
        out[tname] = {"schema": schema, "columns": {}}
        for c in cols:
            c = {"name": c} if isinstance(c, str) else c  # column = "name" or {name, data_type, properties}
            if c["name"] in out[tname]["columns"]:
                raise LineageError(f"{role}: duplicate column {tname}.{c['name']}")
            extra = {k: v for k, v in c.items() if k not in ("name", "data_type", "properties")}  # e.g. pk, fk
            out[tname]["columns"][c["name"]] = {
                "data_type": c.get("data_type"),
                "properties": {**c.get("properties", {}), **extra},
            }
    return out


def validate_and_build(doc: dict, idx: int) -> dict:
    """Fail loudly on any bad reference; nothing is written until every document passes."""
    where = f"document #{idx}"
    for k in ("source", "target", "mapping"):
        if k not in doc:
            raise LineageError(f"{where}: missing '{k}'")
    src, tgt = norm_tables(doc["source"], "source"), norm_tables(doc["target"], "target")
    maps = []
    for m in doc["mapping"]:
        edge = []
        for side, tables, system in (
            ("from", src, doc["source"]["system"]),
            ("to", tgt, doc["target"]["system"]),
        ):
            ref = m.get(side, "")
            t, _, c = ref.partition(".")
            if t not in tables:
                raise LineageError(
                    f"{where}: mapping '{side}': '{ref}' -> unknown table '{t}' "
                    f"in system '{system}' (known: {sorted(tables)})")
            if c not in tables[t]["columns"]:
                raise LineageError(
                    f"{where}: mapping '{side}': '{ref}' -> unknown column '{c}' "
                    f"in table '{t}' (known: {sorted(tables[t]['columns'])})")
            edge.append((system, t, c))
        maps.append({
            "from": edge[0],
            "to": edge[1],
            "name": m.get("mapping_name") or f"{edge[0][1]}.{edge[0][2]}->{edge[1][1]}.{edge[1][2]}",
            "transformation": m.get("transformation"),
        })
    return {
        "source": (doc["source"]["system"], doc["source"].get("type", "source"), src),
        "target": (doc["target"]["system"], doc["target"].get("type", "target"), tgt),
        "maps": maps,
    }


def assemble(systems: dict, mappings: list) -> list:
    """Turn source files + system-level mapping entries into lineage documents.
    mapping entry: {"from": system, "to": system, "fields": {src_col: tgt_col},
                    optional "from_table"/"to_table" when a system has several tables}"""
    def side(system, table, role):
        if system not in systems:
            raise LineageError(f"mapping {role} system '{system}' has no source file (known: {sorted(systems)})")
        tables = systems[system]
        t = table or (next(iter(tables)) if len(tables) == 1 else None)
        if t is None:
            raise LineageError(f"system '{system}' has tables {sorted(tables)}; set '{role}_table' in the mapping")
        cols = []
        for c, v in tables[t].items():  # v = sample value, or {"value": ..., "data_type": ...} if the file states a type
            val, dtype = (v.get("value"), v.get("data_type")) if isinstance(v, dict) else (v, None)
            cols.append({"name": c, "data_type": dtype, "properties": {} if val is None else {"sample": val}})
        return {"system": system, "tables": {t: cols}}, t

    docs = []
    for m in mappings:
        src, st = side(m.get("from"), m.get("from_table"), "from")
        tgt, tt = side(m.get("to"), m.get("to_table"), "to")
        docs.append({"source": src, "target": tgt,
                     "mapping": [{"from": f"{st}.{a}", "to": f"{tt}.{b}"} for a, b in m.get("fields", {}).items()]})
    return docs


def string_mappings(systems: dict, entries: list) -> list:
    """"table.col -> table.col" strings (no system names) -> system-level mapping entries for assemble().
    Each table name must belong to exactly one system file."""
    owner = {}
    for sysname, tables in systems.items():
        for t in tables:
            owner.setdefault(t, []).append(sysname)

    def locate(ref, entry):
        t, _, c = ref.strip().partition(".")
        if len(owner.get(t, [])) != 1:
            raise LineageError(f"mapping '{entry}': table '{t}' must be in exactly one system file "
                               f"(found in {owner.get(t, [])})")
        return owner[t][0], t, c

    groups = {}
    for e in entries:
        a, sep, b = e.partition("->")
        if not sep:
            raise LineageError(f"mapping '{e}': expected 'table.column -> table.column'")
        (fs, ft, fc), (ts, tt, tc) = locate(a, e), locate(b, e)
        fields = groups.setdefault((fs, ft, ts, tt), {})
        if fc in fields:
            raise LineageError(f"mapping '{e}': {ft}.{fc} is mapped twice into {tt}")
        fields[fc] = tc
    return [{"from": fs, "from_table": ft, "to": ts, "to_table": tt, "fields": f}
            for (fs, ft, ts, tt), f in groups.items()]


def build_all(paths) -> list:
    """Read every JSON file (or folder), assemble and validate all documents; nothing is written."""
    files = []
    for p in map(Path, paths):  # a path may be a file or a folder of *.json files
        files += sorted(p.glob("*.json")) if p.is_dir() else [p]
    if not files:
        raise LineageError(f"no .json files found in {paths}")
    docs, systems, mappings, sys_files, map_files, str_maps = [], {}, [], {}, [], []
    for p in files:
        data = json.loads(p.read_text(encoding="utf-8"))
        for d in data if isinstance(data, list) else [data]:
            if "source" in d and "target" in d:  # full lineage document
                docs.append(d)
            elif "system" in d and isinstance(d.get("tables"), dict):  # {system, type, tables: {t: [columns]}}
                sys_files[d["system"]] = d
            elif "source_system" in d and "target_system" in d:        # {source_system, target_system, mapping: [table.col]}
                map_files.append(d)
            elif "mapping" in d:                 # older system-level mapping file
                for m in d["mapping"]:
                    (str_maps if isinstance(m, str) else mappings).append(m)
            else:                                # older source file: {system: {table: {column: sample}}}
                systems.update(d)
    for m in map_files:  # pair each mapping file with its two system files
        for role in ("source_system", "target_system"):
            if m[role] not in sys_files:
                raise LineageError(f"mapping '{m.get('mapping_name', '?')}': {role} '{m[role]}' has no system file "
                                   f"(known: {sorted(sys_files)})")
        name = m.get("mapping_name")
        docs.append({"source": sys_files[m["source_system"]], "target": sys_files[m["target_system"]],
                     "mapping": [{**e, **({"mapping_name": name} if name and "mapping_name" not in e else {})}
                                 for e in m["mapping"]]})
    mappings += string_mappings(systems, str_maps)
    docs += assemble(systems, mappings)
    return [validate_and_build(d, i + 1) for i, d in enumerate(docs)]  # validate ALL first


def main(paths):
    """Validate only. Writing to Neo4j is done by the LLM-generated Cypher in ai/sync.py."""
    built = build_all(paths)
    print(f"Validated {len(built)} document(s), {sum(len(b['maps']) for b in built)} mapping(s). "
          "Nothing written; load with: python ai/sync.py")


if __name__ == "__main__":
    try:
        main(sys.argv[1:] or [str(Path(__file__).parent / "json_files")])
    except LineageError as e:
        sys.exit(f"VALIDATION ERROR: {e}")
