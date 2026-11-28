"""Tiny provider-agnostic chat client. Credentials come from ../.env.

LLM_PROVIDER = anthropic | openai | azure | llama   ('stub' cannot write Cypher and is rejected)
"""
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

PROVIDER = os.getenv("LLM_PROVIDER", "").lower()
if PROVIDER in ("", "stub"):  # none chosen (or the mock, which can't write Cypher): use a provider with credentials
    PROVIDER = next((p for p, var in (("anthropic", "ANTHROPIC_API_KEY"), ("openai", "OPENAI_API_KEY"),
                                      ("azure", "AZURE_OPENAI_API_KEY")) if os.getenv(var)), "")


def chat(system: str, user: str, max_tokens: int = 4000) -> str:
    """Send one system+user prompt, return the model's text."""
    if PROVIDER == "anthropic":
        r = requests.post(
            os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1").rstrip("/") + "/messages",
            headers={"x-api-key": _need("ANTHROPIC_API_KEY"), "anthropic-version": "2023-06-01"},
            json={"model": os.getenv("ANTHROPIC_CHAT_MODEL", "claude-haiku-5-5"), "max_tokens": max_tokens,
                  "system": system, "messages": [{"role": "user", "content": user}]},
            timeout=120)
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")

    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    if PROVIDER == "llama":  # self-hosted Ollama-style server: POST {base}/chat
        r = requests.post(
            os.getenv("LLAMA_BASE_URL", "").rstrip("/") + "/chat", **llama_auth(),
            json={"model": os.getenv("LLAMA_MODEL", "llama3.1:8b"), "messages": msgs, "stream": False,
                  "options": {"temperature": 0, "num_predict": max_tokens}},
            timeout=300, verify=os.getenv("LLAMA_VERIFY_SSL", "true").lower() != "false")
        r.raise_for_status()
        return r.json()["message"]["content"]
    if PROVIDER == "openai":
        url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {_need('OPENAI_API_KEY')}"}
        body = {"model": os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini"), "messages": msgs, "temperature": 0}
    elif PROVIDER == "azure":
        url = (f"{_need('AZURE_OPENAI_ENDPOINT').rstrip('/')}/openai/deployments/{_need('AZURE_OPENAI_CHAT_DEPLOYMENT')}"
               f"/chat/completions?api-version={os.getenv('AZURE_OPENAI_API_VERSION', '2024-06-01')}")
        headers = {"api-key": _need("AZURE_OPENAI_API_KEY")}
        body = {"messages": msgs, "temperature": 0}
    else:
        raise RuntimeError(f"LLM_PROVIDER={PROVIDER!r} cannot generate Cypher; set it to anthropic, openai or azure in .env")
    r = requests.post(url, headers=headers, json=body, timeout=120)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def llama_auth() -> dict:
    """requests kwargs: basic auth from LLAMA_USERNAME/LLAMA_PASSWORD, else LLAMA_API_KEY_HEADER as Authorization."""
    user, pw = (os.getenv("LLAMA_USERNAME") or "").strip(), os.getenv("LLAMA_PASSWORD") or ""
    if user and pw:
        return {"auth": (user, pw)}
    key = _need("LLAMA_API_KEY_HEADER")  # Bearer is added if the value has no scheme
    return {"headers": {"Authorization": key if key.split(" ")[0] in ("Bearer", "Basic") else f"Bearer {key}"}}


def _need(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"{name} is not set in .env")
    return v
