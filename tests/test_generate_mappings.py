import json

import pytest

from mapping import generate_mappings as gm

SOURCES = [
    {"object_id": "mdm.contact_api", "object_type": "api", "version": 1, "captured_at": None, "field_count": 3, "mapped": False},
    {"object_id": "mdm.product_api", "object_type": "api", "version": 2, "captured_at": None, "field_count": 4, "mapped": True},
    {"object_id": "mdm.empty_api", "object_type": "api", "version": 1, "captured_at": None, "field_count": 0, "mapped": False},
]


class Resp:
    def __init__(self, code, body):
        self.status_code, self._body, self.ok, self.text = code, body, code < 400, json.dumps(body)

    def json(self):
        return self._body


@pytest.fixture
def calls(monkeypatch):
    sent = []
    monkeypatch.setattr(gm, "source_tables", lambda: SOURCES)

    def post(url, json, timeout):
        sent.append((url, json))
        if json["source"] == "broken":
            return Resp(502, {"detail": "LLM error"})
        if json["source"].startswith("nokey"):
            return Resp(502, {"detail": "LLM error: 401 Client Error: Unauthorized for url: https://api.anthropic.com/v1/messages"})
        return Resp(200, {"batch_id": "b1", "mappings": [{}, {}], "already_pending": []})

    monkeypatch.setattr(gm.requests, "post", post)
    return sent


def test_default_maps_only_unmapped_sources_with_fields(calls):
    assert gm.main(["--api-url", "http://x:8080/api/schema-mapping"]) == 0
    assert [(u, j["source"]) for u, j in calls] == [("http://x:8080/api/schema-mapping", "mdm.contact_api")]
    assert calls[0][1]["min_conf"] == 0.6 and calls[0][1]["target_object_ids"] is None


def test_all_and_explicit_sources(calls):
    gm.main(["--all", "--api-url", "http://x:8080"])
    gm.main(["--source", "product_api", "--targets", "snowflake.dim_model", "--requested-by", "manas", "--api-url", "http://x:8080"])
    assert [j["source"] for _, j in calls] == ["mdm.contact_api", "mdm.product_api", "product_api"]
    assert calls[-1][1]["target_object_ids"] == ["snowflake.dim_model"] and calls[-1][1]["requested_by"] == "manas"


def test_list_calls_nothing_and_failures_set_exit_code(calls):
    assert gm.main(["--list"]) == 0 and calls == []
    assert gm.main(["--source", "broken", "--api-url", "http://x:8080"]) == 1


def test_stops_after_a_rejected_key(calls):
    assert gm.main(["--source", "nokey1", "--source", "nokey2", "--source", "nokey3", "--api-url", "http://x:8080"]) == 1
    assert [j["source"] for _, j in calls] == ["nokey1"]
