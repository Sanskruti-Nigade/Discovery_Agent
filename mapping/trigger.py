"""Generate schema mappings automatically when the ingestion job writes a source table to PostgreSQL.

How it works
1. A PostgreSQL trigger on metadata_snapshot sends a NOTIFY (channel `schema_mapping_source`) for every inserted
   snapshot: a source table/API (initial ingestion or a new schema version) or a target table.
2. This listener receives it and calls the mapping endpoint  POST {MAPPING_API_URL}  {"source": "<object_id>"},
   which reads the schemas, asks the LLM and stores a pending batch in review_queue for human review.

Postgres can't call HTTP itself, so the trigger only notifies; this small process does the call.

    python -m mapping.trigger install     # create the trigger + function in the database (once)
    python -m mapping.trigger run         # the listener: catch up, then wait for new snapshots (keep it running)
    python -m mapping.trigger catch-up    # one pass: map every source whose latest version has no batch yet
    python -m mapping.trigger status      # is the trigger installed? which sources still need a mapping?
    python -m mapping.trigger uninstall

Behaviour
- Each (source, version) is mapped once: if review_queue already holds a schema-mapping batch for that version,
  it is skipped. A new version of a source table is mapped again (pairs already pending are not duplicated).
- Notifications are debounced (MAPPING_TRIGGER_DEBOUNCE_SECONDS, default 30) so an ingestion run that writes the
  source and then the targets is mapped once, after it finishes.
- "No target tables yet" waits for the next target snapshot (or retries every MAPPING_TRIGGER_RETRY_SECONDS);
  LLM / network / database errors retry with backoff, up to MAPPING_TRIGGER_MAX_ATTEMPTS.
- On start, `run` catches up on anything missed while it was stopped (and, after the review tables were reset,
  generates the initial mapping for every existing source table).

Settings (.env): PG_DSN or POSTGRES_*, ENVIRONMENT, MAPPING_API_URL (default
http://localhost:8080/api/schema-mapping; use http://localhost:8001/api/schema-mapping for the standalone service),
MAPPING_TRIGGER_MIN_CONF (default 0.6).
"""
import argparse
import json
import logging
import os
import sys
import time

import psycopg
import requests

from mapping import db
from mapping.db import ENVIRONMENT

log = logging.getLogger("mapping.trigger")

CHANNEL = "schema_mapping_source"
REQUESTED_BY = "svc-schema-mapping-trigger"
API_URL = os.getenv("MAPPING_API_URL", "http://localhost:8080/api/schema-mapping").rstrip("/")
DEBOUNCE = float(os.getenv("MAPPING_TRIGGER_DEBOUNCE_SECONDS", "30"))
RETRY = float(os.getenv("MAPPING_TRIGGER_RETRY_SECONDS", "300"))
MAX_ATTEMPTS = int(os.getenv("MAPPING_TRIGGER_MAX_ATTEMPTS", "5"))
MIN_CONF = float(os.getenv("MAPPING_TRIGGER_MIN_CONF", "0.6"))
HTTP_TIMEOUT = float(os.getenv("MAPPING_TRIGGER_HTTP_TIMEOUT", "600"))  # the LLM call can take minutes

INSTALL_SQL = f"""
CREATE OR REPLACE FUNCTION schema_mapping_notify_snapshot() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_notify('{CHANNEL}', json_build_object(
    'object_id', NEW.object_id, 'object_type', NEW.object_type, 'version', NEW.version,
    'environment', NEW.environment, 'snapshot_id', NEW.snapshot_id)::text);
  RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS trg_schema_mapping_snapshot ON metadata_snapshot;
CREATE TRIGGER trg_schema_mapping_snapshot
  AFTER INSERT ON metadata_snapshot
  FOR EACH ROW WHEN (NEW.object_type IN ('table', 'api', 'target_object'))
  EXECUTE FUNCTION schema_mapping_notify_snapshot();
"""

UNINSTALL_SQL = """
DROP TRIGGER IF EXISTS trg_schema_mapping_snapshot ON metadata_snapshot;
DROP FUNCTION IF EXISTS schema_mapping_notify_snapshot();
"""

