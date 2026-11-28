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
NL = chr(10)

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
- Every mapping "from"/"to" you output must be a table.column you listed.
- A mapping entry whose "from" or "to" is NOT a listed column (e.g. a read-only/computed field, a cross-table
  reference like "parentcustomerid.accountnumber", a column missing from the tables) must be LEFT OUT of "mapping" and
  recorded in a top-level "skipped" list: {"documents": [...], "skipped": [{"from": "...", "to": "...", "reason": "..."}]}.
  Do not invent columns for it and do not fail the whole file.
- If a mapping target is written as a description such as "sn_jobfunction (reference via _sn_jobfunction_value)",
  strip the parenthesised note and use the plain name ONLY if that exact column is listed; otherwise skip it.
- FLATTENING CONVENTION (do not ask the user about it): for nested JSON, a column is the leaf path from the entity,
  written exactly as the mapping file writes it, e.g. "profile[].mdm_id" or
  "profile[].communication[type=email && primary=true].value". Use these path strings VERBATIM as column names, for the
  source and target tables alike. Source tables are the entities ("contact", "account"); target tables are the target
  entities. Derive the column list from the sample records AND from every path the mapping refers to. A mapping entry
  with several targets becomes several entries; a filtered/derived path is still just a column named by that path.
- Never refuse or ask for smaller batches because the output is long: output everything. If a length limit cuts the
  reply, it will be continued from where it stopped.
- Output {"error": "<why>"} only when nothing at all can be loaded (e.g. no tables or systems found).
- If a column has an example value in the data (e.g. "ACTIVE": false), keep it as that column's
  "properties": {"sample": <the value as written>}, and set "data_type" from the JSON value type
  (true/false -> "boolean", number -> "number") when no type is stated. For text values do NOT guess a type
  (it may be a date or timestamp): leave data_type out.
- The reply must be complete, strictly valid JSON: escape every double quote and newline inside string values, no
  trailing commas, no comments. Keep "transformation" text short (one sentence, quotes escaped).
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


def split_mapping_groups(data):
    """Find a list of >=2 mapping groups (dicts that each hold a list of dict entries, e.g. 'mappings') anywhere in a
    parsed mapping file. -> a list of single-group copies of the document, or None if it has no such list."""
    def is_group(x):
        return isinstance(x, dict) and any(isinstance(v, list) and v and isinstance(v[0], dict) for v in x.values())

    def find(node, path):
        if isinstance(node, list) and len(node) >= 2 and all(is_group(x) for x in node):
            return path
        for k, v in (node.items() if isinstance(node, dict) else []):
            if (found := find(v, path + [k])) is not None:
                return found

    path = find(data, [])
    if path is None:
        return None
    chunks = []
    group_list = data
    for k in path:
        group_list = group_list[k]
    for g in group_list:
        copy = json.loads(json.dumps(data))
        node = copy
        for k in path[:-1]:
            node = node[k]
        node[path[-1]] = [g]
        chunks.append(copy)
    return chunks


def ask_llm(user: str, retries: int):
    """One LLM call (with retries) -> list of documents; each is validated before it is accepted."""
    prompt, err = user, None
    for _ in range(retries + 1):
        retry = NL.join([user, "", f"Your previous output was rejected: {err}", "Fix it and output the JSON again."])
        raw = chat(PROMPT, prompt if not err else retry, max_tokens=16000)
        try:
            data = extract_json(raw)
            if "error" in data:
                raise loader.LineageError(f"LLM found a problem in the data: {data['error']}")
            docs = data["documents"]
            for s in data.get("skipped", []):
                print(f"  skipped mapping {s.get('from')} -> {s.get('to')}: {s.get('reason')}")
            for i, d in enumerate(docs):
                loader.validate_and_build(d, i + 1)  # fail loudly
            return docs
        except loader.LineageError as e:
            if str(e).startswith("LLM found"):
                raise  # a genuine data error: surface it, don't retry
            err = str(e)
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
            err = f"{type(e).__name__}: {e}"
            OUT_DIR.mkdir(exist_ok=True)  # keep the bad reply so it can be inspected
            (OUT_DIR / "last_bad_reply.txt").write_text(raw, encoding="utf-8")
            print(f"  LLM reply was not valid JSON ({err}); raw reply saved to {OUT_DIR / 'last_bad_reply.txt'}; retrying")
    raise loader.LineageError(f"could not normalize the files: {err}")


def merge_docs(docs):
    """Combine documents of the same source->target pair: union of tables/columns, all mappings."""
    merged = {}
    for d in docs:
        m = merged.setdefault((d["source"].get("system"), d["target"].get("system")),
                              {"source": {**d["source"], "tables": {}}, "target": {**d["target"], "tables": {}},
                               "mapping": []})
        for side in ("source", "target"):
            for table, cols in d[side]["tables"].items():
                have = m[side]["tables"].setdefault(table, [])
                names = {c["name"] for c in have}
                have += [c for c in cols if c["name"] not in names]
        seen = {(x["from"], x["to"], x.get("mapping_name")) for x in m["mapping"]}
        m["mapping"] += [x for x in d.get("mapping", []) if (x["from"], x["to"], x.get("mapping_name")) not in seen]
    return list(merged.values())


def normalize(paths, retries: int = 3):
    """-> (built, docs): validated 'built' structures for loader.load(), plus the normalized documents.
    A mapping file holding several mapping groups is sent to the LLM one group at a time (a single reply for the
    whole thing would be too long), then the results are merged."""
    files = collect_files(paths)
    texts = {f: f.read_text(encoding="utf-8") for f in files}
    chunks = None
    for f, t in sorted(texts.items(), key=lambda ft: len(ft[1]), reverse=True):  # biggest file first: the mapping
        try:
            chunks = split_mapping_groups(json.loads(t))
        except json.JSONDecodeError:
            chunks = None
        if chunks:
            split_file = f
            break
    def block(skip=None):
        return (NL + NL).join(f"--- FILE: {f.name} ---" + NL + t for f, t in texts.items() if f != skip)
    if not chunks:
        docs = ask_llm(block(), retries)
    else:
        others = block(split_file)
        docs = []
        for n, c in enumerate(chunks, 1):
            print(f'  normalizing part {n}/{len(chunks)} of {split_file.name} ...')
            note = ('NOTE: this request holds ONE part of a larger mapping file. List only the source and target '
                    'tables that this part uses (every column of those tables), and only this part mappings. '
                    'Other parts are handled in separate requests.')
            head = f'--- FILE: {split_file.name} (part {n}/{len(chunks)}) ---'
            docs += ask_llm((NL + NL).join([note, others, head + NL + json.dumps(c)]), retries)
        docs = merge_docs(docs)
    built = [loader.validate_and_build(d, i + 1) for i, d in enumerate(docs)]
    return built, docs


def save(docs) -> Path:
    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / f"normalized_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps(docs, indent=2), encoding="utf-8")
    return out
