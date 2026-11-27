"""CLI:  python -m graphload --source S.json --target T.json --mapping M.json [--dry-run] [--yes | --remap-all]

If the data differs from what is already in Neo4j you are asked first:
  small change        -> the changes are listed and you confirm ("okay, you got it?")
  almost all changed  -> you are asked whether to REMAP EVERYTHING (old mappings cleared, new ones loaded)
"""
import argparse
import logging
import sys

from . import ChangeConfirmationRequired, LoadError, load

ap = argparse.ArgumentParser(prog="graphload", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--mapping-only", metavar="FILE", help="load from the mapping file ALONE (it lists the systems, tables, columns and mappings)")
ap.add_argument("--no-llm-group", action="store_true", help="with --mapping-only: group source tables by name prefix instead of asking the LLM")
ap.add_argument("--source")
ap.add_argument("--target")
ap.add_argument("--mapping")
ap.add_argument("--source-name", default="Source")
ap.add_argument("--target-name", default="Target")
ap.add_argument("--dry-run", action="store_true", help="show the detected changes, run everything, then roll back")
ap.add_argument("--prune", action="store_true", help="also delete nodes/links from earlier loads that are no longer present")
ap.add_argument("--include-samples", action="store_true", help="store a sample value per column (may contain personal data)")
ap.add_argument("--llm-model", action="store_true", help="let the LLM propose label names (it sees structure only, never values)")
ap.add_argument("--model-file", help="JSON graph model: used if it exists; written here when --llm-model creates one")
ap.add_argument("--no-constraints", action="store_true")
ap.add_argument("--yes", action="store_true", help="accept a SMALL set of detected changes without asking")
ap.add_argument("--remap-all", action="store_true", help="the source changed entirely: clear old mappings and rebuild everything")
a = ap.parse_args()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def ask(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:  # no input available: treat as "no", never as consent
        return False


if not a.mapping_only and not (a.source and a.target):
    ap.error("give --mapping-only FILE, or both --source and --target")
kwargs = dict(mapping_only=a.mapping_only, use_llm=not a.no_llm_group, source_name=a.source_name, target_name=a.target_name, dry_run=a.dry_run, prune=a.prune,
              include_samples=a.include_samples, llm_model=a.llm_model, model_file=a.model_file,
              constraints=not a.no_constraints)
decision = "remap" if a.remap_all else "apply" if a.yes else None
try:
    while True:
        try:
            result = load(a.source, a.target, a.mapping, decision=decision, **kwargs)
            changes = [s for s in result["changes"] if s["kind"] in ("minor", "major")]
            if a.dry_run and changes:
                print(f"Detected {result['change_kind']} changes:")
                for s in changes:
                    print(f"  {s['system']}: {s['changed_ratio']:.0%} changed; +{len(s['added_columns'])} "
                          f"-{len(s['removed_columns'])} ~{len(s['type_changes'])} columns")
            print({k: v for k, v in result.items() if k != "changes"})
            break
        except ChangeConfirmationRequired as e:
            print(e.plan.describe())
            if not sys.stdin.isatty():
                raise SystemExit("Not confirmed (non-interactive). Re-run with --yes to accept these changes "
                                 "or --remap-all to rebuild everything.")
            if e.plan.kind == "major":
                ok = ask("\nThe source has changed almost entirely. Remap EVERYTHING (clear old mappings, drop stale "
                         "nodes, load the new data and mapping)?")
                decision = "remap" if ok else None
            else:
                ok = ask("\nOkay, you got it? Apply these changes?")
                decision = "apply" if ok else None
            if not ok:
                raise SystemExit("Cancelled. Nothing was written.")
except LoadError as e:
    raise SystemExit(f"ERROR: {e}")
