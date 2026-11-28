"""Unit tests: no Neo4j and no LLM needed.   Run:  python -m pytest tests -q"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from graphload import mappings as mp  # noqa: E402
from graphload.config import Limits, LoadError  # noqa: E402
from graphload.model import GraphModel  # noqa: E402
from graphload.reader import check_depth, read_document, resolve_inside  # noqa: E402
from graphload.schema import analyse, join_path, split_path  # noqa: E402
from graphload.writer import build_queries  # noqa: E402

L = Limits()


def cols(a):
    return {c["path"]: c["data_type"] for c in a.columns}


def test_nested_arrays_and_mixed_shapes_merge():
    data = {"a": [{"b": [{"c": [{"d": 1}, {"e": None, "f": [1, 2]}]}], "x": "2024-01-01"}, {"b": {"c": {"z": True}}}],
            "n": None}
    a = analyse("S", "source", data, L)
    assert cols(a) == {"root.n": None, "a.x": "date", "a.b.c.d": "integer", "a.b.c.e": None,
                       "a.b.c.f": "array", "a.b.c.z": "boolean"}
    assert [t["path"] for t in a.tables if t["name"] == "c"] == ["a.b.c"]


def test_same_table_name_at_two_paths_is_rejected_not_merged():
    with pytest.raises(LoadError, match="repeat"):
        analyse("S", "source", {"a": {"meta": {"x": 1}}, "b": {"meta": {"y": 1}}}, L)


def test_conflicting_types_become_mixed_or_number():
    a = analyse("S", "source", {"t": [{"v": 1}, {"v": "x"}], "u": [{"v": 1}, {"v": 2.5}]}, L)
    assert cols(a)["t.v"] == "mixed" and cols(a)["u.v"] == "number"


def test_dotted_keys_do_not_collide():
    a = analyse("S", "source", {"a": {"b": 1}, "a.b": {"c": 1}}, L)  # different table names: a and a.b
    ids = [c["id"] for c in a.columns]
    assert len(ids) == len(set(ids)) == 2
    assert split_path(join_path(["a.b", "c"])) == ["a.b", "c"]


def test_samples_are_off_by_default():
    assert all("sample" not in c for c in analyse("S", "source", {"t": {"k": "secret"}}, L).columns)
    assert analyse("S", "source", {"t": {"k": "v"}}, L, include_samples=True).columns[0]["sample"] == "v"


def test_limits_are_enforced():
    deep = cur = {}
    for _ in range(150):
        cur["k"] = {}
        cur = cur["k"]
    with pytest.raises(LoadError):
        check_depth(deep, 100)
    with pytest.raises(LoadError):
        analyse("S", "source", {f"t{i}": {"c": 1} for i in range(5)}, Limits(max_tables=3))


def test_mapping_resolution_and_errors():
    src = analyse("S", "source", {"P": {"M": [{"ID": 1}]}}, L)
    tgt = analyse("T", "target", {"dim": {"model_id": 1}}, L)
    ok = mp.resolve(mp.parse({"mapping": ["P.M[].ID -> dim.model_id", "P.M.ID -> dim.model_id"]}, L), src, tgt)
    assert len(ok) == 1  # indexes stripped, duplicate dropped
    with pytest.raises(LoadError, match="did you mean"):
        mp.resolve(mp.parse(["P.M.IDD -> dim.model_id"], L), src, tgt)


def test_graph_model_rejects_injection():
    for bad in ({"table_label": "Table`) DETACH DELETE (n"}, {"owns": "owns"}, {"table_label": "Column"}):
        with pytest.raises(LoadError):
            GraphModel.from_dict(bad)
    assert GraphModel.from_dict({"table_label": "Entity", "evil_key": "x"}).table_label == "Entity"


def test_queries_use_parameters_only():
    q = build_queries(GraphModel.from_dict({"table_label": "Entity"}))
    flat = [q["tables"], q["columns"], q["has_table"], q["has_column"], q["maps_to"], q["system"]("SourceSystem"),
            q["owns"]("SourceSystem"), *q["constraints"], *q["prune_nodes"], q["prune_maps"]]
    assert "`Entity`" in q["tables"] and all("$" in s for s in flat if "CONSTRAINT" not in s)


def test_path_traversal_and_bad_files(tmp_path):
    (tmp_path / "ok.json").write_text("{}")
    assert resolve_inside(tmp_path, "../../etc/ok.json").name == "ok.json"  # directory part dropped
    with pytest.raises(LoadError):
        resolve_inside(tmp_path, "missing.json")
    (tmp_path / "bad.json").write_text("{not json")
    with pytest.raises(LoadError):
        read_document(tmp_path / "bad.json", L)
    (tmp_path / "big.json").write_text(json.dumps({"k": "x" * 100}))
    with pytest.raises(LoadError):
        read_document(tmp_path / "big.json", Limits(max_file_bytes=10))


def test_real_sample_data_resolves_all_mappings():
    root = Path(__file__).resolve().parent.parent / "DummyData"
    if not (root / "Source.json").exists():
        pytest.skip("no DummyData")
    s = analyse("Source", "source", read_document(root / "Source.json", L), L)
    t = analyse("Target", "target", read_document(root / "Target.json", L), L)
    assert len(mp.resolve(mp.parse(read_document(root / "mappping.json", L), L), s, t)) == 46


# ----------------------------- change detection -----------------------------
from graphload import ChangeConfirmationRequired, check_decision  # noqa: E402
from graphload.diff import ChangePlan, diff_system  # noqa: E402

OLD = {("t", f"c{i}"): "string" for i in range(10)}
SAME = {"t": {f"c{i}": "x" for i in range(10)}}


def _plan(new, old_cols):
    return ChangePlan([diff_system(analyse("S", "source", new, L), {t for t, _ in old_cols}, old_cols)])


def test_change_kinds():
    assert _plan(SAME, OLD).kind == "none"
    assert _plan(SAME, {}).kind == "initial"
    minor = _plan({"t": {**SAME["t"], "extra": "x"}}, OLD)
    assert minor.kind == "minor" and minor.systems[0].added_columns == [("t", "extra")]
    assert _plan({"t": {"a": 1, "b": 2}}, OLD).kind == "major"
    assert _plan({"t": {**SAME["t"], "c0": 5}}, OLD).systems[0].type_changes == [("t", "c0", "string", "integer")]


def test_decisions():
    minor = _plan({"t": {**SAME["t"], "extra": "x"}}, OLD)
    major = _plan({"u": {"a": 1}}, OLD)
    with pytest.raises(ChangeConfirmationRequired):
        check_decision(minor, None, True)
    check_decision(minor, "apply", True)
    with pytest.raises(ChangeConfirmationRequired):
        check_decision(major, "apply", True)  # a total change needs an explicit remap
    check_decision(major, "remap", True)
    with pytest.raises(LoadError, match="mapping file"):
        check_decision(major, "remap", False)
