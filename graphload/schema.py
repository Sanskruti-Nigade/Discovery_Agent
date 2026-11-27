"""Turn ANY nested document into tables and columns. Pure Python, no database, no hardcoded key names.

Rules
  * an object, or an array of objects (merged across ALL items), is a Table named by its key
  * a scalar or an array of scalars is a Column of the table that directly contains it
  * ids are "<system>:<escaped.dotted.path>"; a literal "." inside a key is escaped as "\\." so ids never collide
"""
import re
from dataclasses import dataclass, field

from .config import Limits, LoadError

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


# ----------------------------- paths -----------------------------

def esc(segment: str) -> str:
    return segment.replace("\\", "\\\\").replace(".", "\\.")


def join_path(segments) -> str:
    return ".".join(esc(s) for s in segments)


def split_path(ref: str) -> list[str]:
    """Inverse of join_path: split on unescaped dots."""
    out, cur, i = [], [], 0
    while i < len(ref):
        ch = ref[i]
        if ch == "\\" and i + 1 < len(ref):
            cur.append(ref[i + 1])
            i += 2
            continue
        if ch == ".":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    out.append("".join(cur))
    return out


# ----------------------------- shape merging -----------------------------

@dataclass
class Leaf:
    """What we learned about one scalar / array-of-scalars field across every place it appears."""
    types: set = field(default_factory=set)
    sample: object = None
    is_array: bool = False

    @property
    def data_type(self) -> str | None:
        if self.is_array:
            return "array"
        t = self.types - {None}
        if not t:
            return None
        if len(t) == 1:
            return next(iter(t))
        return "number" if t <= {"integer", "number"} else "mixed"


def scalar_type(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    s = str(v)
    if _DATE.match(s):
        return "date"
    if _TS.match(s):
        return "timestamp"
    return "string"


def _flatten(values):
    for v in values:
        if isinstance(v, list):
            yield from _flatten(v)
        else:
            yield v


def merge(values: list):
    """Collapse many samples (e.g. every item of an array) into ONE shape: a dict with the union of keys
    (recursively), or a Leaf. Recursion depth is bounded by Limits.max_depth, checked before we get here."""
    had_list = any(isinstance(v, list) for v in values)
    flat = list(_flatten(values))
    dicts = [v for v in flat if isinstance(v, dict)]
    if dicts:  # objects win over scalars when a key is inconsistent
        keys = list(dict.fromkeys(k for d in dicts for k in d))
        return {k: merge([d[k] for d in dicts if k in d]) for k in keys}
    leaf = Leaf(is_array=had_list)
    for v in flat:
        leaf.types.add(scalar_type(v))
        if leaf.sample is None and v is not None:
            leaf.sample = v
    return leaf


# ----------------------------- analysis -----------------------------

@dataclass
class Analysis:
    system: str
    role: str  # "source" | "target"
    tables: list = field(default_factory=list)
    columns: list = field(default_factory=list)
    owns: list = field(default_factory=list)        # (system_id, table_id)
    has_table: list = field(default_factory=list)   # (parent_table_id, child_table_id)
    has_column: list = field(default_factory=list)  # (table_id, column_id)

    @property
    def system_id(self) -> str:
        return f"{self.system}:"


def _clean(name, limits: Limits) -> str:
    name = str(name)
    if not name or len(name) > limits.max_name_len:
        raise LoadError(f"key is empty or longer than {limits.max_name_len} characters: {name[:30]!r}")
    return name


def analyse(system: str, role: str, data, limits: Limits, include_samples: bool = False) -> Analysis:
    """Walk the document iteratively (no recursion limit) and describe its tables and columns."""
    if isinstance(data, list):
        data = {"root": data}  # a root array is one anonymous table
    if not isinstance(data, dict):
        raise LoadError(f"{system}: the document root must be an object or an array")
    a = Analysis(system, role)

    def sid(segs):
        return f"{system}:{join_path(segs)}"

    def add_table(segs, parent):
        if len(a.tables) >= limits.max_tables:
            raise LoadError(f"more than {limits.max_tables} tables")
        a.tables.append({"id": sid(segs), "name": segs[-1], "path": ".".join(segs), "system": system})
        (a.has_table if parent else a.owns).append((sid(parent) if parent else a.system_id, sid(segs)))

    def add_column(table_segs, name, leaf):
        if len(a.columns) >= limits.max_columns:
            raise LoadError(f"more than {limits.max_columns} columns")
        segs = (*table_segs, name)
        row = {"id": sid(segs), "name": name, "table": table_segs[-1], "path": ".".join(segs),
               "system": system, "data_type": leaf.data_type}
        if include_samples and leaf.sample is not None:
            row["sample"] = str(leaf.sample)[:200]
        a.columns.append(row)
        a.has_column.append((sid(table_segs), row["id"]))

    stack, root_ready = [], False
    for key, value in data.items():
        key, shaped = _clean(key, limits), merge([value])
        if isinstance(shaped, dict):
            stack.append(((key,), shaped, None))
        else:  # top-level scalar: lives in an implicit "root" table
            if not root_ready:
                add_table(("root",), None)
                root_ready = True
            add_column(("root",), key, shaped)
    while stack:
        segs, obj, parent = stack.pop()
        add_table(segs, parent)
        for key, v in obj.items():
            key = _clean(key, limits)
            if isinstance(v, dict):
                stack.append(((*segs, key), v, segs))
            else:
                add_column(segs, key, v)
    _reject_name_collisions(a)
    return a


def _reject_name_collisions(a: Analysis) -> None:
    """The graph identifies a table by (system, name). Two tables with the same name at different paths would be
    silently merged into one node, so refuse and say exactly where."""
    seen: dict = {}
    for t in a.tables:
        seen.setdefault(t["name"], []).append(t["path"])
    clashes = {n: p for n, p in seen.items() if len(p) > 1}
    if clashes:
        shown = "; ".join(f"'{n}' at {', '.join(p[:3])}" for n, p in list(clashes.items())[:5])
        raise LoadError(f"{a.system}: tables are identified by (system, name) but these names repeat: {shown}. "
                        "Rename one of them in the source, or load the parts as separate systems.")
