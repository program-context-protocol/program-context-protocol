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


_PYTEST_ASSERTION_CONTEXT_MANAGERS = ("raises", "warns", "deprecated_call")


def _is_pytest_assertion_context(node: ast.AST) -> bool:
    """`with pytest.raises(...):` / `pytest.warns(...)` / `pytest.deprecated_call()`
    -- these ARE the assertion (a block that doesn't raise/warn fails the
    `with`), just spelled as a context manager instead of `assert`."""
    items = getattr(node, "items", None)
    if items is None:
        return False
    for item in items:
        call = item.context_expr
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr in _PYTEST_ASSERTION_CONTEXT_MANAGERS
        ):
            return True
    return False


def has_any_assertion(func: ast.FunctionDef) -> bool:
    """Facet 3 of the testing prior-art sweep (2026-08-08) -- a test with
    ZERO assertions of any kind (no `assert`, no `pytest.raises`/`warns`,
    no `self.assertX`) proves nothing at all, a distinct and arguably worse
    problem than grep-shaped (which at least checks something, however
    weak). classify_test_function's 'other' bucket already conflated this
    with 'has an assert but no recognized signal' -- this is the narrower,
    honest check for the empty case specifically.

    Prior-art check (2026-08-08): PyNose (JetBrains Research) is a PyCharm
    IDE plugin, not a standalone CLI -- not embeddable in this codebase's
    detect-and-shell-out pattern. pytest-smell (PyPI, MIT, a dissertation
    research tool) was spiked directly: on a fixture with one genuinely
    zero-assertion test and one real-assertion test, it missed the
    zero-assertion test entirely and flagged the real-assertion test with a
    nonsensical 'Assertion Roullete' (multiple-asserts smell) on a function
    with exactly one assert -- unreliable, disqualified. Decision:
    build-fresh. This is a rung-1 AST presence check (no assert anywhere =
    True/False), the same tier and same file this session's own
    test_composition.py check already occupies -- not a new dependency,
    not a new module."""
    for n in ast.walk(func):
        if isinstance(n, ast.Assert):
            return True
        if isinstance(n, (ast.With, ast.AsyncWith)) and _is_pytest_assertion_context(n):
            return True
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr.startswith("assert"):
            return True  # self.assertEqual(...) (unittest) or mock_x.assert_called_once() (Mock/MagicMock)
    return False


def _patch_call_target_name(call: ast.Call) -> str | None:
    """`patch("module.path.compute_score")` / `mock.patch(...)` /
    `patch.object(thing, "compute_score")` -> "compute_score" (the last
    dotted segment, or the literal name for patch.object's second arg).
    None if this isn't a patch call at all."""
    func = call.func
    is_patch = (
        (isinstance(func, ast.Name) and func.id == "patch")
        or (isinstance(func, ast.Attribute) and func.attr == "patch")
    )
    is_patch_object = isinstance(func, ast.Attribute) and func.attr == "object" and (
        (isinstance(func.value, ast.Name) and func.value.id == "patch")
        or (isinstance(func.value, ast.Attribute) and func.value.attr == "patch")
    )
    if is_patch_object and len(call.args) >= 2:
        arg = call.args[1]
        return arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else None
    if is_patch and call.args:
        arg = call.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value.rsplit(".", 1)[-1]
    return None


def _patched_target_names(func: ast.FunctionDef) -> set[str]:
    """Every symbol name this test patches -- via `with patch(...) as x:`
    or the `@patch(...)` decorator -- collected regardless of what the
    bound variable is called, since the fake pattern below cares about
    what got REPLACED, not what it was renamed to locally."""
    names = set()
    for n in ast.walk(func):
        if isinstance(n, (ast.With, ast.AsyncWith)):
            for item in n.items:
                if isinstance(item.context_expr, ast.Call):
                    target = _patch_call_target_name(item.context_expr)
                    if target:
                        names.add(target)
    for deco in getattr(func, "decorator_list", []):
        if isinstance(deco, ast.Call):
            target = _patch_call_target_name(deco)
            if target:
                names.add(target)
    return names


def calls_only_its_own_patched_target(func: ast.FunctionDef) -> bool:
    """The mock-hides-the-fake pattern: a test patches symbol X, then its
    only 'real execution' signal is calling X itself -- which, patched,
    just returns whatever the test configured. It isn't testing X; it's
    testing that Mock returns what it was told to return. Distinct from
    (and must NOT flag) the legitimate case of patching a DEPENDENCY while
    calling a different, real orchestrating function -- this only fires
    when every non-safe call in the function resolves to a name the same
    function itself patched.

    Real gap found 2026-08-08 auditing PCP's own facet-3 work: `classify_test_function`
    marks a test 'real_execution' the moment it calls anything off the
    safe-list, including a call to a name the test just patched -- a test
    can be 100% fake (asserting a Mock returns its own configured value)
    and still read as the strongest possible verdict. Rung-1, deterministic,
    same file/tier as everything else here -- no prior-art search needed,
    this is a structural AST correlation (does call-target intersect
    patch-target), not a new detection category."""
    patched = _patched_target_names(func)
    if not patched:
        return False
    real_calls = set()
    for c in ast.walk(func):
        if not isinstance(c, ast.Call):
            continue
        if _is_subprocess_call(c):
            return False  # a genuine subprocess call is real regardless of what else is patched
        name = _call_name(c)
        if (
            name is not None and name not in _SAFE_CALL_NAMES
            and name not in ("patch", "object") and not name.startswith("assert")
        ):
            real_calls.add(name)
    if not real_calls:
        return False
    return real_calls.issubset(patched)


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
        return {
            "path": str(path), "real_execution": 0, "grep_shaped": 0, "other": 0,
            "grep_shaped_functions": [], "assertion_free": 0, "assertion_free_functions": [],
            "self_mocked": 0, "self_mocked_functions": [],
        }

    counts = {"real_execution": 0, "grep_shaped": 0, "other": 0}
    grep_shaped_functions = []
    assertion_free_functions = []
    self_mocked_functions = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            verdict = classify_test_function(node)
            counts[verdict] += 1
            if verdict == "grep_shaped":
                grep_shaped_functions.append({
                    "test_name": node.name,
                    "targets": extract_grep_targets(node),
                })
            if not has_any_assertion(node):
                assertion_free_functions.append(node.name)
            if calls_only_its_own_patched_target(node):
                self_mocked_functions.append(node.name)
    return {
        "path": str(path), **counts, "grep_shaped_functions": grep_shaped_functions,
        "assertion_free": len(assertion_free_functions),
        "assertion_free_functions": assertion_free_functions,
        "self_mocked": len(self_mocked_functions),
        "self_mocked_functions": self_mocked_functions,
    }


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
    total_assertion_free = sum(f["assertion_free"] for f in per_file)
    total_self_mocked = sum(f["self_mocked"] for f in per_file)

    return {
        "total_test_functions": total,
        "real_execution": total_real,
        "grep_shaped": total_grep,
        "other": total_other,
        "grep_shaped_ratio": round(total_grep / total, 4) if total else 0.0,
        "assertion_free": total_assertion_free,
        "assertion_free_ratio": round(total_assertion_free / total, 4) if total else 0.0,
        "self_mocked": total_self_mocked,
        "self_mocked_ratio": round(total_self_mocked / total, 4) if total else 0.0,
        "files": per_file,
        "scope_note": "Python only (stdlib ast) -- other languages' test files are not analyzed.",
    }