# latest version of every source table/API that has no schema-mapping batch for that version yet
UNMAPPED_SOURCES = """
WITH latest AS (
    SELECT DISTINCT ON (object_id) object_id, version
    FROM metadata_snapshot
    WHERE environment = %s AND object_type IN ('table', 'api')
    ORDER BY object_id, version DESC, captured_at DESC
)
SELECT l.object_id, l.version FROM latest l
WHERE NOT EXISTS (
    SELECT 1 FROM review_queue q
    WHERE q.evidence->>'source' = 'schema_mapping'
      AND q.evidence->>'source_object_id' = l.object_id
      AND q.evidence->>'source_object_version' = l.version::text)
ORDER BY l.object_id
"""

HAS_BATCH = """
SELECT 1 FROM review_queue
WHERE evidence->>'source' = 'schema_mapping' AND evidence->>'source_object_id' = %s
  AND evidence->>'source_object_version' = %s
LIMIT 1
"""


# ---------------------------------------------------------------- install / status
def install() -> None:
    with db.connect() as conn:
        conn.execute(INSTALL_SQL)
        conn.commit()
    print(f"Trigger trg_schema_mapping_snapshot installed on metadata_snapshot (NOTIFY channel '{CHANNEL}').")


def uninstall() -> None:
    with db.connect() as conn:
        conn.execute(UNINSTALL_SQL)
        conn.commit()
    print("Trigger and function removed.")


def unmapped_sources() -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(UNMAPPED_SOURCES, (ENVIRONMENT,)).fetchall()]


def status() -> None:
    with db.connect() as conn:
        installed = conn.execute("SELECT 1 FROM pg_trigger WHERE tgname = 'trg_schema_mapping_snapshot'").fetchone()
    print(f"Trigger installed: {'yes' if installed else 'NO (run: python -m mapping.trigger install)'}")
    print(f"Mapping endpoint:  {API_URL}")
    todo = unmapped_sources()
    print(f"Sources without a mapping for their latest version ({ENVIRONMENT}): {len(todo)}")
    for s in todo:
        print(f"  {s['object_id']} v{s['version']}")


# ---------------------------------------------------------------- calling the endpoint
class Outcome:
    DONE, WAIT_FOR_TARGETS, RETRY, GIVE_UP = "done", "wait_for_targets", "retry", "give_up"


