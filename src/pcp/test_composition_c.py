"""Test-suite composition audit for C -- the language-coverage gap
`test_composition.py` names honestly in its own scope_note ("Python only").

Real finding this closes (2026-08-09, Project W/Project I native-bridge
dogfood): every
fake-pass pattern found auditing 15 C DLL adapters that session -- a Mach
thread-scheduling call stripped to a hardcoded fallback literal that its own
test then echoed back as the "expected" value -- was written in C, and
none of PCP's existing Python-`ast`-only tooling could see any of it.

Scope, stated honestly rather than silently (same discipline as
test_composition.py's own scope_note): covers the `test_*`/`*_test`
function-naming convention only. Macro-based frameworks that register
tests via a macro call rather than a plain function definition (Unity's
`RUN_TEST`, CUnit's `CU_add_test`, Google Test's `TEST(Suite, Case)`) are
NOT expanded by a syntax-only parser and are invisible here -- not
analyzed, not counted, reported in `scope_note` rather than implied covered.

Requires the optional `tree-sitter`/`tree-sitter-c` extra
(`pip install program-context-protocol[c]`) -- gracefully reports
`available: False` when absent, same posture as every other optional-tool
integration in this codebase (ruff/semgrep/opa in doctor.py).

Deterministic, rung 1: pure syntax-tree inspection, zero LLM cost. Advisory
only, report-first -- surfaces the ratio, never blocks or rewrites a test.
"""

from pathlib import Path

from pcp.test_composition import meaningful_literal_overlap

try:
    import tree_sitter
    import tree_sitter_c

    _LANGUAGE = tree_sitter.Language(tree_sitter_c.language())
    _PARSER = tree_sitter.Parser(_LANGUAGE)
    TREE_SITTER_AVAILABLE = True
except ImportError:
    _LANGUAGE = None
    _PARSER = None
    TREE_SITTER_AVAILABLE = False


_TRIVIAL_NUMBERS = {"-1", "0", "1", "2"}
_EQUALITY_OPS = ("==", "!=")


def _text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _function_name(func_node, source: bytes) -> str | None:
    """The declared name of a function_definition -- walks through any
    pointer_declarator wrapping (`char *test_foo(void)`) to the identifier,
    same "don't assume simple shape" discipline as the rest of this
    codebase's AST walkers."""
    for n in _walk(func_node):
        if n.type == "function_declarator":
            for c in n.children:
                if c.type == "identifier":
                    return _text(c, source)
    return None


def has_any_assertion_c(func_node, source: bytes) -> bool:
    """Any call whose function name contains "assert" (case-insensitive) --
    covers assert.h's `assert()`, and common C test-framework macros/calls
    (`CU_ASSERT`, `TEST_ASSERT_EQUAL`, `ck_assert`, `assert_true`) without
    hardcoding a framework allowlist. Same permissive-by-design choice as
    `has_any_assertion`'s self.assertX/mock.assert_x handling in the Python
    module -- broad recall over narrow precision, since the failure mode
    this exists to catch (zero real assertions) is worse than an occasional
    over-count."""
    for n in _walk(func_node):
        if n.type == "call_expression":
            for c in n.children:
                if c.type == "identifier" and "assert" in _text(c, source).lower():
                    return True
    return False


def assertion_literals_c(func_node, source: bytes) -> set:
    """The literal(s) a C test's equality checks claim are correct.
    Handles two shapes: a direct `x == "lit"` / `x == 42` binary_expression,
    and the idiomatic C string-equality pattern `strcmp(x, "lit") == 0`
    (C has no `==` for strings, so this IS the equality check, not a
    different thing) -- both count as the test's claimed expected value."""
    literals: set = set()
    for n in _walk(func_node):
        if n.type != "binary_expression":
            continue
        children = list(n.children)
        op_idx = next((i for i, c in enumerate(children) if c.type in _EQUALITY_OPS), None)
        if op_idx is None:
            continue
        left, right = children[op_idx - 1], children[op_idx + 1]
        for side in (left, right):
            lit = _literal_value(side, source)
            if lit is not None:
                literals.add(lit)
            if side.type == "call_expression":
                literals |= _strcmp_string_arg(side, source)
    return literals


def _literal_value(node, source: bytes):
    if node.type == "string_literal":
        for c in node.children:
            if c.type == "string_content":
                return _text(c, source)
        return None
    if node.type == "number_literal":
        text = _text(node, source)
        return None if text in _TRIVIAL_NUMBERS else text
    return None


def _strcmp_string_arg(call_node, source: bytes) -> set:
    """`strcmp(a, "lit")`/`strncmp(a, "lit", n)` -- pull the string literal
    argument regardless of which side it's on, since the test may write
    either `strcmp(actual, "expected")` or `strcmp("expected", actual)`."""
    fn = next((c for c in call_node.children if c.type == "identifier"), None)
    if fn is None or _text(fn, source) not in ("strcmp", "strncmp"):
        return set()
    args_node = next((c for c in call_node.children if c.type == "argument_list"), None)
    if args_node is None:
        return set()
    out = set()
    for arg in args_node.children:
        val = _literal_value(arg, source)
        if val is not None:
            out.add(val)
    return out


