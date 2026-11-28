"""graphload: load any nested JSON/YAML into Neo4j as a lineage graph (deterministic, parameterised, optional LLM naming)."""
from pathlib import Path

from dotenv import load_dotenv

from . import mappings as mapping_parser
from .config import Limits, LoadError, Neo4jConfig
from .diff import ChangePlan, compute_plan
from .model import DEFAULT_MODEL, GraphModel, propose_model
from .from_mapping import build as build_from_mapping, is_mapping_only
from .prepare import prepare
from .reader import read_document
from .schema import analyse
from .writer import write_graph

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

__all__ = ["load", "LoadError", "GraphModel", "ChangeConfirmationRequired", "check_decision"]


class ChangeConfirmationRequired(LoadError):
    """The data differs from what is stored and the caller has not said what to do. Nothing was written."""

    def __init__(self, plan: ChangePlan):
        self.plan = plan
        super().__init__("the source has changed almost entirely; remap everything?" if plan.kind == "major"
                         else "changes detected; confirm to apply them")


def check_decision(plan: ChangePlan, decision, has_mapping: bool) -> None:
    """decision: None | "apply" (accept the listed changes) | "remap" (rebuild everything, incl. all mappings)."""
    if decision not in (None, "apply", "remap"):
        raise LoadError("decision must be 'apply' or 'remap'")
    if plan.kind in ("initial", "none"):
        return
    if decision == "remap":
        if not has_mapping:
            raise LoadError("remapping everything needs a mapping file: the old mappings are cleared")
        return
    if decision == "apply" and plan.kind == "minor":
        return
    raise ChangeConfirmationRequired(plan)


def _doc(x, limits):
    return x if isinstance(x, (dict, list)) else read_document(x, limits)


def load(source=None, target=None, mapping=None, *, mapping_only=None, use_llm=True, source_name="Source", target_name="Target", dry_run=False,
         prune=False, include_samples=False, llm_model=False, model_file=None, constraints=True,
         decision=None) -> dict:
    """Read -> analyse -> (optional LLM names) -> resolve mappings -> write. Nothing is written if any step fails."""
    limits = Limits()
    if mapping_only is not None:  # the mapping file alone describes everything
        source, target, mapping, source_name, target_name = build_from_mapping(
            read_document(mapping_only, limits), use_llm)
    if source_name == target_name:
        raise LoadError("source and target need different system names")
    src = analyse(source_name, "source", prepare(_doc(source, limits)), limits, include_samples)
    tgt = analyse(target_name, "target", prepare(_doc(target, limits)), limits, include_samples)
    maps = mapping_parser.resolve(mapping_parser.parse(_doc(mapping, limits), limits), src, tgt) if mapping else []

    if model_file and Path(model_file).is_file():
        model = GraphModel.load(model_file)            # a reviewed, pinned model wins
    elif llm_model:
        model = propose_model([src, tgt])
        if model_file:
            model.save(model_file)                      # keep it for review and reproducible re-runs
    else:
        model = DEFAULT_MODEL
    cfg = Neo4jConfig.from_env()
    plan = compute_plan([src, tgt], model, cfg)
    if not dry_run:  # a dry run only previews, so it never needs confirmation
        check_decision(plan, decision, bool(mapping))
    result = write_graph([src, tgt], maps, model, cfg, limits, dry_run=dry_run, prune=prune, constraints=constraints,
                         plan=plan, decision=decision)
    return {**result, "change_kind": plan.kind, "changes": plan.to_dict()["systems"]}
