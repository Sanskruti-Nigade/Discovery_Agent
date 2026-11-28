"""One-off maintenance for the review tables.

    python -m mapping.admin reset-reviews            # dry run: shows what would be deleted
    python -m mapping.admin reset-reviews --yes      # backs up both tables to JSON, then deletes every row
    python -m mapping.admin reset-reviews --yes --restart-ids   # also restarts review_id / decision_id at 1

Used to drop the old field-change review rows before switching to table-level schema mappings.
Deletes review_decision first (it references review_queue), in one transaction; nothing else is touched.
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import psycopg

from mapping import db

TABLES = ("review_decision", "review_queue")  # child first


def reset_reviews(yes: bool, restart_ids: bool, backup_dir: Path) -> int:
    with db.connect() as conn:
        counts = {t: conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"] for t in TABLES}
        info = psycopg.conninfo.conninfo_to_dict(db.dsn())  # never print the password
        print(f"{info.get('host')}:{info.get('port')}/{info.get('dbname')}  ->  "
              + ", ".join(f"{t}: {n} rows" for t, n in counts.items()))
        if not yes:
            print("Dry run. Re-run with --yes to back up and delete these rows.")
            return 0
        if not any(counts.values()):
            print("Nothing to delete.")
            return 0

        backup = {t: [dict(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()] for t in TABLES}
        backup_dir.mkdir(parents=True, exist_ok=True)
        path = backup_dir / f"review_backup_{datetime.now():%Y%m%d_%H%M%S}.json"
        path.write_text(json.dumps(backup, indent=1, default=str), encoding="utf-8")
        print(f"Backup written: {path}")

        for t in TABLES:
            conn.execute(f"DELETE FROM {t}")
        if restart_ids:
            for t, col in (("review_queue", "review_id"), ("review_decision", "decision_id")):
                seq = conn.execute("SELECT pg_get_serial_sequence(%s, %s) AS s", (t, col)).fetchone()["s"]
                if seq:
                    conn.execute(f"ALTER SEQUENCE {seq} RESTART WITH 1")
        conn.commit()
    print("Deleted: " + ", ".join(f"{t} {n}" for t, n in counts.items()) + (" (ids restarted)" if restart_ids else ""))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mapping.admin")
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reset-reviews", help="back up and delete every row of review_queue and review_decision")
    r.add_argument("--yes", action="store_true", help="really delete (default is a dry run)")
    r.add_argument("--restart-ids", action="store_true", help="restart review_id / decision_id sequences at 1")
    r.add_argument("--backup-dir", type=Path, default=Path("backups"), help="where to write the JSON backup")
    args = parser.parse_args(argv)
    try:
        return reset_reviews(args.yes, args.restart_ids, args.backup_dir)
    except psycopg.Error as e:
        print(f"PostgreSQL error: {str(e).splitlines()[0]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
