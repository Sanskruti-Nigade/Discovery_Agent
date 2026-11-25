"""Schema mapping as its own service (no Neo4j, no chat). Same endpoints as when mounted in frontend/app.py.

Run from the repo root:   uvicorn mapping.main:app --port 8001
Docs:                     http://localhost:8001/docs
"""
import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from mapping.api import router
from mapping.health import router as health_router

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(title="Schema Mapping Service",
              description="Postgres schemas -> LLM mappings -> review_queue / review_decision")
app.include_router(router)
app.include_router(health_router)  # GET /health: PostgreSQL, Neo4j, LLM key

# Needed only when the UI is served from another origin (e.g. UI on :8080, this service on :8001).
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv(
        "CORS_ALLOW_ORIGINS", "http://localhost:5173,http://localhost:8080").split(",") if o.strip()],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

