import json

import pytest

from mapping import service as sm


class Result:
    def __init__(self, *, rows=None, row=None):
        self.rows = rows or []
        self.row = row

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.row


class FakeDB:
    """Shared by every store.connect(); answers by statement and records events in order."""

    def __init__(self, batch_rows=None, pending_conflicts=()):
        self.batch_rows = batch_rows or []
        self.pending_conflicts = set(pending_conflicts)
        self.statements = []
        self.events = []
        self.next_id = 100

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self.statements.append((flat, params))
        if flat.startswith("INSERT INTO review_queue"):
            if (params[0], params[1]) in self.pending_conflicts:
                return Result(row=None)
            self.next_id += 1
            self.events.append("INSERT review_queue")
            return Result(row={"review_id": self.next_id})
        if flat.startswith("SELECT review_id, evidence->>'batch_id'"):
            return Result(row={"review_id": 5, "batch_id": "other", "created_by": "svc-ai-matcher"})
        if "%(batch_id)s" in flat:
            return Result(rows=self.batch_rows)
        if flat.startswith("INSERT INTO review_decision"):
            self.events.append(f"DECISION {params[0]} {params[1]}")
        elif flat.startswith("UPDATE review_queue"):
            self.events.append(f"STATUS {params[-1]} {params[0]}")
        return Result()

    def commit(self):
        self.events.append("COMMIT")

    def rollback(self):
        pass


PRODUCT_API = {"object_id": "mdm.product_api", "object_type": "api", "version": 3, "system": "sys.mdm",
               "system_name": None, "entity": "mdm.product", "table_name": None, "api_id": "api.mdm.product",
               "field_count": 4, "captured_at": None,
               "fields": [
                   {"field_path": "MODEL_PRODUCT.ID", "field_key": "mdm.product.MODEL_PRODUCT.ID",
                    "data_type": "string", "description": None, "max_length": None},
                   {"field_path": "ENTITLEMENT_PRODUCT.LIFECYCLE[].GEOGRAPHY",
                    "field_key": "mdm.product.ENTITLEMENT_PRODUCT.LIFECYCLE[].GEOGRAPHY",
                    "data_type": "string", "description": None, "max_length": None},
                   {"field_path": "META.VERSION", "field_key": "mdm.product.META.VERSION",
                    "data_type": "string", "description": None, "max_length": None},
                   {"field_path": "person_id", "field_key": "mdm.product.person_id",
                    "data_type": "string", "description": None, "max_length": None}]}


def target(object_id, system, *fields):
    return {"object_id": object_id, "object_type": "target_object", "version": 1, "system": system,
            "system_name": system[4:].upper(), "entity": object_id, "table_name": None, "api_id": None,
            "field_count": len(fields), "captured_at": None,
            "fields": [{"field_path": f, "field_key": f"{object_id}.{f}", "data_type": "string",
                        "description": None, "max_length": None} for f in fields]}


TARGETS = [target("snowflake.dim_model", "sys.snowflake", "model_id", "model_class"),
           target("snowflake.fact_product_lifecycle", "sys.snowflake", "geography"),
           target("sales_crm.contact", "sys.sales_crm", "Email"),
           target("now_support.contact", "sys.now_support", "EMAIL_ADDR")]


# ---------------------------------------------------------------- labels
@pytest.mark.parametrize("path, label", [
    ("MODEL_PRODUCT.ID", "model_product.id"),
    ("ENTITLEMENT_PRODUCT.LIFECYCLE[].GEOGRAPHY", "entitlement_product_lifecycle.geography"),
    ("META.VERSION", "product_meta.version"),
    ("person_id", "product_response.person_id"),
])
def test_source_label_for_api_fields(path, label):
    assert sm.source_label(PRODUCT_API, path) == label


