"""Compare the incoming data with what Neo4j already holds for the same system, and classify the change.

  initial  nothing loaded yet for this system        -> just load
  none     identical                                   -> just load (idempotent)
  minor    a few columns/tables added, removed, retyped -> show the changes, ask the user to confirm
  major    most of the structure is different          -> ask whether to REMAP EVERYTHING
"""
import os
from dataclasses import dataclass, field

from neo4j import GraphDatabase

from .config import LoadError, Neo4jConfig
from .model import GraphModel
from .schema import Analysis


def major_ratio() -> float:
    try:
        return float(os.getenv("GRAPHLOAD_FULL_REMAP_RATIO", "0.5"))
    except ValueError:
        raise LoadError("GRAPHLOAD_FULL_REMAP_RATIO must be a number between 0 and 1")


@dataclass
class SystemDiff:
    system: str
    old_tables: int = 0
    old_columns: int = 0
    added_tables: list = field(default_factory=list)
    removed_tables: list = field(default_factory=list)
    added_columns: list = field(default_factory=list)    # (table, column)
    removed_columns: list = field(default_factory=list)  # (table, column)
    type_changes: list = field(default_factory=list)     # (table, column, old, new)
    mappings_affected: int = 0                            # existing MAPS_TO links that would be removed

    @property
    def changed(self) -> int:
        return len(self.added_columns) + len(self.removed_columns) + len(self.type_changes)

    @property
    def ratio(self) -> float:
        base = self.old_columns + len(self.added_columns)  # |old ∪ new| columns
        return self.changed / base if base else 0.0

    @property
    def kind(self) -> str:
        if self.old_columns == 0 and self.old_tables == 0:
            return "initial"
        if not (self.changed or self.added_tables or self.removed_tables):
            return "none"
        return "major" if self.ratio >= major_ratio() else "minor"


@dataclass
class ChangePlan:
    systems: list

    @property
    def kind(self) -> str:
        kinds = {s.kind for s in self.systems}
        for k in ("major", "minor"):
            if k in kinds:
                return k
        return "none" if "none" in kinds else "initial"

    def to_dict(self) -> dict:
        return {"kind": self.kind, "systems": [
            {"system": s.system, "kind": s.kind, "changed_ratio": round(s.ratio, 3),
             "added_tables": s.added_tables, "removed_tables": s.removed_tables,
             "added_columns": [f"{t}.{c}" for t, c in s.added_columns],
             "removed_columns": [f"{t}.{c}" for t, c in s.removed_columns],
             "type_changes": [f"{t}.{c}: {o} -> {n}" for t, c, o, n in s.type_changes],
             "mappings_affected": s.mappings_affected} for s in self.systems]}

    def describe(self, limit: int = 8) -> str:
        out = []
        for s in self.systems:
            if s.kind in ("initial", "none"):
                continue
            out.append(f"System '{s.system}': {s.changed} of ~{s.old_columns + len(s.added_columns)} columns changed "
                       f"({s.ratio:.0%}), {len(s.added_tables)} table(s) added, {len(s.removed_tables)} removed")
            for title, items in (("+ added", [f"{t}.{c}" for t, c in s.added_columns]),
                                 ("- removed", [f"{t}.{c}" for t, c in s.removed_columns]),
                                 ("~ retyped", [f"{t}.{c}: {o} -> {n}" for t, c, o, n in s.type_changes]),
                                 ("+ new tables", s.added_tables), ("- dropped tables", s.removed_tables)):
                if items:
                    out.append(f"  {title} ({len(items)}): " + ", ".join(items[:limit]) + (" ..." if len(items) > limit else ""))
            if s.mappings_affected:
                out.append(f"  {s.mappings_affected} existing mapping link(s) touch removed columns")
        return "\n".join(out)


def diff_system(a: Analysis, old_tables: set, old_columns: dict) -> SystemDiff:
    """Pure function (unit-testable): old_columns = {(table, column): data_type}."""
    new_tables = {t["name"] for t in a.tables}
    new_cols = {(c["table"], c["name"]): c["data_type"] for c in a.columns}
    d = SystemDiff(a.system, len(old_tables), len(old_columns))
    d.added_tables = sorted(new_tables - old_tables)
    d.removed_tables = sorted(old_tables - new_tables)
    d.added_columns = sorted(new_cols.keys() - old_columns.keys())
    d.removed_columns = sorted(old_columns.keys() - new_cols.keys())
    d.type_changes = sorted((t, c, old_columns[(t, c)], new_cols[(t, c)]) for (t, c) in new_cols.keys() & old_columns.keys()
                            if old_columns[(t, c)] and new_cols[(t, c)] and old_columns[(t, c)] != new_cols[(t, c)])
    return d


def compute_plan(analyses: list[Analysis], model: GraphModel, cfg: Neo4jConfig) -> ChangePlan:
    """Read what is currently stored for each system and diff it against the incoming data (read-only)."""
    t_lbl, c_lbl = f"`{model.table_label}`", f"`{model.column_label}`"
    driver = GraphDatabase.driver(cfg.uri, auth=(cfg.user, cfg.password), connection_timeout=15)
    diffs = []
    try:
        with driver.session(database=cfg.database, default_access_mode="READ") as s:
            for a in analyses:
                tables = {r["n"] for r in s.run(f"MATCH (t:{t_lbl} {{system: $s}}) RETURN t.name AS n", s=a.system)}
                cols = {(r["t"], r["n"]): r["d"] for r in s.run(
                    f"MATCH (c:{c_lbl} {{system: $s}}) RETURN c.table AS t, c.name AS n, c.data_type AS d", s=a.system)}
                d = diff_system(a, tables, cols)
                if d.removed_columns:
                    d.mappings_affected = s.run(
                        f"UNWIND $rows AS r MATCH (c:{c_lbl} {{system: $s, table: r.t, name: r.n}})-"
                        f"[x:`{model.maps_to}`]-() RETURN count(DISTINCT x) AS n",
                        s=a.system, rows=[{"t": t, "n": n} for t, n in d.removed_columns]).single()["n"]
                diffs.append(d)
    except Exception as e:
        raise LoadError(f"could not read the current graph ({type(e).__name__})") from None
    finally:
        driver.close()
    return ChangePlan(diffs)
