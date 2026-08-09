"""Deterministic test-suite composition audit (2026-08-08) -- real finding:
255 tests, 100% passing, 97% were `assert "Name" in file_contents` (source-
text grep), only 3% actually executed a compiled binary. Tier 2 (grep
passes) and tier 4 (real-execution tested) are both real and both worth
distinguishing -- this module makes that distinction computable."""

import ast
import textwrap

from pcp.test_composition import (
    classify_test_function, analyze_test_file, analyze_test_composition, has_any_assertion,
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
    by_name = {g["test_name"]: g["targets"] for g in result["grep_shaped_functions"]}
    assert by_name == {"test_grep_one": ["foo"], "test_grep_two": ["bar"]}


def test_analyze_test_file_fails_open_on_syntax_error(tmp_path):
    f = tmp_path / "test_broken.py"
    f.write_text("def test_x(:\n    this is not valid python\n")
    result = analyze_test_file(f)
    assert result == {
        "path": str(f), "real_execution": 0, "grep_shaped": 0, "other": 0,
        "grep_shaped_functions": [], "assertion_free": 0, "assertion_free_functions": [],
    }


# ── facet 3: assertion-free detection (2026-08-08) ──

def test_has_any_assertion_true_for_plain_assert():
    func = _func("""
        def test_x():
            assert 1 == 1
    """)
    assert has_any_assertion(func) is True


def test_has_any_assertion_false_for_truly_empty_test():
    func = _func("""
        def test_setup_only():
            x = compute_something()
            y = x + 1
    """)
    assert has_any_assertion(func) is False


def test_has_any_assertion_true_for_pytest_raises_context():
    func = _func("""
        def test_raises():
            import pytest
            with pytest.raises(ValueError):
                do_thing()
    """)
    assert has_any_assertion(func) is True


def test_has_any_assertion_true_for_unittest_style_self_assert():
    func = _func("""
        class T:
            def test_x(self):
                self.assertEqual(1, 1)
    """)
    # extract the method node, not the class
    method = func.body[0]
    assert has_any_assertion(method) is True


def test_has_any_assertion_true_for_mock_assertion_method():
    """Mock/MagicMock's own assert_called_once()/assert_not_called() etc. are
    real assertions, called on an arbitrary variable name, not `self` --
    real false positive found running this against PCP's own suite
    (test_cross_vendor_off_by_default_never_called used mock_agy.assert_not_called()
    with no plain `assert`, was wrongly flagged before this fix)."""
    func = _func("""
        def test_x():
            mock_thing = MagicMock()
            do_call(mock_thing)
            mock_thing.assert_called_once()
    """)
    assert has_any_assertion(func) is True


def test_has_any_assertion_false_for_unrelated_self_call():
    func = _func("""
        class T:
            def test_x(self):
                self.setup_thing()
    """)
    method = func.body[0]
    assert has_any_assertion(method) is False


def test_analyze_test_file_reports_assertion_free_functions(tmp_path):
    f = tmp_path / "test_example2.py"
    f.write_text(textwrap.dedent("""
        def test_empty():
            x = 1

        def test_real():
            assert 1 == 1
    """))
    result = analyze_test_file(f)
    assert result["assertion_free"] == 1
    assert result["assertion_free_functions"] == ["test_empty"]


def test_analyze_test_composition_totals_assertion_free(tmp_path):
    (tmp_path / "test_a.py").write_text(textwrap.dedent("""
        def test_empty_one():
            pass

        def test_empty_two():
            pass

        def test_real():
            assert 1 == 1
    """))
    result = analyze_test_composition(tmp_path)
    assert result["assertion_free"] == 2
    assert result["assertion_free_ratio"] == round(2 / 3, 4)


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