def call_endpoint(object_id: str) -> tuple[str, str]:
    """POST the source table name to the mapping endpoint. Returns (outcome, message)."""
    try:
        r = requests.post(API_URL, json={"source": object_id, "requested_by": REQUESTED_BY, "min_conf": MIN_CONF},
                          timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        return Outcome.RETRY, f"endpoint not reachable: {e}"
    try:
        body = r.json()
    except ValueError:
        body = {"detail": r.text[:300]}
    if r.ok:
        return Outcome.DONE, (f"batch {body.get('batch_id')}: {len(body.get('mappings', []))} mapping(s) queued, "
                              f"{len(body.get('already_pending', []))} already pending")
    detail = str(body.get("detail", body))
    if r.status_code == 422 and "No target tables" in detail:
        return Outcome.WAIT_FOR_TARGETS, detail
    if r.status_code in (404, 422):
        return Outcome.GIVE_UP, f"HTTP {r.status_code}: {detail}"
    return Outcome.RETRY, f"HTTP {r.status_code}: {detail}"


def already_mapped(object_id: str, version: int) -> bool:
    with db.connect() as conn:
        return conn.execute(HAS_BATCH, (object_id, str(version))).fetchone() is not None


# ---------------------------------------------------------------- the listener
class Listener:
    def __init__(self):
        self.pending: dict[str, dict] = {}   # object_id -> {version, due, attempts, waiting_for_targets}

    def enqueue(self, object_id: str, version: int, delay: float = DEBOUNCE) -> None:
        item = self.pending.get(object_id)
        if item and item["version"] > version:
            return
        attempts = item["attempts"] if item and item["version"] == version else 0
        self.pending[object_id] = {"version": version, "due": time.time() + delay, "attempts": attempts,
                                   "waiting_for_targets": False}
        log.info("Queued %s v%s (runs in %.0fs)", object_id, version, delay)

    def on_notify(self, payload: str) -> None:
        try:
            msg = json.loads(payload)
        except ValueError:
            log.warning("Ignoring bad notification payload: %s", payload[:200])
            return
        if msg.get("environment") != ENVIRONMENT:
            return
        if msg.get("object_type") == "target_object":
            # a target table arrived: sources that were waiting for targets can run now (after the debounce)
            for item in self.pending.values():
                if item["waiting_for_targets"]:
                    item["waiting_for_targets"], item["due"] = False, time.time() + DEBOUNCE
            return
        self.enqueue(msg["object_id"], int(msg["version"]))

    def process_due(self) -> None:
        now = time.time()
        for object_id, item in sorted(self.pending.items()):
            if item["due"] > now:
                continue
            version = item["version"]
            if already_mapped(object_id, version):
                log.info("%s v%s already has a mapping batch; skipped", object_id, version)
                del self.pending[object_id]
                continue
            item["attempts"] += 1
            log.info("Generating mapping for %s v%s (attempt %d) via %s", object_id, version, item["attempts"], API_URL)
            outcome, message = call_endpoint(object_id)
            if outcome == Outcome.DONE:
                log.info("%s v%s: %s", object_id, version, message)
                del self.pending[object_id]
            elif outcome == Outcome.GIVE_UP or item["attempts"] >= MAX_ATTEMPTS:
                log.error("%s v%s: not mapped (%s)%s", object_id, version, message,
                          "" if outcome == Outcome.GIVE_UP else f"; gave up after {item['attempts']} attempts")
                del self.pending[object_id]
            elif outcome == Outcome.WAIT_FOR_TARGETS:
                log.warning("%s v%s: no target tables yet; waiting for a target snapshot", object_id, version)
                item["waiting_for_targets"], item["due"] = True, now + RETRY
            else:
                delay = min(RETRY * 2 ** (item["attempts"] - 1), 3600)
                log.warning("%s v%s: %s; retrying in %.0fs", object_id, version, message, delay)
                item["due"] = now + delay

    def catch_up(self) -> None:
        todo = unmapped_sources()
        log.info("Catch-up: %d source(s) without a mapping for their latest version", len(todo))
        for s in todo:
            self.enqueue(s["object_id"], s["version"], delay=0)

    def run_once(self) -> int:
        """catch-up command: one attempt per unmapped source, no waiting or retries. Returns how many are left."""
        self.catch_up()
        global MAX_ATTEMPTS
        MAX_ATTEMPTS = 1
        self.process_due()
        left = unmapped_sources()
        for src in left:
            log.warning("Still unmapped: %s v%s", src["object_id"], src["version"])
        return len(left)

    def run(self, catch_up: bool = True) -> None:
        log.info("Listening on '%s' (environment=%s, endpoint=%s)", CHANNEL, ENVIRONMENT, API_URL)
        first = True
        while True:
            try:
                with psycopg.connect(db.dsn(), autocommit=True) as conn:
                    conn.execute(f"LISTEN {CHANNEL}")
                    if catch_up or not first:  # after a reconnect, catch up on anything missed
                        self.catch_up()
                    first = False
                    while True:
                        for n in conn.notifies(timeout=1.0):
                            self.on_notify(n.payload)
                        self.process_due()
            except psycopg.OperationalError as e:
                log.error("PostgreSQL connection lost (%s); reconnecting in 10s", str(e).splitlines()[0])
                time.sleep(10)


def main(argv=None) -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="python -m mapping.trigger", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("install", help="create the trigger on metadata_snapshot")
    sub.add_parser("uninstall", help="remove the trigger")
    sub.add_parser("status", help="trigger installed? sources still to map?")
    sub.add_parser("catch-up", help="map every source whose latest version has no batch, then exit")
    run = sub.add_parser("run", help="listen for new snapshots and generate mappings (keep running)")
    run.add_argument("--no-catch-up", action="store_true", help="don't map existing unmapped sources on start")
    args = parser.parse_args(argv)

    if args.cmd == "install":
        install()
    elif args.cmd == "uninstall":
        uninstall()
    elif args.cmd == "status":
        status()
    elif args.cmd == "catch-up":
        return 1 if Listener().run_once() else 0
    else:
        Listener().run(catch_up=not args.no_catch_up)
    return 0


if __name__ == "__main__":
    sys.exit(main())