def source_literal_pool_c(tree_root, source: bytes):
    """Every literal appearing anywhere in a SOURCE (non-test) file, WITH
    occurrence counts -- the pool a hardcoded fallback value would live in.
    Same breadth-over-precision choice as `source_literal_pool`'s Python
    counterpart: every literal, not just return values, since a fallback
    can be assigned to a variable well before it's returned. Length
    filtering happens in `meaningful_literal_overlap`, not here (same fix
    as the Python module's 2026-08-09 dogfood correction -- filtering here
    used to only apply to strings, letting any short number through)."""
    from collections import Counter
    literals: Counter = Counter()
    for n in _walk(tree_root):
        val = _literal_value(n, source)
        if val is not None:
            literals[val] += 1
    return literals


def oracle_traceability_risk_c(func_node, source: bytes, source_literals: set) -> set:
    """Same real incident this closes as the Python version
    (`oracle_traceability_risk`'s docstring) -- a test asserting against a
    literal that ALSO appears hardcoded in the implementation it calls
    proves nothing, because the implementation could return the literal
    without doing any real work. Reuses the exact same evidence-bar filter
    (`meaningful_literal_overlap`) as the Python check, not a re-derived
    threshold."""
    return meaningful_literal_overlap(assertion_literals_c(func_node, source), source_literals)


_SKIP_DIR_SEGMENTS = ("__pycache__", ".venv", "venv", "node_modules", ".pcp", ".git", "build")


def _find_project_source_files(project_root: Path) -> list[Path]:
    files = []
    for pattern in ("*.c",):
        for p in project_root.rglob(pattern):
            if any(seg in p.parts for seg in _SKIP_DIR_SEGMENTS):
                continue
            name = p.name
            if name.startswith("test_") or name.endswith("_test.c"):
                continue
            files.append(p)
    return files


def analyze_c_test_file(path: Path, project_root: Path | None = None) -> dict:
    """Per-file breakdown, mirroring `analyze_test_file`'s shape and
    fail-open discipline. `project_root` is optional -- without it, the
    oracle-traceability pool is built from every non-test `.c` file in the
    project (a coarser net than Python's import-based resolution, since C
    has no import graph a syntax-only parser can follow reliably; still
    correct-by-construction never-over-flag behavior, just less targeted)."""
    if not TREE_SITTER_AVAILABLE:
        return {"path": str(path), "available": False}

    try:
        source = path.read_bytes()
        tree = _PARSER.parse(source)
    except OSError:
        return {
            "path": str(path), "available": True, "real_execution": 0, "assertion_free": 0,
            "assertion_free_functions": [], "oracle_risk": 0, "oracle_risk_functions": [],
        }

    from collections import Counter
    source_literals: Counter = Counter()
    if project_root is not None:
        for src_path in _find_project_source_files(Path(project_root)):
            try:
                src_bytes = src_path.read_bytes()
                source_literals.update(source_literal_pool_c(_PARSER.parse(src_bytes).root_node, src_bytes))
            except OSError:
                continue

    assertion_free_functions = []
    oracle_risk_functions = []
    real_execution = 0
    for n in _walk(tree.root_node):
        if n.type != "function_definition":
            continue
        name = _function_name(n, source)
        if not name or not name.lower().startswith("test"):
            continue
        real_execution += 1
        if not has_any_assertion_c(n, source):
            assertion_free_functions.append(name)
        if source_literals:
            risk = oracle_traceability_risk_c(n, source, source_literals)
            if risk:
                oracle_risk_functions.append({"test_name": name, "shared_literals": sorted(map(str, risk))})

    return {
        "path": str(path), "available": True,
        "real_execution": real_execution,
        "assertion_free": len(assertion_free_functions),
        "assertion_free_functions": assertion_free_functions,
        "oracle_risk": len(oracle_risk_functions),
        "oracle_risk_functions": oracle_risk_functions,
    }


def analyze_c_test_composition(project_root: Path) -> dict:
    """Scans every test_*.c / *_test.c file under project_root. Same
    report-only posture as `analyze_test_composition` -- consumers decide
    what to do with the numbers, this only measures them."""
    if not TREE_SITTER_AVAILABLE:
        return {
            "available": False,
            "scope_note": "tree-sitter/tree-sitter-c not installed -- "
                           "`pip install program-context-protocol[c]` to enable C coverage.",
        }

    project_root = Path(project_root)
    files = []
    for pattern in ("test_*.c", "*_test.c"):
        for p in project_root.rglob(pattern):
            if any(seg in p.parts for seg in _SKIP_DIR_SEGMENTS):
                continue
            files.append(p)

    per_file = [analyze_c_test_file(p, project_root) for p in sorted(set(files))]
    total = sum(f["real_execution"] for f in per_file)
    total_assertion_free = sum(f["assertion_free"] for f in per_file)
    total_oracle_risk = sum(f["oracle_risk"] for f in per_file)

    return {
        "available": True,
        "total_test_functions": total,
        "assertion_free": total_assertion_free,
        "assertion_free_ratio": round(total_assertion_free / total, 4) if total else 0.0,
        "oracle_risk": total_oracle_risk,
        "oracle_risk_ratio": round(total_oracle_risk / total, 4) if total else 0.0,
        "files": per_file,
        "scope_note": (
            "C, function-naming convention only (test_*/*_test) -- macro-registered tests "
            "(Unity RUN_TEST, CUnit CU_add_test, Google Test TEST(Suite,Case)) are not expanded "
            "by a syntax-only parser and are invisible here. No grep-shaped or self-mocked "
            "equivalent checks yet (lower-value in C's idiom of calling compiled functions "
            "directly rather than reading source text, and C has no dynamic-patching analogue "
            "to Python's mock.patch) -- assertion-free and oracle-traceability only."
        ),
    }
