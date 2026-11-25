import json
import sys
import types

import pytest

from mapping import llm


def fake_chat(reply):
    module = types.ModuleType("llm_client")
    module.chat = lambda system, user, max_tokens=0: reply
    return module


def test_uses_shared_chat_client_and_tolerates_fences(monkeypatch):
    reply = "```json\n" + json.dumps({"mappings": [{"source_field_key": "a", "target_field_key": "b",
                                                    "confidence": 0.9, "transformation": "", "reason": "x"}]}) + "\n```"
    monkeypatch.setitem(sys.modules, "llm_client", fake_chat(reply))
    monkeypatch.delenv("MAPPING_LLM_PROVIDER", raising=False)

    out = llm.map_schemas({"object_id": "s", "fields": []}, [])

    assert out == [{"source_field_key": "a", "target_field_key": "b", "confidence": 0.9,
                    "transformation": None, "reason": "x"}]


@pytest.mark.parametrize("reply", ["not json", json.dumps({"mappings": [{"source_field_key": "a",
                                   "target_field_key": "b", "confidence": 2, "reason": "x"}]}), json.dumps({})])
def test_rejects_bad_llm_output(monkeypatch, reply):
    monkeypatch.setitem(sys.modules, "llm_client", fake_chat(reply))
    with pytest.raises(llm.LLMResponseError):
        llm.map_schemas({"object_id": "s", "fields": []}, [])


def test_falls_back_to_builtin_client_without_ai_folder(monkeypatch):
    """mapping/ deployed alone: no ai/llm_client.py, the built-in client is used."""
    monkeypatch.setitem(sys.modules, "llm_client", None)  # import llm_client -> ImportError
    monkeypatch.setattr(llm, "_builtin_chat", lambda s, u, max_tokens=0: json.dumps({"mappings": []}))
    monkeypatch.delenv("MAPPING_LLM_PROVIDER", raising=False)

    assert llm.map_schemas({"object_id": "s", "fields": []}, []) == []
