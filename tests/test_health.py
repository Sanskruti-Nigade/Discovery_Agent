import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mapping import health


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for v in ("LLM_PROVIDER", "MAPPING_LLM_PROVIDER", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AZURE_OPENAI_API_KEY",
              "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_CHAT_DEPLOYMENT", "NEO4J_PASSWORD"):
        monkeypatch.delenv(v, raising=False)
    health._llm_cache.clear()


class Resp:
    url = "https://api.example.com/v1/models?limit=1"

    def __init__(self, code):
        self.status_code = code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_llm_not_configured():
    r = health.check_llm()
    assert r["ok"] is False and "LLM_PROVIDER" in r["error"]


def test_llm_missing_key_for_chosen_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    r = health.check_llm()
    assert (r["ok"], r["key_configured"], r["error"]) == (False, False, "not set: OPENAI_API_KEY")


def test_llm_key_verified_live_and_cached(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    calls = []
    monkeypatch.setattr(health.requests, "get", lambda url, **kw: calls.append(url) or Resp(200))

    first, second = health.check_llm(), health.check_llm()

    assert first["ok"] and first["verified"] and first["provider"] == "anthropic"
    assert second.get("cached") and len(calls) == 1 and calls[0].endswith("/models")
    assert "sk-secret" not in str(first)


def test_llm_key_rejected(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "bad")  # provider auto-detected from the key
    monkeypatch.setattr(health.requests, "get", lambda url, **kw: Resp(401))
    r = health.check_llm()
    assert r["ok"] is False and r["provider"] == "openai" and "rejected" in r["error"]


def test_llm_config_only_without_live_call(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(health.requests, "get", lambda *a, **k: pytest.fail("no live call"))
    assert health.check_llm(live=False) == {"ok": True, "provider": "anthropic", "model": health._llm_model("anthropic"),
                                            "key_configured": True, "verified": False}


def test_neo4j_needs_password_and_reports_node_count(monkeypatch):
    assert health.check_neo4j()["error"] == "NEO4J_PASSWORD is not set"
    monkeypatch.setenv("NEO4J_PASSWORD", "pw")

    class Session:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def run(self, q): return types.SimpleNamespace(single=lambda: {"n": 42})

    class Driver:
        def verify_connectivity(self): pass
        def session(self, database): return Session()
        def close(self): pass

    fake = types.ModuleType("neo4j")
    fake.GraphDatabase = types.SimpleNamespace(driver=lambda *a, **k: Driver())
    monkeypatch.setitem(__import__("sys").modules, "neo4j", fake)
    assert health.check_neo4j()["nodes"] == 42


def test_endpoint_reports_each_check_and_strict_returns_503(monkeypatch):
    monkeypatch.setattr(health, "check_postgres", lambda: {"ok": True})
    monkeypatch.setattr(health, "check_neo4j", lambda: (_ for _ in ()).throw(ConnectionError("refused")))
    monkeypatch.setattr(health, "check_llm", lambda live=True: {"ok": True, "provider": "anthropic"})
    app = FastAPI()
    app.include_router(health.router)
    client = TestClient(app)

    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["checks"]["neo4j"] == {"ok": False, "error": "refused", "latency_ms": body["checks"]["neo4j"]["latency_ms"]}
    assert body["checks"]["postgres"]["ok"] and body["checks"]["llm"]["ok"]
    assert client.get("/health?strict=true").status_code == 503


def test_wrong_base_url_is_explained(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(health.requests, "get", lambda url, **kw: Resp(404))
    assert "base URL" in health.check_llm()["error"]