def test_source_label_for_contact_api_and_table_columns():
    contact = {**PRODUCT_API, "entity": "mdm.contact", "object_id": "mdm.contact_api"}
    assert sm.source_label(contact, "profile[].primary_email") == "contact_profile.primary_email"
    assert sm.source_label(contact, "metadata.mdm_version") == "contact_metadata.mdm_version"
    table = {**PRODUCT_API, "object_type": "table", "table_name": "x_mdm_product"}
    assert sm.source_label(table, "number") == "x_mdm_product.number"


def test_target_label_keeps_system_only_when_names_collide():
    assert sm.target_label(TARGETS[0], "model_id") == "dim_model.model_id"
    assert sm.target_label(TARGETS[2], "Email", {"contact"}) == "sales_crm.contact.Email"


@pytest.mark.parametrize("name", ["mdm.product_api", "product_api", "PRODUCT_API", "api.mdm.product"])
def test_resolve_source_accepts_id_short_name_and_api_id(name):
    assert sm._resolve_source(name, [PRODUCT_API])["object_id"] == "mdm.product_api"


def test_resolve_source_unknown_and_ambiguous():
    with pytest.raises(sm.SourceNotFoundError, match="Known: mdm.product_api"):
        sm._resolve_source("nope", [PRODUCT_API])
    twin = {**PRODUCT_API, "object_id": "erp.product_api"}
    with pytest.raises(sm.SchemaMappingError, match="several sources"):
        sm._resolve_source("product_api", [PRODUCT_API, twin])


# ---------------------------------------------------------------- generate
@pytest.fixture
def world(monkeypatch):
    calls = []
    monkeypatch.setattr(sm, "_load", lambda types: [PRODUCT_API] if "api" in types else TARGETS)

    def map_schemas(source_table, target_tables):
        calls.append((source_table, target_tables))
        return [
            {"source_field_key": "mdm.product.MODEL_PRODUCT.ID", "target_field_key": "snowflake.dim_model.model_id",
             "confidence": 0.95, "transformation": None, "reason": "Same model identifier."},
            {"source_field_key": "mdm.product.ENTITLEMENT_PRODUCT.LIFECYCLE[].GEOGRAPHY",
             "target_field_key": "snowflake.fact_product_lifecycle.geography",
             "confidence": 0.9, "transformation": "upper(geography)", "reason": "Same geography."},
            {"source_field_key": "mdm.product.META.VERSION", "target_field_key": "snowflake.dim_model.model_class",
             "confidence": 0.4, "transformation": None, "reason": "Weak."},
            {"source_field_key": "mdm.product.invented", "target_field_key": "snowflake.dim_model.model_id",
             "confidence": 0.99, "transformation": None, "reason": "Hallucinated."},
            {"source_field_key": "mdm.product.MODEL_PRODUCT.ID", "target_field_key": "snowflake.dim_model.model_id",
             "confidence": 0.7, "transformation": None, "reason": "Duplicate."},
        ]

    monkeypatch.setattr(sm.llm_client, "map_schemas", map_schemas)
    return calls


def test_generate_stores_valid_mappings_as_pending_review_rows(monkeypatch, world):
    db = FakeDB()
    monkeypatch.setattr(sm.store, "connect", lambda: db)

    result = sm.generate("product_api", requested_by="manas.s")

    assert result["mapping"] == [
        "entitlement_product_lifecycle.geography -> fact_product_lifecycle.geography",
        "model_product.id -> dim_model.model_id",
    ]
    assert [m["review_id"] for m in result["mappings"]] == [101, 102]
    assert result["target_fields_considered"] == 5
    # the LLM saw only schema_field data: keys, paths, types, descriptions
    source_sent, targets_sent = world[0]
    assert source_sent["object_id"] == "mdm.product_api"
    assert set(source_sent["fields"][0]) == {"field_key", "field_path", "data_type", "description"}
    assert len(targets_sent) == 4
    inserts = [p for sql, p in db.statements if sql.startswith("INSERT INTO review_queue")]
    assert inserts[0][0] == "mdm.product.ENTITLEMENT_PRODUCT.LIFECYCLE[].GEOGRAPHY"
    evidence = json.loads(inserts[0][3])
    assert evidence["source"] == "schema_mapping"
    assert evidence["batch_id"] == result["batch_id"]
    assert evidence["mapping"] == result["mapping"][0]
    assert evidence["transformation"] == "upper(geography)"
    assert evidence["requested_by"] == "manas.s"
    assert inserts[0][4] == "svc-schema-mapping"
    assert "graph_update" not in result  # Neo4j is the graph team's: nothing graph-shaped is produced here


