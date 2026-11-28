"""Generate schema mappings for source tables in PostgreSQL by calling the mapping API.

For each source table/API (latest snapshot in metadata_snapshot) the script calls
    POST {api}/api/schema-mapping   {"source": "<object_id>", ...}
The API reads the source and target schemas, asks the LLM and stores the mappings as one pending batch in
review_queue (reviewed on the Reviews page; decisions go to review_decision). The script writes nothing itself.

Usage (repo root, venv active, PG_DSN in .env, the website or the mapping service running):
    python mapping/generate_mappings.py --list                    # show source tables, nothing is called
    python mapping/generate_mappings.py                           # every source whose latest version is not mapped yet
    python mapping/generate_mappings.py --all                     # every source table, mapped or not
    python mapping/generate_mappings.py --source mdm.product_api --source mdm.contact_api
    python mapping/generate_mappings.py --source product_api --targets snowflake.dim_model snowflake.dim_product

Options: --api-url (default MAPPING_API_URL or http://localhost:8080), --min-conf 0.6, --requested-by <name>.
Exit code 0 when every call succeeded, 1 otherwise.
"""
import argparse
import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # also runnable as `python mapping/generate_mappings.py`

from mapping import db  # noqa: E402
from mapping.db import ENVIRONMENT  # noqa: E402

SOURCES = """
WITH latest AS (
    SELECT DISTINCT ON (object_id) snapshot_id, object_id, object_type, version, captured_at
    FROM metadata_snapshot
    WHERE environment = %s AND object_type IN ('table', 'api')
    ORDER BY object_id, version DESC, captured_at DESC
)
SELECT l.object_id, l.object_type, l.version, l.captured_at,
       (SELECT count(*) FROM schema_field f WHERE f.snapshot_id = l.snapshot_id) AS field_count,
       EXISTS (SELECT 1 FROM review_queue q
               WHERE q.evidence->>'source' = 'schema_mapping'
                 AND q.evidence->>'source_object_id' = l.object_id
                 AND q.evidence->>'source_object_version' = l.version::text) AS mapped
FROM latest l
ORDER BY l.object_id
"""


def source_tables() -> list[dict]:
    """Latest snapshot of every source table/API, with whether that version already has a mapping batch."""
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(SOURCES, (ENVIRONMENT,)).fetchall()]


