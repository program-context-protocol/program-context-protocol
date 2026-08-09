"""Targeted mutation-testing confirmation for grep-shaped tests flagged by
test_composition.py.

Deliberately NOT a whole-module or whole-project mutation sweep -- that was
considered and rejected as low-signal-per-cost: mutating everything and
seeing what survives is a fishing expedition. The actual valuable target is
narrow: the SPECIFIC function(s) a grep-shaped test claims to check
(`assert "compute_score" in content` -> "compute_score"), confirmed
empirically instead of left as a static pattern-match guess.

Two-stage funnel:
  1. test_composition.py (cheap, static, always-on) flags candidates.
  2. This module (expensive, opt-in) mutation-tests ONLY the flagged
     function, using the test file that claimed to cover it, and reports
     whether the mutation score empirically confirms the static suspicion.

cosmic-ray is detected via shutil.which and shelled out to, same
detect-and-invoke pattern as pytest/ruff/semgrep/vulture/jscpd elsewhere in
this codebase -- never vendored, skipped gracefully if absent. MIT
license, zero copyleft concern regardless.
"""

import ast
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

_SKIP_DIR_SEGMENTS = ("__pycache__", ".venv", "venv", "node_modules", ".pcp", ".git")


def cosmic_ray_available() -> bool:
    return shutil.which("cosmic-ray") is not None


def resolve_definition_file(project_root: Path, name: str) -> Path | None:
    """Which source file defines a function/class named `name`? Scans the
    project's own source (excluding test files and the standard noise
    dirs) for a top-level or nested def/class with this exact name.
    Returns the FIRST match -- if the name is ambiguous (defined in more
    than one file), that ambiguity itself means a single-file mutation
    scope can't be trusted, so the caller should treat a None-adjacent
    "found but ambiguous" case with the same caution as not-found (see
    resolve_definition_file_all for the full match list)."""
    matches = resolve_definition_file_all(project_root, name)
    return matches[0] if matches else None


def resolve_definition_file_all(project_root: Path, name: str) -> list[Path]:
    project_root = Path(project_root)
    matches = []
    for p in project_root.rglob("*.py"):
        if any(seg in p.parts for seg in _SKIP_DIR_SEGMENTS):
            continue
        if p.name.startswith("test_") or p.name.endswith("_test.py"):
            continue  # looking for the DEFINITION, not another test file
        try:
            tree = ast.parse(p.read_text(errors="replace"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
                matches.append(p)
                break
    return matches


def run_targeted_mutation_test(
    project_root: Path, target_name: str, definition_file: Path, test_file: Path,
    timeout_sec: float = 30.0,
) -> dict:
    """Runs cosmic-ray scoped to ONE file, tested by ONE test file, then
    filters the dump output down to mutations of exactly `target_name`
    (cosmic-ray's own dump JSON tags each mutation with `definition_name`,
    so a file with other functions in it doesn't pollute the score).

    Returns {"available": False} if cosmic-ray isn't installed -- never
    raises, never blocks; this is an opt-in advisory confirmation, not a
    gate. {"available": True, "killed": N, "survived": N, "mutation_score":
    float|None, "confirms_grep_shaped": bool|None} on success.
    "confirms_grep_shaped" is True when the score is exactly 0.0 (nothing
    caught), matching what a purely grep-shaped test structurally cannot
    catch -- None if there were zero mutations to judge (e.g. the target
    name doesn't map to anything cosmic-ray's operators can mutate, like a
    class with no mutable expressions)."""
    if not cosmic_ray_available():
        return {"available": False}

    project_root = Path(project_root)
    definition_file = Path(definition_file)
    test_file = Path(test_file)
    rel_module = definition_file.relative_to(project_root)
    rel_test = test_file.relative_to(project_root)

    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "config.toml"
        session_path = Path(tmp) / "session.sqlite"
        config_path.write_text(
            f'[cosmic-ray]\n'
            f'module-path = "{rel_module.as_posix()}"\n'
            f'timeout = {timeout_sec}\n'
            f'excluded-modules = []\n'
            f'test-command = "pytest {rel_test.as_posix()}"\n'
            f'\n[cosmic-ray.distributor]\nname = "local"\n'
        )

        def _run(*args):
            return subprocess.run(
                ["cosmic-ray", *args], cwd=project_root, capture_output=True, text=True,
                timeout=timeout_sec * 20 + 60,  # generous ceiling for init+baseline+exec combined
            )

        init_result = _run("init", str(config_path), str(session_path))
        if init_result.returncode != 0:
            return {"available": True, "error": f"init failed: {init_result.stderr[-500:]}"}

        baseline_result = _run("baseline", str(config_path))
        if baseline_result.returncode != 0:
            # The UNMUTATED code doesn't even pass its own tests -- a real,
            # different finding than "test is weak", and mutation results
            # on top of a failing baseline would be meaningless noise.
            return {"available": True, "error": f"baseline failed (test_file doesn't pass against unmutated code): {baseline_result.stderr[-500:]}"}

        exec_result = _run("exec", str(config_path), str(session_path))
        if exec_result.returncode != 0:
            return {"available": True, "error": f"exec failed: {exec_result.stderr[-500:]}"}

        dump_result = _run("dump", str(session_path))
        if dump_result.returncode != 0:
            return {"available": True, "error": f"dump failed: {dump_result.stderr[-500:]}"}

        killed = survived = other = 0
        for line in dump_result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                mutation_info, work_result = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            mutations = mutation_info.get("mutations") or []
            if not any(m.get("definition_name") == target_name for m in mutations):
                continue
            outcome = work_result.get("test_outcome")
            if outcome == "killed":
                killed += 1
            elif outcome == "survived":
                survived += 1
            else:
                other += 1

        total = killed + survived
        return {
            "available": True,
            "target_name": target_name,
            "definition_file": str(rel_module),
            "test_file": str(rel_test),
            "killed": killed,
            "survived": survived,
            "other_outcomes": other,
            "mutation_score": round(killed / total, 4) if total else None,
            "confirms_grep_shaped": (killed == 0 and total > 0) if total else None,
        }