def test_generate_reports_pairs_already_pending(monkeypatch, world):
    db = FakeDB(pending_conflicts={("mdm.product.MODEL_PRODUCT.ID", "snowflake.dim_model.model_id")})
    monkeypatch.setattr(sm.store, "connect", lambda: db)

    result = sm.generate("product_api")

    assert result["mapping"] == ["entitlement_product_lifecycle.geography -> fact_product_lifecycle.geography"]
    assert result["already_pending"] == [{"mapping": "model_product.id -> dim_model.model_id",
                                          "review_id": 5, "batch_id": "other", "created_by": "svc-ai-matcher"}]


def test_generate_validates_targets_size_and_sync(monkeypatch, world):
    monkeypatch.setattr(sm.store, "connect", lambda: FakeDB())
    with pytest.raises(sm.SchemaMappingError, match="Unknown target tables"):
        sm.generate("product_api", target_object_ids=["snowflake.nope"])
    monkeypatch.setattr(sm, "MAX_TARGET_FIELDS_FOR_LLM", 2)
    with pytest.raises(sm.SchemaMappingError, match="pass target_object_ids"):
        sm.generate("product_api")
    monkeypatch.setattr(sm, "_load", lambda types: [{**PRODUCT_API, "fields": []}] if "api" in types else TARGETS)
    with pytest.raises(sm.SchemaMappingError, match="no fields"):
        sm.generate("product_api")


def test_generate_only_sends_chosen_targets(monkeypatch, world):
    monkeypatch.setattr(sm.store, "connect", lambda: FakeDB())

    sm.generate("product_api", target_object_ids=["snowflake.dim_model"])

    assert [t["object_id"] for t in world[0][1]] == ["snowflake.dim_model"]


# ---------------------------------------------------------------- decide (PostgreSQL only)
def batch_row(review_id, status="pending", decision=None, key="model_id"):
    return {"review_id": review_id, "from_field_key": "mdm.product.MODEL_PRODUCT.ID",
            "to_field_key": f"snowflake.dim_model.{key}", "confidence": 0.9, "status": status,
            "created_at": None, "created_by": "svc-schema-mapping", "decision": decision, "link_type": "MAPS_TO",
            "batch_id": "b1", "origin": "schema_mapping", "mapping": f"model_product.id -> dim_model.{key}",
            "source_object_id": "mdm.product_api", "target_object_id": "snowflake.dim_model",
            "decided_by": None, "decided_at": None, "comment": None,
            "evidence": {"source": "schema_mapping", "batch_id": "b1", "source_object_id": "mdm.product_api",
                         "mapping": f"model_product.id -> dim_model.{key}", "target_object_id": "snowflake.dim_model",
                         "target_system": "sys.snowflake", "target_system_name": "SNOWFLAKE",
                         "source_field": PRODUCT_API["fields"][0],
                         "target_field": TARGETS[0]["fields"][0 if key == "model_id" else 1],
                         "transformation": None, "reason": "Same id."}}


def test_partial_approval_records_every_decision_in_one_commit(monkeypatch):
    db = FakeDB([batch_row(7), batch_row(8, key="model_class")])
    monkeypatch.setattr(sm.store, "connect", lambda: db)

    result = sm.decide_batch("b1", "approved", "manas.s", "keep id only", accepted_review_ids=[7])

    assert db.events == ["DECISION 7 approved", "STATUS 7 approved", "DECISION 8 rejected", "STATUS 8 rejected",
                         "COMMIT"]
    assert result["status"] == "approved"
    assert result["mapping"] == ["model_product.id -> dim_model.model_id"]
    assert (result["approved_review_ids"], result["rejected_review_ids"]) == ([7], [8])


