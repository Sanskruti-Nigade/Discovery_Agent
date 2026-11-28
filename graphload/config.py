"""Configuration: resource limits and Neo4j connection. Everything comes from the environment (.env); no secrets in code."""
import os
from dataclasses import dataclass, field


class LoadError(ValueError):
    """A problem with the input data or configuration. The message is safe to show to a caller."""


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        raise LoadError(f"{name} must be an integer")


@dataclass(frozen=True)
class Limits:
    """Hard caps so a hostile or accidental huge input cannot exhaust memory or the database."""
    max_file_bytes: int = field(default_factory=lambda: _int("GRAPHLOAD_MAX_FILE_MB", 50) * 1024 * 1024)
    max_depth: int = field(default_factory=lambda: _int("GRAPHLOAD_MAX_DEPTH", 100))
    max_tables: int = field(default_factory=lambda: _int("GRAPHLOAD_MAX_TABLES", 50_000))
    max_columns: int = field(default_factory=lambda: _int("GRAPHLOAD_MAX_COLUMNS", 500_000))
    max_mappings: int = field(default_factory=lambda: _int("GRAPHLOAD_MAX_MAPPINGS", 500_000))
    max_name_len: int = 200
    batch_size: int = 1000


@dataclass(frozen=True)
class Neo4jConfig:
    uri: str
    user: str
    password: str = field(repr=False)  # never printed or logged
    database: str = "neo4j"

    @classmethod
    def from_env(cls) -> "Neo4jConfig":
        user = os.getenv("NEO4J_USERNAME") or os.getenv("NEO4J_USER")
        password = os.getenv("NEO4J_PASSWORD")
        if not user or not password:
            raise LoadError("NEO4J_USERNAME / NEO4J_PASSWORD are not set")
        return cls(os.getenv("NEO4J_URI", "neo4j://127.0.0.1:7687"), user, password,
                   os.getenv("NEO4J_DATABASE", "neo4j"))
