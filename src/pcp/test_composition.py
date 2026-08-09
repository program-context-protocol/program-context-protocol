"""Test-suite composition audit: how many tests actually EXECUTE the code
under test, vs. how many only check that a name/string appears somewhere in
a file (source-text grep) -- both are "tests that pass," but they answer
different questions, and reporting both as bare "tests pass" or "verified"
erases that difference.

Real finding this closes (2026-08-08): a real project's test suite -- 255
tests, 6710 lines, 100% passing -- was 97% source-text grep
(`assert "FunctionName" in file_contents`), 3% actual execution against a
compiled binary. Every one of those 247 tests was a genuine, honest,
passing test; none of them proved the thing it named actually worked. Tier
2 (source-grep passes) and tier 4 (real-execution tested) are both real,
both worth having, and must never be reported under the same bare word.

Deterministic, rung 1: pure AST inspection, zero LLM cost, zero agent
sessions. Same posture as ast_grep_swallowed_exceptions/lazy_marker in this
codebase -- a structural pattern check, not a judgment call. Advisory only,
report-first: this SURFACES the ratio, it never blocks or rewrites a test.

Scope, stated honestly rather than silently: Python only (stdlib `ast`).
A project's Swift/C#/other-language tests are invisible to this pass --
not analyzed, not counted in the ratio, and the report says so rather than
implying full coverage.
"""

import ast
from pathlib import Path

_FILE_READ_METHOD_NAMES = ("read_text", "read", "readlines")

# Calls that don't indicate "this test invoked the thing under test" --
# builtins, string/collection methods, and the file-read methods
# themselves. Kept small and explicit rather than trying to enumerate every
# stdlib method: the classifier is conservative in the "real_execution"
# direction (a call to an unrecognized name is treated as real execution,
# not the reverse), so an incomplete safe-list only ever makes the check
# UNDER-flag grep-shaped tests, never over-flag a real one.
_SAFE_CALL_NAMES = {
    "open", "Path", "str", "len", "set", "list", "dict", "tuple", "sorted", "print",
    "isinstance", "read", "read_text", "readlines", "strip", "lstrip",
    "rstrip", "split", "splitlines", "join", "format", "get", "items",
    "keys", "values", "append", "extend", "sort", "lower", "upper",
    "startswith", "endswith", "replace", "encode", "decode",
}


def _is_file_read_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _FILE_READ_METHOD_NAMES
    )


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def _is_subprocess_call(call: ast.Call) -> bool:
    return (
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "subprocess"
    )


def classify_test_function(func: ast.FunctionDef) -> str:
    """'real_execution' | 'grep_shaped' | 'other'.

    'other' is the honest middle ground -- no asserts at all, or asserts
    that are neither a recognized real-execution call NOR a pure file-read
    'in' check (e.g. `assert 2 + 2 == 4`, a hardcoded-literal comparison).
    Not conflated with either real bucket: "we could not classify this" and
    "this is grep-shaped" are different claims, same discipline as
    design_audit.py's `undetermined` bucket for unmeasurable UI criteria."""
    asserts = [n for n in ast.walk(func) if isinstance(n, ast.Assert)]
    if not asserts:
        return "other"

    calls = [n for n in ast.walk(func) if isinstance(n, ast.Call)]
    for c in calls:
        if _is_subprocess_call(c):
            return "real_execution"
        name = _call_name(c)
        if name is not None and name not in _SAFE_CALL_NAMES:
            return "real_execution"

    all_in_checks = all(
        isinstance(a.test, ast.Compare)
        and any(isinstance(op, (ast.In, ast.NotIn)) for op in a.test.ops)
        for a in asserts
    )
    has_file_read = any(_is_file_read_call(n) for n in ast.walk(func))
    if all_in_checks and has_file_read:
        return "grep_shaped"
    return "other"


def extract_grep_targets(func: ast.FunctionDef) -> list[str]:
    """The string literal(s) a grep-shaped test's 'in' checks are actually
    looking for -- e.g. `assert "compute_score" in content` -> "compute_score".
    Only meaningful when classify_test_function already returned
    'grep_shaped'; called separately (not fused into classification) so a
    caller can classify cheaply first and only extract targets for the
    functions worth it.

    This is the identity that turns a cheap static flag into a targeted
    empirical check (see mutation_confirm.py): resolve each target string
    to whatever function/class it names, mutate ONLY that definition, and
    confirm the flagged test really does score near-zero against it --
    instead of mutation-testing a whole module speculatively."""
    targets = []
    for a in ast.walk(func):
        if not isinstance(a, ast.Assert):
            continue
        if not isinstance(a.test, ast.Compare):
            continue
        if not any(isinstance(op, (ast.In, ast.NotIn)) for op in a.test.ops):
            continue
        left = a.test.left
        if isinstance(left, ast.Constant) and isinstance(left.value, str):
            targets.append(left.value)
    return targets


def analyze_test_file(path: Path) -> dict:
    """Per-file breakdown. Fails open (empty result) on a syntax error --
    a test file that doesn't even parse is a different, louder problem
    (surfaced elsewhere), not silently double-counted here."""
    try:
        tree = ast.parse(path.read_text(errors="replace"))
    except (SyntaxError, OSError):
        return {"path": str(path), "real_execution": 0, "grep_shaped": 0, "other": 0, "grep_shaped_functions": []}

    counts = {"real_execution": 0, "grep_shaped": 0, "other": 0}
    grep_shaped_functions = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            verdict = classify_test_function(node)
            counts[verdict] += 1
            if verdict == "grep_shaped":
                grep_shaped_functions.append({
                    "test_name": node.name,
                    "targets": extract_grep_targets(node),
                })
    return {"path": str(path), **counts, "grep_shaped_functions": grep_shaped_functions}


_SKIP_DIR_SEGMENTS = ("__pycache__", ".venv", "venv", "node_modules", ".pcp", ".git")


def analyze_test_composition(project_root: Path) -> dict:
    """Scans every test_*.py / *_test.py file under project_root, returns
    per-file breakdowns plus an overall ratio.

    Real numeric report, not a pass/fail gate -- consumers (pcp audit)
    decide what to do with the ratio; this function only measures it."""
    project_root = Path(project_root)
    files = []
    for pattern in ("test_*.py", "*_test.py"):
        for p in project_root.rglob(pattern):
            if any(seg in p.parts for seg in _SKIP_DIR_SEGMENTS):
                continue
            files.append(p)

    per_file = [analyze_test_file(p) for p in sorted(set(files))]
    total_real = sum(f["real_execution"] for f in per_file)
    total_grep = sum(f["grep_shaped"] for f in per_file)
    total_other = sum(f["other"] for f in per_file)
    total = total_real + total_grep + total_other

    return {
        "total_test_functions": total,
        "real_execution": total_real,
        "grep_shaped": total_grep,
        "other": total_other,
        "grep_shaped_ratio": round(total_grep / total, 4) if total else 0.0,
        "files": per_file,
        "scope_note": "Python only (stdlib ast) -- other languages' test files are not analyzed.",
    }
