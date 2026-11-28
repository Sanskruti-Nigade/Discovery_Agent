import json

import pytest

from mapping import trigger as tr


@pytest.fixture
def world(monkeypatch):
    calls, mapped = [], set()
    outcomes = {}
    monkeypatch.setattr(tr, "already_mapped", lambda oid, v: (oid, v) in mapped)

    def call(object_id):
        calls.append(object_id)
        result = outcomes.get(object_id, [(tr.Outcome.DONE, "ok")])
        return result.pop(0) if len(result) > 1 else result[0]

    monkeypatch.setattr(tr, "call_endpoint", call)
    monkeypatch.setattr(tr, "ENVIRONMENT", "dev")
    return {"calls": calls, "mapped": mapped, "outcomes": outcomes}


def notify(listener, object_id, version=1, object_type="api", env="dev"):
    listener.on_notify(json.dumps({"object_id": object_id, "object_type": object_type, "version": version,
                                   "environment": env, "snapshot_id": 1}))


def make_due(listener):
    for item in listener.pending.values():
        item["due"] = 0


def test_new_source_snapshot_calls_the_endpoint_once_after_debounce(world):
    lst = tr.Listener()
    notify(lst, "mdm.product_api")
    notify(lst, "mdm.product_api")       # ingestion may write twice: still one call
    lst.process_due()
    assert world["calls"] == []          # debounced
    make_due(lst)
    lst.process_due()
    assert world["calls"] == ["mdm.product_api"] and not lst.pending


def test_other_environment_and_already_mapped_versions_are_skipped(world):
    lst = tr.Listener()
    notify(lst, "mdm.product_api", env="prod")
    assert not lst.pending
    world["mapped"].add(("mdm.contact_api", 2))
    notify(lst, "mdm.contact_api", version=2)
    make_due(lst)
    lst.process_due()
    assert world["calls"] == [] and not lst.pending


def test_newer_version_replaces_older_and_older_is_ignored(world):
    lst = tr.Listener()
    notify(lst, "mdm.product_api", version=2)
    notify(lst, "mdm.product_api", version=1)
    assert lst.pending["mdm.product_api"]["version"] == 2
    notify(lst, "mdm.product_api", version=3)
    assert lst.pending["mdm.product_api"]["version"] == 3


def test_waits_for_targets_and_wakes_on_target_snapshot(world):
    world["outcomes"]["mdm.product_api"] = [(tr.Outcome.WAIT_FOR_TARGETS, "No target tables"), (tr.Outcome.DONE, "ok")]
    lst = tr.Listener()
    notify(lst, "mdm.product_api")
    make_due(lst)
    lst.process_due()
    item = lst.pending["mdm.product_api"]
    assert item["waiting_for_targets"] and item["due"] > 0
    notify(lst, "snowflake.dim_model", object_type="target_object")
    assert not item["waiting_for_targets"]
    make_due(lst)
    lst.process_due()
    assert world["calls"] == ["mdm.product_api", "mdm.product_api"] and not lst.pending


def test_errors_retry_with_backoff_then_give_up(world, monkeypatch):
    monkeypatch.setattr(tr, "MAX_ATTEMPTS", 3)
    world["outcomes"]["mdm.product_api"] = [(tr.Outcome.RETRY, "HTTP 502")]
    lst = tr.Listener()
    notify(lst, "mdm.product_api")
    delays = []
    for _ in range(3):
        make_due(lst)
        lst.process_due()
        if "mdm.product_api" in lst.pending:
            delays.append(lst.pending["mdm.product_api"]["due"])
    assert len(world["calls"]) == 3 and not lst.pending and len(delays) == 2


def test_not_found_is_not_retried(world):
    world["outcomes"]["nope"] = [(tr.Outcome.GIVE_UP, "HTTP 404")]
    lst = tr.Listener()
    notify(lst, "nope")
    make_due(lst)
    lst.process_due()
    assert world["calls"] == ["nope"] and not lst.pending


class Resp:
    def __init__(self, code, body):
        self.status_code, self._body, self.text, self.ok = code, body, json.dumps(body), code < 400

    def json(self):
        return self._body


@pytest.mark.parametrize("code, body, outcome", [
    (200, {"batch_id": "b", "mappings": [1, 2], "already_pending": []}, tr.Outcome.DONE),
    (422, {"detail": "No target tables found in metadata_snapshot"}, tr.Outcome.WAIT_FOR_TARGETS),
    (422, {"detail": "700 target fields is more than 600"}, tr.Outcome.GIVE_UP),
    (404, {"detail": "No source table"}, tr.Outcome.GIVE_UP),
    (502, {"detail": "LLM error"}, tr.Outcome.RETRY),
])
def test_endpoint_responses(monkeypatch, code, body, outcome):
    sent = {}
    monkeypatch.setattr(tr.requests, "post", lambda url, json, timeout: sent.update(url=url, json=json) or Resp(code, body))
    assert tr.call_endpoint("mdm.product_api")[0] == outcome
    assert sent["url"].endswith("/api/schema-mapping")
    assert sent["json"]["source"] == "mdm.product_api" and sent["json"]["requested_by"] == tr.REQUESTED_BY
