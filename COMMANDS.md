# Discovery Agent – Command Reference

What each command does and when to use it. Run everything from the project root
(`Discovery_agent/`) unless noted. Settings and credentials are read from `.env`.

## Typical order (first-time setup)

| Step | Command | Purpose |
|---|---|---|
| 1 | `docker compose up -d neo4j` | Start Neo4j |
| 2 | `pip install -r requirements.txt` | Install Python packages |
| 3 | `python ai/sync.py` | Load the JSON lineage files into Neo4j (the LLM writes the Cypher) |
| 4 | `python ai/index_graph.py` | Create embeddings and the vector index |
| 5 | `cd frontend` then `python -m uvicorn app:app --port 8080` | Start the chat and graph website |

After you change the data in `lineage/json_files/`, repeat steps 3 and 4.

---

## 1. Neo4j database

| Command | What it does |
|---|---|
| `docker compose up -d neo4j` | Starts Neo4j in Docker. Bolt is on `7687`, the browser UI on http://localhost:7474 |
| `docker compose stop neo4j` | Stops Neo4j and keeps the data |
| `docker compose down` | Stops and removes the containers (data volume is kept) |
| `docker compose up -d` | Starts Neo4j and the API container together |

## 2. Loading lineage data (`lineage/`)

| Command | What it does |
|---|---|
| `python lineage/loader.py` | Only validates every `*.json` in `lineage/json_files/` (no Cypher, nothing written). Load with `python ai/sync.py` |
| `python lineage/loader.py <file-or-folder>` | Same, for a specific file or folder |
| `python lineage/visualize.py` | Writes `graph.html`, a standalone graph picture you can open in a browser |

Input files in `lineage/json_files/`:
- `source1_mdm.json`, `source2_snowflake.json`: tables and columns for each system.
- `mapping.json`: lines like `table.col -> table.col` that link source columns to target columns.

## 3. AI search (question → embedding → Neo4j → LLM answer)

| Command | What it does |
|---|---|
| `python ai/index_graph.py` | Turns every Column and Table node into an embedding and stores it in the Neo4j vector index `lineage_embeddings`. Re-run after reloading data |
| `python ai/ask.py "your question"` | Embeds the question, finds the closest graph nodes, adds the lineage around them, and the LLM answers from that |
| `python ai/ask.py "your question" --show-context` | Same, and also prints what was retrieved from Neo4j |

Needs Neo4j 5.11 or later. Embedding settings in `.env`: `EMBED_PROVIDER` (`local`, `openai` or `azure`) and `EMBED_MODEL`.
The answering model is set by `LLM_PROVIDER` and its API key.

## 4. AI-generated Cypher and change tracking (`ai/`)

| Command | What it does |
|---|---|
| `python ai/cypher_generator.py lineage/json_files/*.json` | The LLM writes Cypher for the data and saves it to `ai/generated/query_<time>.cypher` for review |
| `python ai/apply_query.py ai/generated/query_<time>.cypher` | Dry run: shows what would change, and commits only if you approve |
| `python ai/sync.py` | Loads the JSON, detects renames and data-type changes against what is in Neo4j, records them, and commits in one transaction after approval |
| `python ai/sync.py --yes` | Same, without the approval prompt |

Read queries (search, lineage, impact, history, graph view) are also LLM-written: each caller describes what it needs,
`ai/query_gen.py` has the LLM write the Cypher, checks it is read-only and returns the right columns, and caches it in
`ai/generated/read_queries.json` (delete that file to regenerate). The first run of each feature calls the LLM once.

## 5. Website (`frontend/`)

Start it from the `frontend/` folder:

| Command | What it does |
|---|---|
| `python -m uvicorn app:app --port 8080` | Starts the website |
| `python -m uvicorn app:app --port 8080 --reload` | Same, but restarts on code changes (restart by hand if a change does not show up) |

Pages and endpoints:

| URL | What it is |
|---|---|
| http://localhost:8080 | Chat Q&A. Ask about lineage, impact ("what happens if I change X") or change history |
| http://localhost:8080/graph | Interactive graph. Click a node to expand it, double-click to collapse |
| `GET /api/health` | Checks the Neo4j connection and node counts |
| `POST /api/ask` | The chat API, body `{"question": "..."}` |
| `GET /api/graph/roots` | The starting nodes of the graph (the systems) |
| `GET /api/graph/expand?id=<id>` | The children of a graph node |

Questions the chat understands:

| Ask like | You get |
|---|---|
| "What will happen if I change mdm_product_api" | A summary plus a table of every affected system, table and column |
| "mdm_product" (a partial name that matches 2 or more things) | Buttons to pick which one you mean |
| "Where does primary_email come from" | A lineage answer with the matching columns |
| "What was the previous type of …" | The change history |

## 5a. React UI and mapping reviews (`ui/`, `mapping/`)

| Command | What it does |
|---|---|
| `cd ui` then `npm install` and `npm run build` | Builds the ServiceNow-themed UI into `ui/dist`; the website above then serves it |
| `cd ui` then `npm run dev` | UI with hot reload on http://localhost:5173 (needs the website on 8080 running) |
| `python -m pytest tests -q` | Unit tests for schema mapping (no database needed) |
| `uvicorn mapping.main:app --port 8001` | Optional: run the schema-mapping API on its own (docs at http://localhost:8001/docs) |

| URL | What it is |
|---|---|
| http://localhost:8080/reviews | Mapping reviews: batches waiting for approval, and all reviews |
| `GET /api/schema-mapping/health` | PostgreSQL, Neo4j and LLM-key status (`?strict=true` returns 503 if any is down) |
| `POST /api/schema-mapping` | LLM maps a source table to the target tables; stores a pending batch in `review_queue` |
| `GET /api/schema-mapping/approved?since=…` | Approved mappings (`table.column -> table.column`) for loading into Neo4j |

See `mapping/README.md` and `ui/README.md`.

## 6. Older YAML loader API (`main.py`)

| Command | What it does |
|---|---|
| `python -m uvicorn main:app --port 8000` | Starts the YAML loader API |
| `GET /neo4j/status` | Checks the Neo4j connection |
| `POST /load` | Loads the `source*.yml` and `mapping*.yml` files from `yml_files/` into Neo4j |

## 7. Troubleshooting

| Problem | Fix |
|---|---|
| Port 8080 is already in use | An old server is still running. Stop it (Task Manager, or `Stop-Process -Id <pid>`) and start again |
| Code change does not show on the website | Restart the uvicorn server and hard-refresh the browser (Ctrl+F5) |
| `/graph` page is blank | Hard-refresh. The graph container needs a fixed height (already fixed in `graph.html`) |
| `Cannot yield value from void procedure` | Fixed in `ai/index_graph.py`. Pull the latest file |
| Loader says `'str' object has no attribute 'get'` | Fixed. `mapping.json` string lines are now supported |
| Vector search finds nothing | Run `python ai/index_graph.py` first. Use the same `EMBED_PROVIDER` for indexing and asking |
