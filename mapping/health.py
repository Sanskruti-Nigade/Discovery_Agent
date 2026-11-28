"""Health check for the schema-mapping service: PostgreSQL, Neo4j and the LLM key.

Served at GET /api/schema-mapping/health (mapping/api.py, so wherever the router is mounted) and, when running
standalone (mapping/main.py), also at GET /health.

    ...health                  -> 200 always, body says what is up ("ok" or "degraded")
    ...health?strict=true      -> 503 when anything is down (for load-balancer / k8s probes)
    ...health?live_llm=false   -> only checks the LLM key is configured, without calling the provider

The checks run in parallel, each with a short timeout. Keys and passwords are never returned.
Uses the same environment variables as the rest of the repo (PG_DSN / POSTGRES_*, NEO4J_*, LLM_PROVIDER + key).
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg
import requests
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from mapping import db

TIMEOUT = float(os.getenv("HEALTH_TIMEOUT_SECONDS", "5"))
LLM_CACHE_SECONDS = 60  # a live key check calls the provider; don't repeat it on every poll

router = APIRouter(tags=["health"])
_llm_cache: dict = {}


def _timed(fn) -> dict:
    started = time.perf_counter()
    try:
        result = fn()
    except Exception as e:  # a health check reports, it never raises
        result = {"ok": False, "error": (str(e).splitlines() or [type(e).__name__])[0][:300]}
    result["latency_ms"] = round((time.perf_counter() - started) * 1000)
    return result


# ---------------------------------------------------------------- PostgreSQL
def check_postgres() -> dict:
    info = psycopg.conninfo.conninfo_to_dict(db.dsn())
    where = {"host": info.get("host"), "port": info.get("port"), "database": info.get("dbname")}
    with db.connect() as conn:
        conn.execute("SELECT 1")
        missing = [t for t in ("review_queue", "review_decision", "metadata_snapshot", "schema_field")
                   if conn.execute("SELECT to_regclass(%s) AS t", (t,)).fetchone()["t"] is None]
    if missing:
        return {"ok": False, **where, "error": f"connected, but tables are missing: {', '.join(missing)}"}
    return {"ok": True, **where}


# ---------------------------------------------------------------- Neo4j
def check_neo4j() -> dict:
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USERNAME") or os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "")
    database = os.getenv("NEO4J_DATABASE", "neo4j")
    where = {"uri": uri, "database": database}
    if not password:
        return {"ok": False, **where, "error": "NEO4J_PASSWORD is not set"}
    try:
        from neo4j import GraphDatabase
    except ImportError:
        return {"ok": False, **where, "error": "neo4j Python driver is not installed (pip install neo4j)"}
    try:
        driver = GraphDatabase.driver(uri, auth=(user, password), connection_timeout=TIMEOUT,
                                      connection_acquisition_timeout=TIMEOUT)
        try:
            driver.verify_connectivity()
            with driver.session(database=database) as session:
                nodes = session.run("MATCH (n) RETURN count(n) AS n").single()["n"]
        finally:
            driver.close()
    except Exception as e:
        return {"ok": False, **where, "error": (str(e).splitlines() or [type(e).__name__])[0][:300]}
    return {"ok": True, **where, "nodes": nodes}


# ---------------------------------------------------------------- LLM
KEY_VARS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "azure": "AZURE_OPENAI_API_KEY"}


def llm_provider() -> str:
    """Same choice as ai/llm_client.py: LLM_PROVIDER, else the first provider with a key."""
    if os.getenv("MAPPING_LLM_PROVIDER", "").lower() == "stub":
        return "stub"
    provider = os.getenv("LLM_PROVIDER", "").lower()
    if provider in ("", "stub"):
        provider = next((p for p, var in KEY_VARS.items() if os.getenv(var)), "")
    return provider


def _llm_model(provider: str) -> str | None:
    return {"anthropic": os.getenv("ANTHROPIC_CHAT_MODEL", "claude-haiku-5-5"),
            "openai": os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini"),
            "azure": os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT"),
            "llama": os.getenv("LLAMA_MODEL", "llama3.1:8b")}.get(provider)


def _llm_live(provider: str) -> None:
    """Cheapest authenticated call per provider (lists models; uses no tokens). Raises on failure."""
    if provider == "anthropic":
        r = requests.get(os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1").rstrip("/") + "/models",
                         headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
                         params={"limit": 1}, timeout=TIMEOUT)
    elif provider == "openai":
        r = requests.get(os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/models",
                         headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}, timeout=TIMEOUT)
    elif provider == "azure":
        r = requests.get(f"{os.environ['AZURE_OPENAI_ENDPOINT'].rstrip('/')}/openai/models",
                         params={"api-version": os.getenv("AZURE_OPENAI_API_VERSION", "2024-06-01")},
                         headers={"api-key": os.environ["AZURE_OPENAI_API_KEY"]}, timeout=TIMEOUT)
    elif provider == "llama":
        auth_kwargs = {}
        user, pw = os.getenv("LLAMA_USERNAME"), os.getenv("LLAMA_PASSWORD")
        if user and pw:
            auth_kwargs = {"auth": (user, pw)}
        elif os.getenv("LLAMA_API_KEY_HEADER"):
            key = os.environ["LLAMA_API_KEY_HEADER"]
            auth_kwargs = {"headers": {"Authorization": key if key.split(" ")[0] in ("Bearer", "Basic") else f"Bearer {key}"}}
        r = requests.get(os.environ["LLAMA_BASE_URL"].rstrip("/") + "/tags", timeout=TIMEOUT,
                         verify=os.getenv("LLAMA_VERIFY_SSL", "true").lower() != "false", **auth_kwargs)
    elif provider == "stub":
        r = requests.get(os.getenv("LLM_BASE_URL", "http://localhost:8090").rstrip("/") + "/", timeout=TIMEOUT)
        if r.status_code < 500:
            return
    else:
        raise RuntimeError(f"unknown provider {provider!r}")
    if r.status_code in (401, 403):
        raise RuntimeError(f"key rejected by {provider} (HTTP {r.status_code})")
    if r.status_code == 404:
        raise RuntimeError(f"{provider} endpoint not found ({r.url.split('?')[0]}): check the base URL / endpoint "
                           "setting (e.g. ANTHROPIC_BASE_URL must end in /v1)")
    r.raise_for_status()


def check_llm(live: bool = True) -> dict:
    provider = llm_provider()
    if not provider:
        return {"ok": False, "provider": None,
                "error": "no LLM configured: set LLM_PROVIDER and its key (ANTHROPIC_API_KEY / OPENAI_API_KEY / "
                         "AZURE_OPENAI_API_KEY) in .env"}
    out = {"provider": provider, "model": _llm_model(provider)}
    needed = {"anthropic": ["ANTHROPIC_API_KEY"], "openai": ["OPENAI_API_KEY"],
              "azure": ["AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_CHAT_DEPLOYMENT"],
              "llama": ["LLAMA_BASE_URL"], "stub": []}.get(provider)
    if needed is None:
        return {"ok": False, **out, "error": f"unsupported LLM_PROVIDER {provider!r}"}
    missing = [v for v in needed if not os.getenv(v)]
    if missing:
        return {"ok": False, **out, "key_configured": False, "error": f"not set: {', '.join(missing)}"}
    out["key_configured"] = True
    if not live:
        return {"ok": True, **out, "verified": False}
    cached = _llm_cache.get(provider)
    if cached and time.time() - cached["at"] < LLM_CACHE_SECONDS:
        return {**cached["result"], "cached": True}
    try:
        _llm_live(provider)
        result = {"ok": True, **out, "verified": True}
    except Exception as e:
        result = {"ok": False, **out, "verified": False, "error": (str(e).splitlines() or ["error"])[0][:300]}
    _llm_cache[provider] = {"at": time.time(), "result": result}
    return result


# ---------------------------------------------------------------- endpoint
def health_report(live_llm: bool = True) -> dict:
    checks = {"postgres": check_postgres, "neo4j": check_neo4j, "llm": lambda: check_llm(live_llm)}
    with ThreadPoolExecutor(max_workers=len(checks)) as pool:
        futures = {name: pool.submit(_timed, fn) for name, fn in checks.items()}
        results = {name: f.result() for name, f in futures.items()}
    return {"status": "ok" if all(r["ok"] for r in results.values()) else "degraded", "checks": results}


@router.get("/health")
def health(strict: bool = False, live_llm: bool = True):
    """PostgreSQL, Neo4j and LLM key status. strict=true returns 503 when any check fails."""
    report = health_report(live_llm)
    return JSONResponse(report, status_code=503 if strict and report["status"] != "ok" else 200)
