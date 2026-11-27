# Schema mapping + review queue (`mapping/`)

Owner: AI Engineer. Moved from `discovery-service` (`discovery/schema_mapping.py`, `map_schemas` in
`discovery/llm_client.py`, the `/discovery/schema-mapping` API).

```
PostgreSQL metadata_snapshot + schema_field   (filled by the ingestion job)
   -> LLM maps source fields to target fields   (mapping/llm.py, uses ai/llm_client.py)
   -> review_queue (one pending row per mapping, grouped by evidence.batch_id)
   -> reviewer approves / rejects in the UI     (review_decision row per decision)
   -> graph team reads GET /api/schema-mapping/approved and loads Neo4j with their own code
```

The Reviews page and the read/decide APIs cover **every** `review_queue` row, including rows written by other
discovery-service jobs (`svc-ai-matcher`, `svc-mapping-discovery`, `svc-impact-report`, `feedback`). Those rows
have no `batch_id`, so each one is its own batch `review-<review_id>`; `origin` says which job wrote it.

`graph_update` (in `/batches/{id}` and `/approved`) shows the mappings in the graph dev's model:
`SourceSystem|TargetSystem -OWNS-> Table -HAS_COLUMN-> Column -MAPS_TO-> Column`, named like
`lineage/json_files` (source system `mdm_product_api`, target system `snowflake`). It is a preview and hand-off
payload; the reviewer's comment is saved per row in `review_decision.comment`.

This module **never writes Neo4j**. Approved mappings come out as `"table.column -> table.column"` strings,
the same format as `lineage/json_files/mapping.json`.

| File | Role |
|---|---|
| `db.py` | Postgres connection: `PG_DSN`, or `POSTGRES_HOST/PORT/USER/PASSWORD/DB` (same as `main.py`) |
| `llm.py` | `map_schemas()` prompt + validation. Uses the shared `ai/llm_client.chat` (`LLM_PROVIDER` in `.env`) |
| `service.py` | generate, list/read batches and reviews, decide, approved feed |
| `api.py` | The HTTP endpoints (FastAPI router under `/api/schema-mapping`) |
| `health.py` | Health check: PostgreSQL, Neo4j and LLM key (`/api/schema-mapping/health`) |
| `main.py`, `requirements.txt`, `Dockerfile` | Run the endpoints as a standalone microservice |
| `mapping_examples.yaml` | Curated scoring examples sent with every prompt |

## Setup

```bash
pip install -r requirements.txt
cd frontend && python -m uvicorn app:app --port 8080
```

Uses the existing discovery tables only (created by the discovery-service migrations): `metadata_snapshot`,
`schema_field` (filled by the ingestion job), `review_queue`, `review_decision`. Nothing here creates tables.
Offline: set `MAPPING_LLM_PROVIDER=stub` and run the discovery-service `llm-stub` container (`LLM_BASE_URL`).

## Two ways to run it

The endpoints live in `api.py` (a FastAPI `APIRouter`); `service.py` holds the logic they call. The same
router runs in either of two ways, with identical paths:

| Mode | Command | URL |
|---|---|---|
| Inside the Discovery Agent website (default) | `cd frontend` then `python -m uvicorn app:app --port 8080` | http://localhost:8080/api/schema-mapping/... |
| As its own microservice | from the repo root: `uvicorn mapping.main:app --port 8001` | http://localhost:8001/api/schema-mapping/... , `/health`, `/docs` |

The `mapping/` folder is self-contained: it imports nothing from `frontend/`, `ai/` or `lineage/`. Inside this
repo it uses `ai/llm_client.py` for the LLM so the repo has one LLM setting; copied anywhere without `ai/` it
uses its built-in client with the same variables (`LLM_PROVIDER`, `ANTHROPIC_API_KEY`, `ANTHROPIC_CHAT_MODEL`,
`OPENAI_*`, `AZURE_OPENAI_*`).

Microservice extras: `main.py` (app + `/health` + CORS), `requirements.txt` (only what this service needs),
`Dockerfile`:

```bash
docker build -f mapping/Dockerfile -t schema-mapping .
docker run -p 8001:8001 --env-file .env schema-mapping
```

Environment: `PG_DSN` (or `POSTGRES_*`), `ENVIRONMENT`, the LLM variables above, `CORS_ALLOW_ORIGINS`
(default `http://localhost:5173,http://localhost:8080`), `LOG_LEVEL`. Point the UI at it with
`VITE_MAPPING_API_BASE=http://localhost:8001` in `ui/.env` and rebuild.

## Health check: `GET /api/schema-mapping/health`

Part of this service's API, so it works in both run modes: http://localhost:8080/api/schema-mapping/health and
http://localhost:8001/api/schema-mapping/health (the standalone service also answers at http://localhost:8001/health). The three checks run
in parallel with a 5 s timeout each (`HEALTH_TIMEOUT_SECONDS`):

| Check | OK when |
|---|---|
| `postgres` | `PG_DSN` answers and `review_queue`, `review_decision`, `metadata_snapshot`, `schema_field` exist |
| `neo4j` | `NEO4J_URI` / `NEO4J_USERNAME` / `NEO4J_PASSWORD` connect; also returns the node count |
| `llm` | `LLM_PROVIDER` (or an auto-detected key) is set and the provider accepts the key: a free "list models" call, cached for 60 s |

```json
{"status": "degraded",
 "checks": {"postgres": {"ok": true, "host": "localhost", "port": "5432", "database": "discovery", "latency_ms": 12},
            "neo4j":    {"ok": false, "uri": "bolt://localhost:7687", "error": "Couldn't connect to localhost:7687", "latency_ms": 4},
            "llm":      {"ok": false, "provider": "anthropic", "key_configured": true, "verified": false,
                         "error": "key rejected by anthropic (HTTP 401)", "latency_ms": 56}}}
```

- Always HTTP 200; `?strict=true` returns 503 when any check fails (for load balancers / Kubernetes probes).
- `?live_llm=false` only checks that the key is set, without calling the provider.
- Keys and passwords are never returned. The UI's top-bar dots (Neo4j, PostgreSQL, LLM) use this endpoint.

## API (`/api/schema-mapping`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/sources`, `/targets` | Latest source (table/api) and target snapshots |
| POST | `` | `{"source","target_object_ids"?,"min_conf"?,"requested_by"?}` -> LLM -> new pending batch |
| GET | `/summary` | Counts by status + batches waiting for approval |
| GET | `/batches?status=pending\|approved\|rejected\|all` | Batches, newest first |
| GET | `/batches/{batch_id}` | One batch with every mapping, its latest decision and `graph_update` (preview in the shared Neo4j model) |
| POST | `/batches/{batch_id}/decision` | `{"decision":"approved"\|"rejected","decided_by","comment"?,"accepted_review_ids"?}` |
| GET | `/reviews?status=&q=&limit=&offset=` | Flat list of every review row |
| GET | `/approved?since=&source_object_id=&limit=` | Feed for the graph team |

Statuses: `pending -> approved | rejected`. Approving with `accepted_review_ids` approves those rows and
rejects the other open rows of the batch. Rows left in `approving` / `failed` by the older discovery-service
flow are treated as pending.

## Tests

`python -m pytest tests -q` (no database needed).
