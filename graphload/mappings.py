"""Parse mapping files and resolve every endpoint to a real Column. All problems are reported together, before any write."""
import difflib
import logging
import re

from .config import Limits, LoadError
from .schema import Analysis, split_path


def _strip_indexes(ref: str) -> str:
    return re.sub(r"\[\d*\]", "", ref).strip()  # "A.B[].c" and "A.B[0].c" -> "A.B.c"


def _clean_ref(ref: str) -> str:
    """'profile[].communication[type='email' && primary=true].value' -> 'profile.communication.value';
    'sn_joblevel (reference via _x)' -> 'sn_joblevel'. Filters and notes are not part of a column's identity."""
    ref = re.sub(r"\[[^\]]*\]", "", str(ref))
    ref = re.sub(r"\s*\(.*$", "", ref)
    return ref.strip()


def parse_entity_mappings(doc, limits: Limits) -> list[dict]:
    """The {"mapping_set": {"entity_mappings": [{mapping_id, source_entity, target_entity, mappings: [{source, target,
    transformation}]}]}} shape. Endpoints are '<entity>.<path>'. 'soft' entries that cannot be resolved are skipped
    (and reported) instead of failing the whole load."""
    out = []
    for g in doc["mapping_set"]["entity_mappings"]:
        for m in g.get("mappings", []):
            out.append({"from": f"{g['source_entity']}.{_clean_ref(m.get('source', ''))}",
                        "to": f"{g['target_entity']}.{_clean_ref(m.get('target', ''))}",
                        "name": g.get("mapping_id"), "transformation": m.get("transformation"), "soft": True})
    if len(out) > limits.max_mappings:
        raise LoadError(f"more than {limits.max_mappings} mappings")
    return out


def parse_mapping_groups(doc, limits: Limits) -> list[dict]:
    """{"mapping_groups": [{mapping_id, mappings: [{source: 'table.column', target: 'table.column', rule}]}]}.
    Unresolvable entries are skipped and reported, not fatal."""
    out = []
    for g in doc["mapping_groups"]:
        for m in g.get("mappings", []):
            out.append({"from": _clean_ref(m.get("source", "")), "to": _clean_ref(m.get("target", "")),
                        "name": g.get("mapping_id"), "transformation": m.get("rule") or m.get("transformation"),
                        "soft": True})
    if len(out) > limits.max_mappings:
        raise LoadError(f"more than {limits.max_mappings} mappings")
    return out


def parse(doc, limits: Limits) -> list[dict]:
    """Accepts {"mapping": [...]} or a bare list. Entries: "src.path -> tgt.path"  or  {from, to, mapping_name, transformation}."""
    if isinstance(doc, list) and doc and all(isinstance(x, dict) and x.get("soft") for x in doc):
        return doc  # already-resolved entries from the mapping-only builder
    if isinstance(doc, dict) and isinstance(doc.get("mapping_set"), dict) and "entity_mappings" in doc["mapping_set"]:
        return parse_entity_mappings(doc, limits)
    if isinstance(doc, dict) and isinstance(doc.get("mapping_groups"), list):
        return parse_mapping_groups(doc, limits)
    items = doc.get("mapping", []) if isinstance(doc, dict) else doc
    if not isinstance(items, list):
        raise LoadError("'mapping' must be a list")
    if len(items) > limits.max_mappings:
        raise LoadError(f"more than {limits.max_mappings} mappings")
    out = []
    for m in items:
        if isinstance(m, str):
            src, sep, tgt = m.partition("->")
            if not sep:
                raise LoadError(f"mapping {m[:80]!r}: expected 'source.path -> target.path'")
            out.append({"from": _strip_indexes(src), "to": _strip_indexes(tgt), "name": None, "transformation": None})
        elif isinstance(m, dict) and "from" in m and "to" in m:
            out.append({"from": _strip_indexes(str(m["from"])), "to": _strip_indexes(str(m["to"])),
                        "name": m.get("mapping_name"), "transformation": m.get("transformation")})
        else:
            raise LoadError(f"mapping entry not understood: {str(m)[:80]!r}")
    return out


def _index(a: Analysis) -> dict:
    """Lookup by display path ('a.b.c'). A path that two columns share is flagged ambiguous (use escaped dots)."""
    idx: dict = {}
    for c in a.columns:
        idx.setdefault(c["path"], []).append(c["id"])
    return idx


def resolve(mappings: list[dict], source: Analysis, target: Analysis) -> list[dict]:
    idx = {"from": _index(source), "to": _index(target)}
    rels, errors, seen, skipped = [], [], set(), []
    for m in mappings:
        ends = {}
        for side in ("from", "to"):
            ref = ".".join(split_path(m[side]))  # normalise escaping to a display path
            ids = idx[side].get(ref, [])
            if not ids:  # a mapping may name the table only: 'mdm_account.x' for the column 'account.mdm_account.x'
                tails = [p for p in idx[side] if p.endswith("." + ref)]
                ids = idx[side][tails[0]] if len(tails) == 1 else []
            if len(ids) == 1:
                ends[side] = ids[0]
            elif m.get("soft"):
                skipped.append(f"{m['from']} -> {m['to']}: {side} column not found")
                break
            elif ids:
                errors.append(f"{side} '{m[side]}' is ambiguous (several columns share this path)")
            else:
                near = difflib.get_close_matches(ref, list(idx[side]), n=2)
                errors.append(f"{side} '{m[side]}' not found" + (f" (did you mean {near}?)" if near else ""))
        if len(ends) == 2 and (ends["from"], ends["to"]) not in seen:
            seen.add((ends["from"], ends["to"]))
            rels.append({"a": ends["from"], "b": ends["to"],
                         "props": {"mapping_name": m["name"], "transformation": m["transformation"]}})
    if skipped:
        logging.getLogger("graphload").warning("%d mapping(s) skipped (a column is not in the data):\n  %s%s",
                                               len(skipped), "\n  ".join(skipped[:15]),
                                               f"\n  ...and {len(skipped) - 15} more" if len(skipped) > 15 else "")
    if errors:
        shown = errors[:20]
        raise LoadError("Unresolved mapping endpoints:\n  " + "\n  ".join(shown)
                        + (f"\n  ...and {len(errors) - 20} more" if len(errors) > 20 else ""))
    return rels
