"""Let the LLM read data files in ANY structure and rewrite them into the one canonical lineage format.
The loader's validation then checks the result, so a wrong or hallucinated reference fails loudly.

Canonical output (list of documents):
  {"source": {"system","type","tables": {table: [{"name","data_type","properties":{...}}]}},
   "target": {... same ...},
   "mapping": [{"from":"table.column","to":"table.column","transformation":..., "mapping_name":...}]}
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lineage"))
import loader  # noqa: E402

from llm_client import chat  # noqa: E402

OUT_DIR = Path(__file__).parent / "generated"

PROMPT = """You normalize data-lineage metadata files, whatever their structure, into ONE canonical JSON format.

Input: the contents of one or more files (any shape) describing source systems, their tables and columns,
target systems, and column-to-column mappings.

Output ONLY JSON (no prose, no markdown fences): {"documents": [ <document>, ... ]}, one document per
source-system -> target-system pair, each exactly:
{
  "source": {"system": "<name>", "type": "<system type if stated>",
             "tables": {"<table>": [{"name": "<col>", "data_type": "<type if stated>", "properties": {"<k>": "<v>"}}]}},
  "target": { same shape },
  "mapping": [{"from": "<table>.<column>", "to": "<table>.<column>",
               "transformation": "<if stated>", "mapping_name": "<if stated>"}]
}

RULES:
- Use ONLY what the files say. Never invent systems, tables, columns, types or mappings.
- Keep every column, including extra attributes (pk, fk, nullable, ...): put them in that column's "properties".
- If a file gives a mapping name (e.g. "mapping_name") for a whole mapping, copy it into EVERY entry of that mapping's
  "mapping" list as "mapping_name", and copy per-entry "transformation" values exactly.
- "from" is always on the source side and "to" on the target side (lineage flows source -> target).
- Every mapping "from"/"to" must be a table.column you listed. If a mapping points at a table or column that does not
  exist in the files, output exactly {"error": "<which reference is missing>"} instead of guessing or fixing typos.
- If a column has an example value in the data (e.g. "ACTIVE": false), keep it as that column's
  "properties": {"sample": <the value as written>}, and set "data_type" from the JSON value type
  (true/false -> "boolean", number -> "number") when no type is stated. For text values do NOT guess a type
  (it may be a date or timestamp): leave data_type out.
- Omit a key whose value is not stated (e.g. no data_type) rather than guessing it.
"""


def extract_json(text: str):
    """Parse the JSON object out of a reply, ignoring any prose or code fences around it."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise json.JSONDecodeError("no JSON object in the reply", text, 0)
    return json.loads(text[start:end + 1])


def collect_files(paths) -> list[Path]:
    files = []
    for p in map(Path, paths):
        files += sorted(p.glob("*.json")) if p.is_dir() else [p]
    if not files:
        raise loader.LineageError(f"no .json files found in {paths}")
    return files


def normalize(paths, retries: int = 1):
    """-> (built, docs): validated 'built' structures for loader.load(), plus the normalized documents."""
    files = collect_files(paths)
    user = "\n\n".join(f"--- FILE: {f.name} ---\n{f.read_text(encoding='utf-8')}" for f in files)
    prompt, err = user, None
    for _ in range(retries + 1):
        raw = chat(PROMPT, prompt if not err else
                   f"{user}\n\nYour previous output was rejected: {err}\nFix it and output the JSON again.",
                   max_tokens=16000)
        try:
            data = extract_json(raw)
            if "error" in data:
                raise loader.LineageError(f"LLM found a problem in the data: {data['error']}")
            docs = data["documents"]
            built = [loader.validate_and_build(d, i + 1) for i, d in enumerate(docs)]  # fail loudly
            return built, docs
        except loader.LineageError as e:
            if str(e).startswith("LLM found"):
                raise  # a genuine data error: surface it, don't retry
            err = str(e)
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
            err = f"{type(e).__name__}: {e}"
    raise loader.LineageError(f"could not normalize the files: {err}")


def save(docs) -> Path:
    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / f"normalized_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps(docs, indent=2), encoding="utf-8")
    return out
