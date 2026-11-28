"""HTTP API for schema mapping + review. Mounted by frontend/app.py under /api/schema-mapping."""
import logging

import psycopg
import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from mapping import health as health_check
from mapping import llm, service

log = logging.getLogger("mapping.api")
router = APIRouter(prefix="/api/schema-mapping", tags=["schema mapping"])
# PostgreSQL, Neo4j and LLM-key status, under this service's own prefix (works wherever the router is mounted).
router.add_api_route("/health", health_check.health, methods=["GET"], tags=["health"])


class GenerateRequest(BaseModel):
    source: str = Field(..., description="object id (mdm.product_api), short name (product_api), table or API id")
    target_object_ids: list[str] | None = None
    min_conf: float = Field(0.6, ge=0, le=1)
    requested_by: str | None = None
    # auto: whole table the first time, afterwards only fields change_diff reports for the new version;
    # full: whole table, and re-propose pairs that were already approved/rejected
    scope: str = Field("auto", pattern="^(auto|full)$")


class DecisionRequest(BaseModel):
    decision: str = Field(..., pattern="^(approved|rejected)$")
    decided_by: str = Field(..., min_length=1)
    comment: str | None = None
    # review_queue ids the reviewer keeps; omitted = every open mapping in the batch.
    accepted_review_ids: list[int] | None = None


def _db(call, *args, **kwargs):
    """Turn database problems into clear HTTP errors."""
    try:
        return call(*args, **kwargs)
    except psycopg.errors.UndefinedTable as e:
        raise HTTPException(503, f"Missing table ({e.diag.message_primary}). PG_DSN must point at the discovery "
                                 "database (review_queue, review_decision, metadata_snapshot, schema_field).")
    except psycopg.OperationalError as e:
        raise HTTPException(503, f"PostgreSQL is not reachable: {str(e).splitlines()[0]}")


@router.get("/sources")
def sources():
    """Source tables / APIs (latest metadata_snapshot of type table or api)."""
    return {"items": _db(service.list_objects, "sources")}


@router.get("/targets")
def targets():
    """Target tables (latest metadata_snapshot of type target_object)."""
    return {"items": _db(service.list_objects, "targets")}


@router.post("")
def generate(req: GenerateRequest):
    """Read both schemas from Postgres, ask the LLM for mappings, store them as one pending batch."""
    try:
        return _db(service.generate, req.source, target_object_ids=req.target_object_ids,
                   min_conf=req.min_conf, requested_by=req.requested_by, scope=req.scope)
    except service.SourceNotFoundError as e:
        raise HTTPException(404, str(e))
    except service.SchemaMappingError as e:
        raise HTTPException(422, str(e))
    except (llm.LLMResponseError, requests.RequestException, RuntimeError) as e:
        log.warning("LLM schema mapping failed: %s", e)
        raise HTTPException(502, f"LLM error: {e}")


@router.get("/summary")
def summary():
    """Mapping counts by status and the number of batches waiting for approval."""
    return _db(service.summary)


@router.get("/batches")
def batches(status: str | None = None, source_object_id: str | None = None, limit: int = 50):
    """Batches newest first. status = pending | approved | rejected | all."""
    return {"items": _db(service.list_batches, status, source_object_id, max(1, min(limit, 200)))}


@router.get("/batches/{batch_id}")
def batch(batch_id: str):
    try:
        return _db(service.get_batch, batch_id)
    except service.BatchNotFoundError as e:
        raise HTTPException(404, str(e))


@router.post("/batches/{batch_id}/decision")
def decide(batch_id: str, d: DecisionRequest):
    """Approve (all or the accepted_review_ids) or reject a batch. Recorded in review_queue + review_decision."""
    try:
        return _db(service.decide_batch, batch_id, d.decision, d.decided_by.strip(), d.comment,
                   d.accepted_review_ids)
    except service.BatchNotFoundError as e:
        raise HTTPException(404, str(e))
    except service.BatchSelectionError as e:
        raise HTTPException(422, str(e))
    except service.BatchAlreadyDecidedError as e:
        raise HTTPException(409, str(e))


@router.get("/reviews")
def reviews(status: str | None = None, q: str | None = None, limit: int = 200, offset: int = 0):
    """Every review row (flat list), newest first. status = pending | approved | rejected | all."""
    return _db(service.list_reviews, status, q, max(1, min(limit, 1000)), max(0, offset))


@router.get("/approved")
def approved(since: str | None = None, source_object_id: str | None = None, limit: int = 100):
    """Feed for the graph team: approved batches with "table.column -> table.column" mapping strings.
    Pass the newest approved_at you processed as `since` next time."""
    items = _db(service.list_approved, since, source_object_id, max(1, min(limit, 500)))
    return {"count": len(items), "items": items}
