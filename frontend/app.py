"""Chat frontend: question -> search Neo4j lineage graph -> answer.

Run (from this folder):  uvicorn app:app --port 8080   then open http://localhost:8080
"""
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from neo4j import GraphDatabase
from pydantic import BaseModel

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT / "ai"))
sys.path.insert(0, str(ROOT))

import query_gen  # noqa: E402
from mapping.api import router as schema_mapping_router  # noqa: E402
from query_gen import Q  # noqa: E402

URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
USER = os.getenv("NEO4J_USERNAME") or os.getenv("NEO4J_USER", "neo4j")
PASSWORD = os.getenv("NEO4J_PASSWORD", "")
DB = os.getenv("NEO4J_DATABASE", "neo4j")

STOP = set("""a an the is are was were be of in on at to for from by with and or what which who whom where when why how
does do did show me tell give list find get all any this that these those it its as about into than then there their
column columns table tables field fields data mapped map maps mapping come comes coming source sources target targets
used use uses upstream downstream lineage please can you i we our my""".split())
UP = re.compile(r"\b(come|comes|coming|source|sources|upstream|derived|originat\w*|populated)\b", re.I)
DOWN = re.compile(r"\b(downstream|used|use|feeds?|impact\w*|goes? to|flows? to|maps? to|target|targets)\b", re.I)

app = FastAPI(title="Discovery Agent - lineage Q&A")
app.include_router(schema_mapping_router)  # /api/schema-mapping/* (Postgres review queue, see mapping/)
_driver = None


def driver():
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(URI, auth=(USER, PASSWORD))
    return _driver


def run(q: Q, **params) -> list[dict]:
    """Run the LLM-written Cypher for q (see ai/query_gen.py); no query text lives in this file."""
    with driver().session(database=DB) as s:
        return query_gen.run(s, q, **params)


def keywords(q: str) -> list[str]:
    out = []
    for w in re.findall(r"[A-Za-z0-9_]+", q.lower()):
        if w in STOP or len(w) < 2:
            continue
        out.append(w)
        # also try the pieces of snake_case names ("model_product" -> model, product)
        out += [p for p in w.split("_") if p and p not in STOP and len(p) > 2 and p != w]
    return list(dict.fromkeys(out))


SEARCH_COLUMNS = Q(
    "ui_search_columns",
    "Keyword search over Columns. $kws is a list of lowercase keywords. A Column matches a keyword when its name, table "
    "or system contains it (case-insensitive) or one of its HAS_PROPERTY Property values (as string) contains it. Per "
    "Column collect the DISTINCT matched keywords as hit, and score = size(hit)*2 + bonus, where bonus = 3 if the lowercase "
    "column name is exactly in $kws, plus 2 if any keyword is contained in the lowercase column name, plus 1 if any "
    "keyword is contained in the lowercase table. Return system, `table` (the column's table), name, data_type, score, "
    "ordered by score desc, system, table, name; LIMIT 8.",
    {"kws": ["model"]}, ["system", "table", "name", "data_type", "score"])

COLUMN_DETAIL = Q(
    "ui_column_detail",
    "For the one Column with system=$system, table=$table, name=$name return: props = list of DISTINCT maps "
    "{key, value} of its HAS_PROPERTY Properties; ups = list of DISTINCT maps {system, table, name, via, transformation} "
    "of the Columns that MAP_TO it (via = the relationship's mapping_name); downs = the same for the Columns it MAPS_TO. "
    "Always exactly one row, with empty lists when there is nothing.",
    {"system": "s", "table": "t", "name": "n"}, ["props", "ups", "downs"])

SEARCH_TABLES = Q(
    "ui_search_tables",
    "Tables whose lowercase name contains any keyword in $kws. For each return its system, name as `table`, n = number of "
    "its HAS_COLUMN Columns, cols = the names of its first 12 columns. DISTINCT rows, LIMIT 5.",
    {"kws": ["model"]}, ["system", "table", "n", "cols"])