def test_approval_without_selection_approves_every_open_row(monkeypatch):
    db = FakeDB([batch_row(7), batch_row(8, status="rejected", decision="rejected", key="model_class")])
    monkeypatch.setattr(sm.store, "connect", lambda: db)

    result = sm.decide_batch("b1", "approved", "manas.s")

    assert result["approved_review_ids"] == [7]
    assert db.events == ["DECISION 7 approved", "STATUS 7 approved", "COMMIT"]


def test_rejection_rejects_every_open_row(monkeypatch):
    db = FakeDB([batch_row(7), batch_row(8, key="model_class")])
    monkeypatch.setattr(sm.store, "connect", lambda: db)

    result = sm.decide_batch("b1", "rejected", "manas.s", "wrong table", accepted_review_ids=[7])

    assert result["status"] == "rejected"
    assert db.events == ["DECISION 7 rejected", "STATUS 7 rejected", "DECISION 8 rejected", "STATUS 8 rejected",
                         "COMMIT"]


@pytest.mark.parametrize("legacy", ["approving", "failed"])
def test_rows_left_open_by_the_old_neo4j_flow_can_be_decided(monkeypatch, legacy):
    db = FakeDB([batch_row(7, status=legacy, decision="approved")])
    monkeypatch.setattr(sm.store, "connect", lambda: db)

    assert sm.decide_batch("b1", "approved", "manas.s")["status"] == "approved"


@pytest.mark.parametrize("status", ["approved", "rejected"])
def test_nothing_left_to_decide(monkeypatch, status):
    monkeypatch.setattr(sm.store, "connect", lambda: FakeDB([batch_row(7, status=status)]))

    with pytest.raises(sm.BatchAlreadyDecidedError):
        sm.decide_batch("b1", "approved", "manas.s")


def test_bad_input(monkeypatch):
    monkeypatch.setattr(sm.store, "connect", lambda: FakeDB([batch_row(7)]))
    with pytest.raises(sm.BatchSelectionError):
        sm.decide_batch("b1", "approved", "manas.s", accepted_review_ids=[99])
    with pytest.raises(ValueError, match="decided_by"):
        sm.decide_batch("b1", "approved", "  ")
    monkeypatch.setattr(sm.store, "connect", lambda: FakeDB([]))
    with pytest.raises(sm.BatchNotFoundError):
        sm.decide_batch("b1", "approved", "manas.s")


# ---------------------------------------------------------------- reading
def test_get_batch_returns_mapping_strings_and_statuses(monkeypatch):
    monkeypatch.setattr(sm.store, "connect", lambda: FakeDB(
        [batch_row(7, status="approved", decision="approved"), batch_row(8, status="rejected", key="model_class")]))

    batch = sm.get_batch("b1")

    assert batch["status"] == "approved"
    assert batch["counts"] == {"approved": 1, "rejected": 1}
    assert batch["mapping"] == ["model_product.id -> dim_model.model_id", "model_product.id -> dim_model.model_class"]
    assert [m["status"] for m in batch["mappings"]] == ["approved", "rejected"]


@pytest.mark.parametrize("statuses, expected", [
    (["pending", "pending"], "pending"), (["approved", "rejected"], "approved"), (["rejected"], "rejected"),
    (["failed", "rejected"], "pending"), (["approving", "approved"], "pending")])
def test_batch_status(statuses, expected):
    assert sm._batch_status(statuses) == expected


