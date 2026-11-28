"""Safe input handling: size cap, safe parsers only, depth cap, and a guard against path traversal."""
import json
from pathlib import Path

import yaml

from .config import Limits, LoadError


def resolve_inside(base: str | Path, name: str) -> Path:
    """Resolve `name` under `base` and refuse anything that escapes it (../, absolute paths, symlinks)."""
    root = Path(base).resolve()
    p = (root / Path(name).name).resolve()  # .name drops any directory part the caller supplied
    if root not in p.parents:
        raise LoadError("file is outside the allowed folder")
    if not p.is_file():
        raise LoadError(f"file not found: {p.name}")
    return p


def check_depth(data, max_depth: int) -> None:
    """Iterative (a recursive check could itself overflow the stack on hostile input)."""
    stack = [(data, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > max_depth:
            raise LoadError(f"input is nested deeper than {max_depth} levels")
        children = node.values() if isinstance(node, dict) else node if isinstance(node, list) else ()
        stack.extend((c, depth + 1) for c in children if isinstance(c, (dict, list)))


def read_document(path: str | Path, limits: Limits):
    """Parse a .json / .yml / .yaml file. Only safe parsers are used (yaml.safe_load, never yaml.load)."""
    p = Path(path)
    if not p.is_file():
        raise LoadError(f"file not found: {p.name}")
    if p.stat().st_size > limits.max_file_bytes:
        raise LoadError(f"{p.name} is larger than {limits.max_file_bytes // (1024 * 1024)} MB")
    try:
        text = p.read_text(encoding="utf-8")
        data = yaml.safe_load(text) if p.suffix.lower() in (".yml", ".yaml") else json.loads(text)
    except (ValueError, yaml.YAMLError, RecursionError):
        raise LoadError(f"{p.name} is not valid {p.suffix.lstrip('.').upper() or 'JSON'}") from None
    check_depth(data, limits.max_depth)
    return data