TABLE_NAMES = Q("ui_table_names", "One row per Table: t = lowercase table name.", {}, ["t"])
COLUMN_NAMES = Q("ui_column_names", "DISTINCT lowercase Column names, as c.", {}, ["c"])
SYSTEM_NAMES = Q("ui_system_names", "One row per SourceSystem or TargetSystem node: s = lowercase name.", {}, ["s"])
NAME_KINDS = Q(
    "ui_name_kinds",
    "DISTINCT names of every SourceSystem, TargetSystem and Table node as n, with kind = 'table' for Tables and "
    "'system' otherwise.", {}, ["n", "kind"])


def clean(items):
    return [i for i in items if i.get("name")]


def search(question: str) -> dict:
    kws = keywords(question)
    if not kws:
        return {"keywords": [], "columns": [], "tables": []}
    cols = run(SEARCH_COLUMNS, kws=kws)
    for c in cols:
        d = run(COLUMN_DETAIL, system=c["system"], table=c["table"], name=c["name"])[0]
        c["properties"] = [p for p in d["props"] if p["key"]]
        c["upstream"], c["downstream"] = clean(d["ups"]), clean(d["downs"])
    return {"keywords": kws, "columns": cols, "tables": run(SEARCH_TABLES, kws=kws)}


def ref(x):
    return f"{x['system']}.{x['table']}.{x['name']}"


def friendly(x):
    return f"{x['table']}.{x['name']} (in {x['system']})"


def rule_answer(question: str, r: dict) -> str:
    cols = r["columns"]
    if not cols:
        if r["tables"]:
            t = r["tables"][0]
            return f"The table {t['table']} in {t['system']} has {t['n']} columns: {', '.join(t['cols'])}."
        return "Sorry, I could not find anything about that. Try a column, table or system name."
    want_up, want_down = bool(UP.search(question)), bool(DOWN.search(question))
    out = []
    for c in cols[:3]:
        kind = f", which stores {c['data_type'].lower()} values" if c["data_type"] else ""
        out.append(f"{c['name']} is a column in the table {c['table']} (in {c['system']}){kind}.")
        if want_up or not want_down:
            for u in c["upstream"]:
                how = f" It is changed like this: {u['transformation']}." if u.get("transformation") else ""
                out.append(f"Its value comes from {friendly(u)}.{how}")
        if want_down or not want_up:
            for d in c["downstream"]:
                how = f" It is changed like this: {d['transformation']}." if d.get("transformation") else ""
                out.append(f"Its value is passed on to {friendly(d)}.{how}")
        if not c["upstream"] and not c["downstream"]:
            out.append("It is not linked to any other column.")
        out.append("")
    return chr(10).join(out).strip()


def llm_answer(question: str, r: dict) -> str | None:
    try:
        from llm_client import chat  # reuses ../ai/llm_client.py and ../.env
        facts = chr(10).join(
            f"- column {c['name']} in table {c['table']} of system {c['system']}, type {c['data_type']}; "
            f"value comes from: {[friendly(u) for u in c['upstream']] or 'nothing'}; "
            f"value is passed to: {[friendly(d) for d in c['downstream']] or 'nothing'}; "
            f"extra info: { {p['key']: p['value'] for p in c['properties'] if p['key'] != 'data_type'} }"
            for c in r["columns"][:5])
        facts += "".join(f"{chr(10)}- table {t['table']} in {t['system']} has columns {t['cols']}" for t in r["tables"])
        return chat(
            "You explain data to a non-technical business user. Use ONLY the facts given. "
            "Answer in 1-3 short, simple sentences in everyday words, then optionally up to 4 short bullet lines "
            "starting with '- ' for details. No jargon (say 'comes from' / 'is passed to', not lineage, upstream, "
            "MAPS_TO, schema). No markdown symbols such as ** or backticks. Mention the table and system names "
            "so the user knows where to look. If the facts do not answer the question, say so simply.",
            f"Question: {question}{chr(10)*2}Facts:{chr(10)}{facts}", max_tokens=500)
    except Exception:
        return None


IMPACT = re.compile(r"(impact|affect|break|depend|chang|cahng|chag|chnag|cahge|renam|drop|delet|remov|modif|alter|"
                    r"happen|what if|if i )", re.I)