def test_rows_from_other_jobs_are_shown_as_single_reviews(monkeypatch):
    """svc-ai-matcher / feedback rows have no batch_id and only a reason in the evidence."""
    row = {"review_id": 42, "from_field_key": "mdm.product.number", "to_field_key": "sap_brim.material.MATNR",
           "link_type": "MAPS_TO", "confidence": 0.71, "status": "pending", "created_at": None,
           "created_by": "svc-ai-matcher", "batch_id": "review-42", "origin": "svc-ai-matcher",
           "mapping": "mdm.product.number -> sap_brim.material.MATNR", "source_object_id": "mdm.product_table",
           "target_object_id": "sap_brim.material", "src_field_path": "number", "src_data_type": "string",
           "tgt_field_path": "MATNR", "tgt_data_type": "string", "decision": None, "decided_by": None,
           "decided_at": None, "comment": None,
           "evidence": {"reason": "Both are product numbers.", "target_system": "sys.sap_brim", "status": "probable"}}
    db = FakeDB([row])
    monkeypatch.setattr(sm.store, "connect", lambda: db)

    batch = sm.get_batch("review-42")
    m = batch["mappings"][0]
    assert (batch["status"], batch["origin"], m["requested_by"]) == ("pending", "svc-ai-matcher", "svc-ai-matcher")
    assert (m["source_field_path"], m["target_object_id"], m["reason"]) == ("number", "sap_brim.material",
                                                                            "Both are product numbers.")
    assert sm.decide_batch("review-42", "approved", "manas.s")["approved_review_ids"] == [42]
    assert db.events == ["DECISION 42 approved", "STATUS 42 approved", "COMMIT"]


def test_plain_text_evidence_becomes_the_reason(monkeypatch):
    row = {"review_id": 9, "from_field_key": "a.b", "to_field_key": "c.d", "link_type": "MAPS_TO", "confidence": 0.8,
           "status": "pending", "created_at": None, "created_by": "svc-mapping-discovery", "batch_id": "review-9",
           "origin": "svc-mapping-discovery", "mapping": "a.b -> c.d", "source_object_id": None,
           "target_object_id": None, "decision": None, "decided_by": None, "decided_at": None, "comment": None,
           "evidence": "name similarity 0.8"}
    monkeypatch.setattr(sm.store, "connect", lambda: FakeDB([row]))

    assert sm.get_batch("review-9")["mappings"][0]["reason"] == "name similarity 0.8"


def test_graph_update_uses_the_shared_lineage_model(monkeypatch):
    monkeypatch.setattr(sm.store, "connect", lambda: FakeDB([batch_row(7), batch_row(8, key="model_class")]))

    g = sm.get_batch("b1")["graph_update"]

    labels = {n["id"]: n["label"] for n in g["nodes"]}
    assert labels["mdm_product_api"] == "SourceSystem" and labels["snowflake"] == "TargetSystem"
    assert labels["mdm_product_api.model_product.id"] == "Column"
    maps = [r for r in g["relationships"] if r["type"] == "MAPS_TO"]
    assert [(r["from"]["id"], r["to"]["id"], r["properties"]["review_id"]) for r in maps] == [
        ("mdm_product_api.model_product.id", "snowflake.dim_model.model_id", 7),
        ("mdm_product_api.model_product.id", "snowflake.dim_model.model_class", 8)]
    assert {r["type"] for r in g["relationships"]} == {"OWNS", "HAS_COLUMN", "MAPS_TO"}
    assert all("on_create_only" not in r for r in maps)


# ---------------------------------------------------------------- change_diff scope + decided pairs
def test_new_version_sends_only_changed_fields_to_the_llm(monkeypatch, world):
    monkeypatch.setattr(sm.store, "connect", lambda: FakeDB())
    monkeypatch.setattr(sm, "_change_scope", lambda source, scope: {
        "mode": "changes_only", "changed": {"MODEL_PRODUCT.ID": "added"}, "removed": ["OLD.FIELD"]})

    result = sm.generate("product_api")

    assert [f["field_path"] for f in world[0][0]["fields"]] == ["MODEL_PRODUCT.ID"]
    assert result["mapping"] == ["model_product.id -> dim_model.model_id"]
    assert result["scope"] == {"mode": "changes_only", "fields_sent_to_llm": 1, "fields_in_table": 4,
                               "changed_fields": ["MODEL_PRODUCT.ID"], "removed_fields": ["OLD.FIELD"]}


