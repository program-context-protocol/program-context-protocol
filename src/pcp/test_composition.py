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

Known residual limitation of the oracle-traceability check specifically
(see `meaningful_literal_overlap`): occurrence frequency is scoped to the
RESOLVING TEST's own imported modules, not the whole project. A globally
common enum/status value can still slip through if it happens to appear
only once or twice within the specific module a given test imports from --
real residual noise found dogfooding this against PCP's own suite (2026-08-09,
"complete"/"pending" still occasionally flagged post-fix). Advisory-only, so
left as a stated limitation rather than chased to zero -- same posture as
every other check in this file earning tighter status only after a measured
false-positive rate, not before.
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
    "exists", "isfile", "isdir",
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


_EXISTENCE_CHECK_NAMES = ("exists", "isfile", "isdir")


def _is_existence_check(node: ast.AST) -> bool:
    """`os.path.exists(...)` / `Path(...).exists()` / `os.path.isfile(...)`,
    optionally negated (`not os.path.exists(...)`). A leading file-presence
    guard alongside `assert "X" in src` is the same source-grep pattern this
    module exists to catch (see module docstring's real finding: 97% of a
    real suite was exactly this shape) -- treating it as disqualifying
    grep_shaped just pushed the whole pattern into the unclassified 'other'
    bucket instead, which undercounts grep-shaped tests just as badly as
    the pre-fix 'real_execution' misclassification did."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        node = node.operand
    return isinstance(node, ast.Call) and _call_name(node) in _EXISTENCE_CHECK_NAMES


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
        (
            isinstance(a.test, ast.Compare)
            and any(isinstance(op, (ast.In, ast.NotIn)) for op in a.test.ops)
        )
        or _is_existence_check(a.test)
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


_TRIVIAL_LITERALS = {"", None, True, False, 0, 1, -1, 2}


def assertion_literals(func: ast.FunctionDef) -> set:
    """Every literal constant a test's `==`/`!=` assertions compare against
    -- the value the test claims is "correct". Distinct from
    `extract_grep_targets` (which is `in`/`not in` checks specifically);
    this is the equality-comparison counterpart, feeding the
    oracle-traceability check below."""
    literals = set()
    for a in ast.walk(func):
        if not isinstance(a, ast.Assert) or not isinstance(a.test, ast.Compare):
            continue
        if not any(isinstance(op, (ast.Eq, ast.NotEq)) for op in a.test.ops):
            continue
        for node in (a.test.left, *a.test.comparators):
            if isinstance(node, ast.Constant) and node.value not in _TRIVIAL_LITERALS:
                literals.add(node.value)
    return literals


def source_literal_pool(tree: ast.Module) -> "Counter":
    """Every literal constant appearing anywhere in a SOURCE (non-test)
    module, WITH occurrence counts -- the pool a hardcoded fallback value
    would live in. Deliberately broad (every literal, not just return
    values): a fallback can be assigned to a variable and returned two
    lines later, and this is a conservative overlap check, not a data-flow
    proof.

    Counted, not a plain set, since 2026-08-09: see
    `meaningful_literal_overlap`'s docstring for why frequency is the real
    signal here, not mere presence."""
    from collections import Counter
    literals: Counter = Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value not in _TRIVIAL_LITERALS:
            literals[node.value] += 1
    return literals


def oracle_traceability_risk(func: ast.FunctionDef, source_literals: "Counter") -> set:
    """The literal(s) this test asserts as correct that ALSO appear as a
    hardcoded literal inside the implementation it's importing from.

    Real incident this closes (2026-08-09, Project W/avrt dogfood): a Mach
    thread-scheduling call was stripped down to a hardcoded no-op fallback
    value, and the test asserting the function "worked" simply compared the
    result against that same hardcoded literal -- passing honestly, proving
    nothing, because the implementation could return the literal without
    doing any real work and the test could not tell the difference. A test
    whose expected value is a literal the code itself could fabricate is not
    an independent check; it is the code checking itself.

    Advisory only, same posture as the rest of this module — a real overlap
    is a prompt to look closer, not proof the test is fake. See
    `meaningful_literal_overlap` for the evidence bar."""
    return meaningful_literal_overlap(assertion_literals(func), source_literals)


_MIN_MEANINGFUL_LITERAL_LEN = 6
_MAX_SHARED_OCCURRENCES = 2


def meaningful_literal_overlap(test_literals: set, source_literals: "Counter") -> set:
    """Language-agnostic core of `oracle_traceability_risk` -- factored out
    so other languages' test-composition modules (see test_composition_c.py)
    reuse the exact same evidence bar instead of re-deriving it.

    Real dogfood correction (2026-08-09): the first version of this check,
    run for real against PCP's own 1481-test suite (not just unit tests
    against synthetic fixtures), flagged 114 "risks" -- almost all noise.
    Two real bugs, both caught only by running on a real project, same
    lesson as `feedback_metric_must_be_run_on_real_data_2026_07_30`:

    1. The length filter only applied to strings, not numbers -- `_TRIVIAL_
       LITERALS` only excludes a fixed handful (0, 1, -1, 2), so ANY other
       small int (3, 4, 50, 300...) counted as "meaningful evidence." Small
       numbers recur constantly for unrelated reasons (counts, indices,
       timeouts) and prove nothing. Fixed by applying the same length
       threshold to the number's string form, not just literal strings.

    2. Presence alone isn't evidence. The top false positives were PCP's
       OWN schema vocabulary -- "complete" (74 occurrences across src/),
       "pending" (47), "skipped" (26), "advisory" (15) -- widely-reused,
       legitimate enum/status values that tests are SUPPOSED to reference,
       not fabricated fallbacks. A genuine ad-hoc hardcoded fallback is
       narrowly scoped (defined and used in one place); a real shared
       vocabulary term recurs everywhere. `_MAX_SHARED_OCCURRENCES` bounds
       this: only a literal appearing in the source pool a FEW times (not
       dozens) counts as risk-worthy."""
    risky = set()
    for lit in test_literals:
        if len(str(lit)) < _MIN_MEANINGFUL_LITERAL_LEN:
            continue
        count = source_literals.get(lit, 0)
        if 0 < count <= _MAX_SHARED_OCCURRENCES:
            risky.add(lit)
    return risky


def _resolve_imported_source_literals(tree: ast.Module, project_root: Path) -> "Counter":
    """Best-effort: for every module this test file imports, find that
    module's source file under project_root and pool its literals (with
    occurrence counts -- see `meaningful_literal_overlap`).

    Deliberately simple and fail-open — a project's import layout varies
    too much for a general resolver to be exact, so this only ever
    UNDER-flags (misses a real overlap because the import didn't resolve),
    never over-flags. Zero matches for an import is silently skipped, not
    an error: "we couldn't trace this one" is honest, not a false claim
    that the source is clean."""
    from collections import Counter
    module_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            module_names.add(node.module.split(".")[-1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                module_names.add(alias.name.split(".")[-1])

    literals: Counter = Counter()
    for name in module_names:
        for candidate in project_root.rglob(f"{name}.py"):
            if any(seg in candidate.parts for seg in _SKIP_DIR_SEGMENTS):
                continue
            try:
                literals.update(source_literal_pool(ast.parse(candidate.read_text(errors="replace"))))
            except (SyntaxError, OSError):
                continue
    return literals


def analyze_test_file(path: Path, project_root: Path | None = None) -> dict:
    """Per-file breakdown. Fails open (empty result) on a syntax error --
    a test file that doesn't even parse is a different, louder problem
    (surfaced elsewhere), not silently double-counted here.

    `project_root` is optional -- without it, oracle-traceability checking
    is skipped entirely (reported as 0, not guessed at), since it needs a
    root to resolve imports against."""
    try:
        tree = ast.parse(path.read_text(errors="replace"))
    except (SyntaxError, OSError):
        return {
            "path": str(path), "real_execution": 0, "grep_shaped": 0, "other": 0,
            "grep_shaped_functions": [], "assertion_free": 0, "assertion_free_functions": [],
            "self_mocked": 0, "self_mocked_functions": [],
            "oracle_risk": 0, "oracle_risk_functions": [],
        }

    source_literals = _resolve_imported_source_literals(tree, project_root) if project_root else set()

    counts = {"real_execution": 0, "grep_shaped": 0, "other": 0}
    grep_shaped_functions = []
    assertion_free_functions = []
    self_mocked_functions = []
    oracle_risk_functions = []
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
            if source_literals:
                risk = oracle_traceability_risk(node, source_literals)
                if risk:
                    oracle_risk_functions.append({"test_name": node.name, "shared_literals": sorted(map(str, risk))})
    return {
        "path": str(path), **counts, "grep_shaped_functions": grep_shaped_functions,
        "assertion_free": len(assertion_free_functions),
        "assertion_free_functions": assertion_free_functions,
        "self_mocked": len(self_mocked_functions),
        "self_mocked_functions": self_mocked_functions,
        "oracle_risk": len(oracle_risk_functions),
        "oracle_risk_functions": oracle_risk_functions,
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

    per_file = [analyze_test_file(p, project_root) for p in sorted(set(files))]
    total_real = sum(f["real_execution"] for f in per_file)
    total_grep = sum(f["grep_shaped"] for f in per_file)
    total_other = sum(f["other"] for f in per_file)
    total = total_real + total_grep + total_other
    total_assertion_free = sum(f["assertion_free"] for f in per_file)
    total_self_mocked = sum(f["self_mocked"] for f in per_file)
    total_oracle_risk = sum(f["oracle_risk"] for f in per_file)

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
        "oracle_risk": total_oracle_risk,
        "oracle_risk_ratio": round(total_oracle_risk / total, 4) if total else 0.0,
        "files": per_file,
        "scope_note": "Python only (stdlib ast) -- other languages' test files are not analyzed.",
    }
