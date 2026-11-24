"""Text -> vector. EMBED_PROVIDER = local (default, no key) | openai | azure.

Anthropic has no embeddings API, so the answering LLM (llm_client.chat) and the embedder are separate.
The same provider/model MUST be used for indexing and for questions.
"""
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

PROVIDER = os.getenv("EMBED_PROVIDER", "local").lower()
_model = None


def embed(texts: list[str]) -> list[list[float]]:
    if PROVIDER == "local":
        global _model
        if _model is None:
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer(os.getenv("EMBED_MODEL", "all-MiniLM-L6-v2"))  # 384 dims
        return _model.encode(texts, normalize_embeddings=True).tolist()
    if PROVIDER == "openai":
        r = requests.post(os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/embeddings",
                          headers={"Authorization": f"Bearer {_need('OPENAI_API_KEY')}"},
                          json={"model": os.getenv("EMBED_MODEL", "text-embedding-3-small"), "input": texts},
                          timeout=120)
    elif PROVIDER == "azure":
        r = requests.post(f"{_need('AZURE_OPENAI_ENDPOINT').rstrip('/')}/openai/deployments/"
                          f"{_need('AZURE_OPENAI_EMBED_DEPLOYMENT')}/embeddings"
                          f"?api-version={os.getenv('AZURE_OPENAI_API_VERSION', '2024-06-01')}",
                          headers={"api-key": _need("AZURE_OPENAI_API_KEY")}, json={"input": texts}, timeout=120)
    else:
        raise RuntimeError(f"EMBED_PROVIDER={PROVIDER!r}: use local, openai or azure")
    r.raise_for_status()
    return [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]


def _need(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"{name} is not set in .env")
    return v