IMPACT_ROOTS = Q(
    "ui_impact_roots",
    "Columns selected by lowercase filters. $tables, $cols, $systems are lists of lowercase names. A Column matches when "
    "(the table list is empty OR lowercase column table IN $tables) AND (cols empty OR lowercase column name IN $cols) AND "
    "(systems empty OR lowercase column system IN $systems) AND at least one of the three lists is non-empty. "
    "Return system, `table`, name; LIMIT 200.",
    {"tables": ["t"], "cols": ["c"], "systems": []}, ["system", "table", "name"])

IMPACT_DOWN = Q(
    "ui_impact_down",
    "Starting at the Column with system=$system, table=$table, name=$name, find every downstream Column reachable by "
    "MAPS_TO paths of 1 to 8 hops. For each path return the end Column's system, `table`, name, data_type, level = path "
    "length, via = list of 'table.name' strings of the intermediate Columns (nodes of the path excluding first and last). "
    "Order by level, system, table, name.",
    {"system": "s", "table": "t", "name": "n"}, ["system", "table", "name", "data_type", "level", "via"])


def impact_search(kws: list[str]) -> dict | None:
    """Find what is impacted (everything downstream) if the named column / table changes."""
    tnames = {r["t"] for r in run(TABLE_NAMES)}
    cnames = {r["c"] for r in run(COLUMN_NAMES)}
    snames = {r["s"] for r in run(SYSTEM_NAMES)}
    systems = [k for k in kws if k in snames]
    tables = [k for k in kws if k in tnames]
    cols = [k for k in kws if k in cnames]
    if not tables and not cols and not systems:
        return None
    roots = run(IMPACT_ROOTS, tables=tables, cols=cols, systems=systems)
    if not roots and tables and cols:  # column word was not really a column of that table
        roots = run(IMPACT_ROOTS, tables=tables, cols=[], systems=systems)
    rows, seen = [], set()
    for root in roots:
        for d in run(IMPACT_DOWN, **root):
            key = (root["table"], root["name"], d["system"], d["table"], d["name"])
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "changed": f"{root['system']}.{root['table']}.{root['name']}",
                "system": d["system"], "table": d["table"], "column": d["name"],
                "data_type": d["data_type"] or "", "level": d["level"],
                "path": " > ".join([f"{root['table']}.{root['name']}", *d["via"], f"{d['table']}.{d['name']}"]),
            })
    return {"roots": roots, "rows": rows}


def impact_answer(res: dict) -> str:
    roots, rows = res["roots"], res["rows"]
    if not roots:
        return "I could not find that column or table in the graph."
    names = sorted({f"{r['table']}.{r['name']}" for r in roots})
    label = names[0] if len(names) == 1 else f"{len(names)} columns"
    if not rows:
        return f"Good news: nothing else depends on {label}. Changing it will not affect any other table or column."
    tbls = sorted({(r["system"], r["table"]) for r in rows})
    return (f"If you change {label}, {len(rows)} column(s) in {len(tbls)} table(s) will be affected: "
            f"{', '.join(t for _, t in tbls)}. Details are in the table below.")


def impact_words(question: str, res: dict) -> str:
    """Plain-words explanation of the impact (LLM if available, otherwise built from the rows)."""
    base = impact_answer(res)
    if not res["rows"]:
        return base
    try:
        from llm_client import chat
        nl = chr(10)
        facts = nl.join(f"- {r['changed']} -> {r['table']}.{r['column']} in {r['system']} ({r['level']} step(s); path {r['path']})"
                        for r in res["rows"][:40])
        text = chat(
            "You explain the impact of a data change to a non-technical business user. Use ONLY the facts given. "
            "Write 2-3 short, simple sentences in everyday words saying what would be affected and why, then up to "
            "4 short bullet lines starting with '- '. No jargon, no markdown symbols like ** or backticks. "
            "Name tables and systems. Do not repeat every row; a table with all rows is shown separately.",
            f"Question: {question}{nl*2}Affected columns:{nl}{facts}", max_tokens=450)
        return text.strip() or base
    except Exception:
        by = {}
        for r in res["rows"]:
            by.setdefault(f"{r['table']} (in {r['system']})", []).append(r["column"])
        lines = [base, ""] + [f"- {t}: {', '.join(c)}" for t, c in by.items()]
        return chr(10).join(lines)


