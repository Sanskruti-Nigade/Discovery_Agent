# Frontend - ask questions about the lineage graph

Type a question; the backend searches Neo4j (columns, tables, systems, properties, `MAPS_TO` lineage) and answers from what it finds.
If an LLM is configured in `../.env`, the answer is phrased by the model using only the retrieved facts; otherwise a rule-based answer is shown.

## Run
1. Neo4j running and loaded (`python ../lineage/loader.py`).
2. `pip install -r requirements.txt`
3. `uvicorn app:app --port 8080` (from this folder), then open http://localhost:8080

Credentials are read from `../.env` (`NEO4J_URI`, `NEO4J_USERNAME`/`NEO4J_USER`, `NEO4J_PASSWORD`, `NEO4J_DATABASE`).

## Example questions
- Where does dim_model.schema_version come from?
- What does model_product.id feed?
- columns of dim_product
