"""PostgreSQL access for schema mapping (owner: AI Engineer).

Same settings as main.py: PG_DSN, or POSTGRES_HOST / POSTGRES_PORT / POSTGRES_USER / POSTGRES_PASSWORD /
POSTGRES_DB (defaults match docker-compose.yml). Rows come back as dicts.
"""
import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

ENVIRONMENT = os.getenv("ENVIRONMENT", "dev")


def dsn() -> str:
    if os.getenv("PG_DSN"):
        return os.environ["PG_DSN"]
    return psycopg.conninfo.make_conninfo(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=os.getenv("POSTGRES_PORT", "5433"),
        user=os.getenv("POSTGRES_USER", "discovery"),
        password=os.getenv("POSTGRES_PASSWORD", "discovery123"),
        dbname=os.getenv("POSTGRES_DB", "discovery"),
    )


def connect():
    """New connection (use as a context manager); commit explicitly."""
    return psycopg.connect(dsn(), row_factory=dict_row, autocommit=False, connect_timeout=5)
