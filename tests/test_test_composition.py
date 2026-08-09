"""Deterministic test-suite composition audit (2026-08-08) -- real finding:
255 tests, 100% passing, 97% were `assert "Name" in file_contents` (source-
text grep), only 3% actually executed a compiled binary. Tier 2 (grep
passes) and tier 4 (real-execution tested) are both real and both worth
distinguishing -- this module makes that distinction computable."""

import ast
import textwrap

from pcp.test_composition import (
    classify_test_function, analyze_test_file, analyze_test_composition,
)


def _func(src: str) -> ast.FunctionDef:
    tree = ast.parse(textwrap.dedent(src))
    return tree.body[0]


def test_grep_shaped_classified_correctly():
    func = _func("""
        def test_function_exists():
            content = open("src/thing.py").read()
            assert "def compute_score" in content
    """)
    assert classify_test_function(func) == "grep_shaped"


def test_grep_shaped_with_read_text_variant():
    func = _func("""
        def test_function_exists():
            from pathlib import Path
            content = Path("src/thing.py").read_text()
            assert "compute_score" in content
    """)
    assert classify_test_function(func) == "grep_shaped"


def test_real_execution_when_project_function_is_called():
    func = _func("""
        def test_compute_score_is_correct():
            from thing import compute_score
            assert compute_score(10, 20) == 30
    """)
    assert classify_test_function(func) == "real_execution"


def test_real_execution_via_subprocess():
    func = _func("""
        def test_binary_runs():
            import subprocess
            result = subprocess.run(["./mybin", "--flag"], capture_output=True)
            assert result.returncode == 0
    """)
    assert classify_test_function(func) == "real_execution"


def test_in_check_without_file_read_is_other_not_grep_shaped():
    """A plain 'in' check against a hardcoded literal isn't grep-shaped --
    there's no file-read signal, so this is honestly unclassifiable, not
    assumed to be the file-content-grep pattern."""
    func = _func("""
        def test_membership():
            assert "a" in ["a", "b", "c"]
    """)
    assert classify_test_function(func) == "other"


def test_no_asserts_is_other():
    func = _func("""
        def test_setup_only():
            x = 1
    """)
    assert classify_test_function(func) == "other"


def test_hardcoded_equality_is_other():
    func = _func("""
        def test_arithmetic():
            assert 2 + 2 == 4
    """)
    assert classify_test_function(func) == "other"


def test_mixed_grep_and_real_call_is_real_execution():
    """Any real-execution signal anywhere in the function wins -- a test
    that both greps AND calls the real thing is still meaningfully testing
    behavior, not purely cosmetic."""
    func = _func("""
        def test_mixed():
            from thing import compute_score
            content = open("src/thing.py").read()
            assert "def compute_score" in content
            assert compute_score(1, 2) == 3
    """)
    assert classify_test_function(func) == "real_execution"


def test_analyze_test_file_counts_and_lists_grep_shaped(tmp_path):
    f = tmp_path / "test_example.py"
    f.write_text(textwrap.dedent("""
        def test_real():
            from thing import compute_score
            assert compute_score(1, 2) == 3

        def test_grep_one():
            content = open("x.py").read()
            assert "foo" in content

        def test_grep_two():
            content = open("y.py").read()
            assert "bar" in content

        def test_ambiguous():
            assert True

        def not_a_test():
            pass
    """))
    result = analyze_test_file(f)
    assert result["real_execution"] == 1
    assert result["grep_shaped"] == 2
    assert result["other"] == 1
    assert set(result["grep_shaped_functions"]) == {"test_grep_one", "test_grep_two"}


def test_analyze_test_file_fails_open_on_syntax_error(tmp_path):
    f = tmp_path / "test_broken.py"
    f.write_text("def test_x(:\n    this is not valid python\n")
    result = analyze_test_file(f)
    assert result == {"path": str(f), "real_execution": 0, "grep_shaped": 0, "other": 0, "grep_shaped_functions": []}


def test_analyze_test_composition_matches_the_real_ratio_shape(tmp_path):
    """Reproduces the actual finding's shape: mostly grep-shaped, a small
    real-execution minority."""
    (tmp_path / "test_a.py").write_text(textwrap.dedent("""
        def test_real():
            from thing import run_binary
            assert run_binary() == 0
    """))
    for i in range(3):
        (tmp_path / f"test_grep_{i}.py").write_text(textwrap.dedent(f"""
            def test_names_present_{i}():
                content = open("f{i}.py").read()
                assert "Function{i}" in content
        """))

    result = analyze_test_composition(tmp_path)
    assert result["total_test_functions"] == 4
    assert result["real_execution"] == 1
    assert result["grep_shaped"] == 3
    assert result["grep_shaped_ratio"] == 0.75
    assert "Python only" in result["scope_note"]


def test_analyze_test_composition_skips_noise_dirs(tmp_path):
    noisy = tmp_path / "__pycache__"
    noisy.mkdir()
    (noisy / "test_ghost.py").write_text('def test_x():\n    assert 1 == 1\n')
    (tmp_path / "test_real.py").write_text(textwrap.dedent("""
        def test_real():
            from thing import run_binary
            assert run_binary() == 0
    """))
    result = analyze_test_composition(tmp_path)
    assert result["total_test_functions"] == 1


def test_analyze_test_composition_empty_project_is_zero_not_error(tmp_path):
    result = analyze_test_composition(tmp_path)
    assert result["total_test_functions"] == 0
    assert result["grep_shaped_ratio"] == 0.0
