"""Per-symbol AST fingerprints (Swimm token-tracking / Fiberplane drift.lock
reference patterns, 2026-07-17, build plan 3.4).

File-level change detection can't distinguish "this file was touched" from
"the symbol a criterion actually depends on changed". Fingerprinting each
top-level function/class by a normalized AST hash (structure + names, not
whitespace/comments/positions) lets `pcp scan` report symbol-level churn —
the noise-reduction the drift-tool field converged on.

Python-only today (ast stdlib); other languages report no symbols rather than
guessing. Deterministic, zero LLM.
"""

import ast
import hashlib
import json
from pathlib import Path

FINGERPRINTS_FILE = "symbol_fingerprints.json"


def fingerprint_python_file(path: Path) -> dict[str, str]:
    import warnings
    try:
        # Scanned projects' own SyntaxWarnings (invalid escape sequences etc.)
        # are their lint problem, not scan output — suppress here or every
        # `pcp scan` re-emits them for files PCP merely fingerprints.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(path.read_text(errors="replace"))
    except (SyntaxError, OSError):
        return {}
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # ast.dump without attributes normalizes away line/col/whitespace;
            # docstrings remain part of the structure deliberately — a
            # contract-describing docstring change IS a symbol change.
            digest = hashlib.sha256(ast.dump(node, annotate_fields=False).encode()).hexdigest()[:16]
            out[node.name] = digest
    return out


def fingerprint_project(project_root: Path) -> dict[str, dict[str, str]]:
    result = {}
    for p in sorted(project_root.rglob("*.py")):
        if any(seg in p.parts for seg in ("__pycache__", ".venv", "venv", "node_modules", ".git", ".pcp")):
            continue
        fp = fingerprint_python_file(p)
        if fp:
            result[str(p.relative_to(project_root))] = fp
    return result


def diff_fingerprints(old: dict, new: dict) -> dict:
    """{changed: [file:symbol], added: [...], removed: [...]}."""
    changed, added, removed = [], [], []
    for f, symbols in new.items():
        old_symbols = old.get(f, {})
        for name, h in symbols.items():
            if name not in old_symbols:
                added.append(f"{f}:{name}")
            elif old_symbols[name] != h:
                changed.append(f"{f}:{name}")
        for name in old_symbols:
            if name not in symbols:
                removed.append(f"{f}:{name}")
    for f in old:
        if f not in new:
            removed.extend(f"{f}:{name}" for name in old[f])
    return {"changed": changed, "added": added, "removed": removed}


def update_fingerprints(pcp_dir: Path) -> dict:
    """Recompute, diff vs stored, persist, return the diff summary."""
    path = pcp_dir / FINGERPRINTS_FILE
    old = {}
    if path.exists():
        try:
            old = json.loads(path.read_text())
        except json.JSONDecodeError:
            old = {}
    new = fingerprint_project(pcp_dir.parent)
    delta = diff_fingerprints(old, new)
    path.write_text(json.dumps(new, indent=1, sort_keys=True))
    return delta