def generate(api_url: str, object_id: str, *, targets: list[str] | None, min_conf: float,
             requested_by: str, timeout: float, scope: str = "auto") -> dict:
    """One API call. Returns {"ok", "batch_id", "mappings", "already_pending", "error"}."""
    payload = {"source": object_id, "min_conf": min_conf, "requested_by": requested_by,
               "target_object_ids": targets or None, "scope": scope}
    try:
        r = requests.post(f"{api_url}/api/schema-mapping", json=payload, timeout=timeout)
    except requests.RequestException as e:
        return {"ok": False, "error": f"API not reachable at {api_url}: {e.__class__.__name__}"}
    try:
        body = r.json()
    except ValueError:
        body = {"detail": r.text[:300]}
    if not r.ok:
        return {"ok": False, "error": f"HTTP {r.status_code}: {body.get('detail', body)}"}
    return {"ok": True, "batch_id": body.get("batch_id"), "mappings": len(body.get("mappings", [])),
            "already_pending": len(body.get("already_pending", [])), "llm": body.get("llm") or {},
            "targets": len(body.get("target_object_ids", [])), "target_fields": body.get("target_fields_considered"),
            "scope": body.get("scope") or {}, "already_decided": len(body.get("already_decided", [])),
            "status": body.get("status")}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Generate schema mappings for PostgreSQL source tables via the API.")
    parser.add_argument("--source", action="append", default=[], metavar="OBJECT_ID",
                        help="source table to map (repeatable); default: every unmapped source")
    parser.add_argument("--all", action="store_true", help="map every source table, even if already mapped")
    parser.add_argument("--list", action="store_true", help="only list the source tables")
    parser.add_argument("--targets", nargs="+", metavar="OBJECT_ID", help="limit to these target tables")
    parser.add_argument("--api-url", default=os.getenv("MAPPING_API_URL", "http://localhost:8080"),
                        help="base URL of the website or mapping service (default %(default)s)")
    parser.add_argument("--min-conf", type=float, default=0.6, help="drop LLM mappings below this confidence")
    parser.add_argument("--requested-by", default=os.getenv("USERNAME") or os.getenv("USER") or "generate_mappings",
                        help="stored as requested_by on the batch (default: your login name)")
    parser.add_argument("--full", action="store_true",
                        help="send every field and re-propose already approved/rejected pairs "
                             "(default: only fields changed since the last mapped version, per change_diff)")
    parser.add_argument("--timeout", type=float, default=600, help="seconds to wait per table (LLM call)")
    args = parser.parse_args(argv)
    # accept either the base URL or the full endpoint
    api_url = args.api_url.rstrip("/").removesuffix("/api/schema-mapping")

    try:
        sources = source_tables()
    except Exception as e:
        print(f"Cannot read source tables from PostgreSQL: {str(e).splitlines()[0]}", file=sys.stderr)
        return 1
    print(f"{len(sources)} source table(s) in metadata_snapshot (environment={ENVIRONMENT})")
    for s in sources:
        state = "no fields" if not s["field_count"] else "mapped" if s["mapped"] else "unmapped"
        print(f"  {state:<9}  {s['object_id']:<40} v{s['version']}  "
              f"{s['field_count']} fields  ({s['object_type']})")
    if args.list:
        return 0

    empty = {s["object_id"] for s in sources if not s["field_count"]}
    if args.source:
        todo = args.source  # the API also accepts short names (product_api) and table names
    elif args.all:
        todo = [s["object_id"] for s in sources if s["object_id"] not in empty]
    else:
        todo = [s["object_id"] for s in sources if not s["mapped"] and s["object_id"] not in empty]
    if empty and not args.source:
        print(f"Skipped (no fields in schema_field): {', '.join(sorted(empty))}")
    if not todo:
        print("Nothing to map: every source table's latest version already has a mapping batch (use --all to redo).")
        return 0

    print(f"\nCalling {api_url}/api/schema-mapping for {len(todo)} table(s)...")
    failed = 0
    for i, object_id in enumerate(todo, 1):
        started = time.perf_counter()
        res = generate(api_url, object_id, targets=args.targets, min_conf=args.min_conf,
                       requested_by=args.requested_by, timeout=args.timeout,
                       scope="full" if args.full else "auto")
        took = time.perf_counter() - started
        if res["ok"] and res["status"] == "nothing_to_map":
            sc = res["scope"]
            print(f"[{i}/{len(todo)}] {object_id}: nothing to map - the new version only removed fields "
                  f"({', '.join(sc.get('removed_fields', [])) or 'none'}); no LLM call")
        elif res["ok"]:
            print(f"[{i}/{len(todo)}] {object_id}: {res['mappings']} mapping(s) queued for review, "
                  f"{res['already_pending']} already pending, {res['already_decided']} already decided  "
                  f"(batch {res['batch_id']}, {took:.1f}s)")
            sc = res["scope"]
            if sc.get("mode") == "changes_only":
                print(f"        new version: sent {sc['fields_sent_to_llm']} of {sc['fields_in_table']} fields "
                      f"(changed: {', '.join(sc['changed_fields'])})"
                      + (f"; removed: {', '.join(sc['removed_fields'])}" if sc.get("removed_fields") else ""))
            llm = res["llm"]
            if llm:
                print(f"        LLM proposed {llm.get('proposed')} against {res['targets']} target table(s) / "
                      f"{res['target_fields']} fields; dropped: {llm.get('unknown_fields')} with unknown field keys, "
                      f"{llm.get('below_min_conf')} below min_conf {llm.get('min_conf')}")
                for ex in llm.get("unknown_field_examples") or []:
                    print(f"          unknown: {ex}")
            if res["mappings"] == 0 and not res["already_pending"] and not res["already_decided"]:
                print("        Nothing was stored for this table (see the counts above).")
        else:
            failed += 1
            print(f"[{i}/{len(todo)}] {object_id}: FAILED - {res['error']}")
            if any(k in res["error"] for k in ("401", "403", "Unauthorized", "Forbidden", "is not set")):
                skipped = len(todo) - i
                print(f"\nStopped: the LLM key was rejected or is missing, so the other {skipped} table(s) would fail "
                      f"the same way.\nFix the key in .env, restart the API, check {api_url}"
                      f"/api/schema-mapping/health (llm.ok must be true), then run this again.")
                failed += skipped
                break
    print(f"\nDone: {len(todo) - failed} succeeded, {failed} failed. "
          "Pending mappings appear on the website's Reviews page (/reviews).")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