HISTORY = re.compile(r"(history|previous|prev|earlier|older|before|latest|recent|rename|data ?type|type change|what changed|"
                     r"changes made|change log|changelog|last change|old type|old name)", re.I)

HISTORY_QUERY = Q(
    "ui_history",
    "Every relationship of type CHANGED_TO or RENAMED_TO between any two nodes a and b. Return kind = type(r), "
    "a = properties(a), b = properties(b), r = properties(r).",
    {}, ["kind", "a", "b", "r"])


def parse_time(t):
    from datetime import datetime, timezone
    try:
        d = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def history_search(kws: list[str]) -> dict:
    rows = []
    for x in run(HISTORY_QUERY):
        a, b, r = x["a"], x["b"], x["r"]
        if x["kind"] == "CHANGED_TO":
            what, old, new = "Data type changed", a.get("value"), b.get("value")
            system, table, column = r.get("system"), r.get("table"), r.get("column")
        else:
            what, old, new = "Renamed", a.get("name"), b.get("name")
            system, table, column = a.get("system") or r.get("system"), a.get("table") or r.get("table"), a.get("name")
        when = parse_time(r.get("detected_at"))
        rows.append({"when": when.strftime("%Y-%m-%d %H:%M UTC") if r.get("detected_at") else "unknown", "_t": when,
                     "what": what, "system": system, "table": table, "column": column, "previous": old, "latest": new})
    named = [k for k in kws if any(k in (str(v or "").lower()) for r in rows for v in (r["table"], r["column"], r["system"]))
             and k not in ("latest", "previous", "recent")]
    exact = [k for k in named if any(k == str(v or "").lower() for r in rows for v in (r["table"], r["column"], r["system"]))]
    if exact:
        rows = [r for r in rows if any(k in (str(r["table"]).lower(), str(r["column"]).lower(), str(r["system"]).lower()) for k in exact)]
    rows.sort(key=lambda r: r["_t"], reverse=True)  # newest first
    for i, r in enumerate(rows):
        r["order"] = "Latest" if i == 0 else f"Earlier #{i}"
        del r["_t"]
    return {"rows": rows}


def history_answer(res: dict) -> str:
    rows = res["rows"]
    if not rows:
        return "No changes (renames or data type changes) have been recorded yet."
    def line(r):
        if r["what"] == "Renamed":
            return f"the column {r['previous']} in table {r['table']} ({r['system']}) was renamed to {r['latest']}"
        return (f"the column {r['column']} in table {r['table']} ({r['system']}) changed its data type "
                f"from {r['previous']} to {r['latest']}")
    out = [f"The latest change was on {rows[0]['when']}: {line(rows[0])}."]
    if len(rows) > 1:
        out.append("Before that:")
        out += [f"- {r['when']}: {line(r)}." for r in rows[1:6]]
    return chr(10).join(out)


def ambiguous_name(q: str) -> dict | None:
    """A word in the question that is only PART of 2+ system/table names (e.g. 'mdm_product' ->
    mdm_product_api, mdm_product_contact_api) and exactly none of them: ask the user which one."""
    names = [(r["n"], r["kind"]) for r in run(NAME_KINDS)]
    exact = {n.lower() for n, _ in names}
    for w in re.findall(r"[A-Za-z0-9_]{3,}", q):
        lw = w.lower()
        if lw in exact or lw in STOP:
            continue
        opts = sorted({(n, k) for n, k in names if lw in n.lower()})
        if len(opts) >= 2:
            return {"term": w, "options": [{"name": n, "kind": k} for n, k in opts]}
    return None


class Ask(BaseModel):
    question: str