def test_only_removed_fields_means_no_llm_call(monkeypatch, world):
    monkeypatch.setattr(sm.store, "connect", lambda: FakeDB())
    monkeypatch.setattr(sm, "_change_scope", lambda source, scope: {"mode": "changes_only", "changed": {}, "removed": ["X"]})

    result = sm.generate("product_api")

    assert world == [] and result["status"] == "nothing_to_map" and result["batch_id"] is None
    assert result["scope"]["removed_fields"] == ["X"]


def test_already_decided_pairs_are_not_proposed_again(monkeypatch, world):
    db = FakeDB()
    monkeypatch.setattr(sm.store, "connect", lambda: db)
    monkeypatch.setattr(sm, "_decided_pairs", lambda keys: {("mdm.product.MODEL_PRODUCT.ID", "snowflake.dim_model.model_id")})

    result = sm.generate("product_api")

    assert result["mapping"] == ["entitlement_product_lifecycle.geography -> fact_product_lifecycle.geography"]
    assert result["already_decided"] == ["mdm.product.MODEL_PRODUCT.ID -> snowflake.dim_model.model_id"]
    assert result["llm"]["already_decided"] == 1


def test_type_change_reopens_a_decided_pair_and_full_scope_ignores_history(monkeypatch, world):
    monkeypatch.setattr(sm.store, "connect", lambda: FakeDB())
    monkeypatch.setattr(sm, "_decided_pairs", lambda keys: {("mdm.product.MODEL_PRODUCT.ID", "snowflake.dim_model.model_id")})
    monkeypatch.setattr(sm, "_change_scope", lambda source, scope: {
        "mode": "changes_only", "changed": {"MODEL_PRODUCT.ID": "type_changed"}, "removed": []})
    assert sm.generate("product_api")["mapping"] == ["model_product.id -> dim_model.model_id"]

    monkeypatch.setattr(sm, "_change_scope", lambda source, scope: {"mode": "full", "changed": None, "removed": []})
    monkeypatch.setattr(sm, "_decided_pairs", lambda keys: pytest.fail("full scope must not filter"))
    assert len(sm.generate("product_api", scope="full")["mapping"]) == 2


class ScopeDB(FakeDB):
    def __init__(self, prior, diffs):
        super().__init__()
        self.prior, self.diffs = prior, diffs

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        if "source_object_version')::int <" in flat:
            return Result(row={"x": 1} if self.prior else None)
        if flat.startswith("SELECT field_path, change_type FROM change_diff"):
            return Result(rows=self.diffs)
        return super().execute(sql, params)


@pytest.mark.parametrize("version, prior, diffs, mode", [
    (1, True, [{"field_path": "a", "change_type": "added"}], "full"),           # first version
    (2, False, [{"field_path": "a", "change_type": "added"}], "full"),          # never mapped before
    (2, True, [], "full"),                                                       # no diff recorded
    (2, True, [{"field_path": "a", "change_type": "added"}, {"field_path": "b", "change_type": "removed"},
               {"field_path": "c", "change_type": "type_changed"}], "changes_only"),
])
def test_change_scope(monkeypatch, version, prior, diffs, mode):
    monkeypatch.setattr(sm.store, "connect", lambda: ScopeDB(prior, diffs))
    out = sm._change_scope({"object_id": "mdm.product_api", "version": version, "snapshot_id": 9}, "auto")
    assert out["mode"] == mode
    if mode == "changes_only":
        assert out["changed"] == {"a": "added", "c": "type_changed"} and out["removed"] == ["b"]
