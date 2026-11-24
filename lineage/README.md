# Data lineage in Neo4j

Model: `SourceSystem|TargetSystem -OWNS-> Table -HAS_COLUMN-> Column -HAS_PROPERTY-> Property`,
plus `Column -MAPS_TO-> Column` (always source -> target).

## Setup
1. Neo4j. From the project folder, using the repo's docker-compose (Bolt 7687, browser http://localhost:7474):

   `docker compose up -d neo4j`

   Or standalone:

   `docker run -d --name neo4j -p 7474:7474 -p 7687:7687 -e NEO4J_AUTH=neo4j/<password> neo4j:5-community`
2. `pip install neo4j pyvis python-dotenv`
3. Credentials come from `../.env`: `NEO4J_URI`, `NEO4J_USERNAME` (or `NEO4J_USER`), `NEO4J_PASSWORD`, optional `NEO4J_DATABASE`.

## Run (from this folder)
1. `python loader.py [file-or-folder ...]` - validates everything, loads with MERGE. Default: every `*.json` in `json_files/` (replaces the old `yml_files`). `sample_data.json` is a larger multi-source example: `python loader.py sample_data.json`. Re-running is safe.
2. `python visualize.py` - writes `graph.html`; open it in a browser.
3. Explore the graph in Neo4j Browser (http://localhost:7474).

## Input
One JSON object or a list of them (see `sample_data.json`).
- Columns may be `"name"` or `{"name", "data_type", "properties": {k: v}}`.
- A table may be `{"schema": "...", "columns": [...]}`.
- Mapping entries accept optional `mapping_name` and `transformation`.
- A bad `from`/`to` reference aborts the whole load with an error naming it.

## Notes
- Property nodes are unique on (key, value), so columns sharing e.g. `nullable=false` share one Property node.
- MAPS_TO is unique per (source column, target column, mapping_name).
