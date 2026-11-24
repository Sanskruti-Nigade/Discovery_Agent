# AI Cypher generation

The LLM reads any input data and writes the Cypher; the code validates it and saves it here for review.

| File | Role |
|---|---|
| `llm_client.py` | Chat client for `anthropic`, `openai` or `azure` (credentials from `../.env`) |
| `cypher_generator.py` | Prompt + validation (MERGE only, lineage labels/relationships only), saves output |
| `apply_query.py` | Runs a saved `.cypher` file as a dry run, shows the changes, commits only on approval |
| `generated/` | **Where the LLM-generated Cypher files are written** (`query_<timestamp>.cypher`) |

## Setup
In `../.env` set `LLM_PROVIDER` (anthropic | openai | azure) and its credentials:
`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `AZURE_OPENAI_ENDPOINT` + `AZURE_OPENAI_API_KEY` + `AZURE_OPENAI_CHAT_DEPLOYMENT`.
`LLM_PROVIDER=stub` cannot generate Cypher.

## Run
1. `python ai/cypher_generator.py lineage/json_files/*.json` -> writes `ai/generated/query_<time>.cypher`
2. Review that file.
3. `python ai/apply_query.py ai/generated/query_<time>.cypher` -> shows pending changes, asks to commit.

Bad references in the data make the LLM emit `// ERROR: ...`, and nothing is generated or written.