@app.post("/api/ask")
def ask(body: Ask):
    q = body.question.strip()
    if not q:
        return {"answer": "Please ask a question.", "matches": []}
    try:
        amb = ambiguous_name(q)
        if amb:
            return {"answer": f"'{amb['term']}' matches {len(amb['options'])} things in the graph. Which one do you mean?",
                    "clarify": amb, "matches": []}
        if HISTORY.search(q):
            h = history_search(keywords(q))
            return {"answer": history_answer(h), "history": h["rows"], "matches": []}
        if IMPACT.search(q):
            res = impact_search(keywords(q))
            if res is not None:
                return {"answer": impact_words(q, res).replace("**", "").replace("`", ""), "impact": res["rows"], "matches": []}
        r = search(q)
    except Exception as e:
        return {"answer": f"Could not query Neo4j ({URI}): {e}", "matches": [], "error": True}
    answer = (llm_answer(q, r) if r["columns"] or r["tables"] else None) or rule_answer(q, r)
    answer = answer.replace("**", "").replace("`", "")
    return {"answer": answer, "matches": r["columns"], "tables": r["tables"], "keywords": r["keywords"]}


HEALTH_COUNTS = Q("ui_health_counts", "Number of nodes per first label: label = labels(n)[0], n = count; order by n desc.",
                  {}, ["label", "n"])
GRAPH_ROOTS = Q("ui_graph_roots", "Every SourceSystem or TargetSystem node: id = elementId(n), l = labels(n), "
                "p = properties(n).", {}, ["id", "l", "p"])
GRAPH_EXPAND = Q(
    "ui_graph_expand",
    "OUTGOING relationships of the node with elementId = $id, excluding relationships of type CHANGED_TO. For each "
    "return id = elementId of the target node, l = its labels, p = its properties, t = the relationship type.",
    {"id": "4:x:1"}, ["id", "l", "p", "t"])


@app.get("/api/health")
def health():
    try:
        driver().verify_connectivity()
        return {"neo4j": True, "counts": run(HEALTH_COUNTS)}
    except Exception as e:
        return {"neo4j": False, "error": str(e)}


@app.get("/api/graph/roots")
def graph_roots():
    """Starting nodes: the systems."""
    return _graph_nodes(run(GRAPH_ROOTS), [])


@app.get("/api/graph/expand")
def graph_expand(id: str):
    """Follow OUTGOING links only: system -> tables -> columns -> properties (and column -> mapped column).
    A Property (e.g. a data type) has no outgoing links to columns, so clicking it does not reveal them."""
    rows = run(GRAPH_EXPAND, id=id)
    return _graph_nodes(rows, [{"from": id, "to": r["id"], "label": r["t"]} for r in rows])


COLORS = {"SourceSystem": "#1F77B4", "TargetSystem": "#2CA02C", "Table": "#FF7F0E", "Column": "#9467BD", "Property": "#7F7F7F"}


def _graph_nodes(rows, edges):
    nodes = []
    for r in rows:
        lab = r["l"][0]
        p = r["p"]
        label = f"{p.get('key')}={p.get('value')}" if lab == "Property" else p.get("name", "")
        nodes.append({"id": r["id"], "label": label, "group": lab, "color": COLORS.get(lab, "#999"),
                      "title": chr(10).join([lab] + [f"{k}: {v}" for k, v in p.items()])})
    return {"nodes": nodes, "edges": edges}


app.mount("/lib", StaticFiles(directory=ROOT / "lineage" / "lib" / "vis-9.1.2"), name="lib")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

# The React UI (ui/, built with `npm run build`) is served from ui/dist when it exists. Without a build
# the original HTML pages are served, so the chat keeps working.
UI_DIST = ROOT / "ui" / "dist"
if (UI_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=UI_DIST / "assets"), name="assets")


def _page(classic: str):
    if (UI_DIST / "index.html").is_file():
        return FileResponse(UI_DIST / "index.html")
    return FileResponse(Path(__file__).parent / "static" / classic)


@app.get("/graph")
def graph_page():
    return _page("graph.html")


@app.get("/reviews")
def reviews_page():
    return _page("index.html")


@app.get("/")
def index():
    return _page("index.html")


@app.get("/{name}.{ext}")
def ui_public_file(name: str, ext: str):
    """Files from ui/public copied into the build (logo, favicon)."""
    p = UI_DIST / f"{name}.{ext}"
    if UI_DIST.is_dir() and p.is_file() and p.parent == UI_DIST:
        return FileResponse(p)
    from fastapi import HTTPException
    raise HTTPException(404)
