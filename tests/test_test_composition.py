"""Deterministic test-suite composition audit (2026-08-08) -- real finding:
255 tests, 100% passing, 97% were `assert "Name" in file_contents` (source-
text grep), only 3% actually executed a compiled binary. Tier 2 (grep
passes) and tier 4 (real-execution tested) are both real and both worth
distinguishing -- this module makes that distinction computable."""

import ast
import textwrap
from collections import Counter

from pcp.test_composition import (
    classify_test_function, analyze_test_file, analyze_test_composition, has_any_assertion,
    calls_only_its_own_patched_target, assertion_literals, source_literal_pool,
    oracle_traceability_risk,
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
        "self_mocked": 0, "self_mocked_functions": [],
        "oracle_risk": 0, "oracle_risk_functions": [],
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


# ── mock-hides-the-fake detection (2026-08-08) ──

def test_flags_a_test_that_only_calls_its_own_patched_target():
    """The exact fake pattern: patch compute_score, then call compute_score
    (which is now the Mock) and assert it returns what was just configured.
    Proves nothing about real compute_score."""
    func = _func("""
        def test_compute_score():
            from unittest.mock import patch
            with patch("mymodule.compute_score") as mock_compute:
                mock_compute.return_value = 42
                result = mymodule.compute_score(1, 2)
                assert result == 42
    """)
    assert calls_only_its_own_patched_target(func) is True


def test_does_not_flag_patching_a_dependency_while_calling_real_code():
    """Legitimate: patches a DIFFERENT symbol (the dependency) while
    calling the real orchestrating function under test."""
    func = _func("""
        def test_orchestrator():
            from unittest.mock import patch
            with patch("mymodule.slow_external_call") as mock_ext:
                mock_ext.return_value = "fake-response"
                result = mymodule.orchestrate(1, 2)
                assert result == "processed:fake-response"
    """)
    assert calls_only_its_own_patched_target(func) is False


def test_does_not_flag_a_test_with_no_patching_at_all():
    func = _func("""
        def test_plain():
            result = compute_score(1, 2)
            assert result == 3
    """)
    assert calls_only_its_own_patched_target(func) is False


def test_does_not_flag_when_a_real_subprocess_call_is_also_present():
    func = _func("""
        def test_mixed():
            from unittest.mock import patch
            with patch("mymodule.compute_score") as mock_compute:
                mock_compute.return_value = 42
                mymodule.compute_score(1, 2)
                import subprocess
                subprocess.run(["./real_binary"])
    """)
    assert calls_only_its_own_patched_target(func) is False


def test_mock_assertion_calls_alongside_fake_target_still_flagged():
    """A mock introspection call (assert_called_once_with) sitting next to
    the fake target call must not dilute the detection -- the core claim
    (compute_score returns 42) is still entirely mocked."""
    func = _func("""
        def test_compute_score():
            from unittest.mock import patch
            with patch("mymodule.compute_score") as mock_compute:
                mock_compute.return_value = 42
                result = mymodule.compute_score(1, 2)
                assert result == 42
                mock_compute.assert_called_once_with(1, 2)
    """)
    assert calls_only_its_own_patched_target(func) is True


def test_patch_object_form_also_detected():
    func = _func("""
        def test_compute_score():
            from unittest.mock import patch
            with patch.object(mymodule, "compute_score") as mock_compute:
                mock_compute.return_value = 42
                result = mymodule.compute_score(1, 2)
                assert result == 42
    """)
    assert calls_only_its_own_patched_target(func) is True


def test_decorator_style_patch_also_detected():
    func = _func("""
        @patch("mymodule.compute_score")
        def test_compute_score(mock_compute):
            mock_compute.return_value = 42
            result = mymodule.compute_score(1, 2)
            assert result == 42
    """)
    assert calls_only_its_own_patched_target(func) is True


def test_analyze_test_file_reports_self_mocked_functions(tmp_path):
    f = tmp_path / "test_fake_mock.py"
    f.write_text(textwrap.dedent("""
        from unittest.mock import patch

        def test_fake():
            with patch("mymodule.compute_score") as mock_compute:
                mock_compute.return_value = 42
                assert mymodule.compute_score(1, 2) == 42

        def test_real():
            assert compute_score(1, 2) == 3
    """))
    result = analyze_test_file(f)
    assert result["self_mocked"] == 1
    assert result["self_mocked_functions"] == ["test_fake"]


# ── oracle-traceability risk detection (2026-08-09, backlog #2) ──

def test_assertion_literals_extracts_equality_comparisons():
    func = _func("""
        def test_x():
            assert compute() == "expected-value-here"
            assert other() != "another-target"
    """)
    assert assertion_literals(func) == {"expected-value-here", "another-target"}


def test_assertion_literals_ignores_trivial_values():
    func = _func("""
        def test_x():
            assert flag() == True
            assert count() == 0
            assert result() == 1
    """)
    assert assertion_literals(func) == set()


def test_assertion_literals_ignores_in_checks():
    """`in`/`not in` is extract_grep_targets's territory, not this one --
    only `==`/`!=` count as "the test's claimed expected value"."""
    func = _func("""
        def test_x():
            assert "needle" in haystack()
    """)
    assert assertion_literals(func) == set()


def test_source_literal_pool_collects_every_literal():
    tree = ast.parse(textwrap.dedent("""
        FALLBACK = "hardcoded-fallback-value"

        def do_thing():
            return FALLBACK
    """))
    pool = source_literal_pool(tree)
    assert pool["hardcoded-fallback-value"] == 1


def test_oracle_traceability_risk_flags_shared_literal():
    func = _func("""
        def test_thread_priority():
            result = get_thread_priority()
            assert result == "hardcoded-fallback-value"
    """)
    risk = oracle_traceability_risk(func, Counter({"hardcoded-fallback-value": 1}))
    assert risk == {"hardcoded-fallback-value"}


def test_oracle_traceability_risk_ignores_short_strings():
    """Short strings are too common to be meaningful evidence of a shared
    fallback -- avoids flagging every test that happens to assert == "ok" """
    func = _func("""
        def test_x():
            assert status() == "ok"
    """)
    risk = oracle_traceability_risk(func, Counter({"ok": 1}))
    assert risk == set()


def test_oracle_traceability_risk_clean_when_no_overlap():
    func = _func("""
        def test_compute():
            assert compute(2, 3) == "genuinely-computed-result"
    """)
    risk = oracle_traceability_risk(func, Counter({"some-other-unrelated-literal": 1}))
    assert risk == set()


def test_oracle_traceability_risk_ignores_widely_reused_literal():
    """Real dogfood correction (2026-08-09): a literal appearing dozens of
    times across the source pool is shared vocabulary (an enum/status
    value tests are SUPPOSED to reference), not a fabricated fallback --
    real false positive found running this against PCP's own suite
    ("complete", 74 occurrences, flagged on nearly every status-related
    test before this fix)."""
    func = _func("""
        def test_status():
            assert get_status() == "complete-status"
    """)
    risk = oracle_traceability_risk(func, Counter({"complete-status": 74}))
    assert risk == set()


def test_oracle_traceability_risk_ignores_short_numbers():
    """Real bug found dogfooding 2026-08-09: the trivial-literals exclusion
    only covered a fixed few small ints (0, 1, -1, 2) -- any OTHER short
    number (3, 4, 50, 300) sailed through as "meaningful evidence." Small
    numbers recur constantly for unrelated reasons (counts, indices,
    timeouts) and prove nothing."""
    func = _func("""
        def test_x():
            assert get_retry_count() == 50
    """)
    risk = oracle_traceability_risk(func, Counter({50: 1}))
    assert risk == set()


def test_oracle_traceability_risk_flags_narrowly_scoped_long_number():
    """A distinctive, longer numeric constant (e.g. a hex/Mach-style value)
    appearing only once or twice in the source pool IS meaningful evidence
    -- the length filter targets short numbers, not all numbers."""
    func = _func("""
        def test_x():
            assert get_flag_value() == 3735928559
    """)
    risk = oracle_traceability_risk(func, Counter({3735928559: 1}))
    assert risk == {3735928559}


def test_analyze_test_file_flags_oracle_risk_with_project_root(tmp_path):
    """End-to-end: a test importing from a real source file whose
    implementation returns a hardcoded literal, and the test asserts
    against that exact same literal -- the avrt incident shape."""
    (tmp_path / "thing.py").write_text(textwrap.dedent("""
        def get_priority():
            return "hardcoded-fallback-value"  # real Mach call was stripped
    """))
    test_file = tmp_path / "test_thing.py"
    test_file.write_text(textwrap.dedent("""
        from thing import get_priority

        def test_get_priority():
            assert get_priority() == "hardcoded-fallback-value"
    """))
    result = analyze_test_file(test_file, project_root=tmp_path)
    assert result["oracle_risk"] == 1
    assert result["oracle_risk_functions"][0]["test_name"] == "test_get_priority"
    assert "hardcoded-fallback-value" in result["oracle_risk_functions"][0]["shared_literals"]


def test_analyze_test_file_skips_oracle_check_without_project_root(tmp_path):
    """No project_root given -- skipped honestly (0), never guessed at."""
    test_file = tmp_path / "test_thing.py"
    test_file.write_text(textwrap.dedent("""
        def test_x():
            assert compute() == "some-literal-value"
    """))
    result = analyze_test_file(test_file)
    assert result["oracle_risk"] == 0


def test_analyze_test_composition_rolls_up_oracle_risk(tmp_path):
    (tmp_path / "thing.py").write_text(textwrap.dedent("""
        def get_priority():
            return "hardcoded-fallback-value"
    """))
    (tmp_path / "test_thing.py").write_text(textwrap.dedent("""
        from thing import get_priority

        def test_get_priority():
            assert get_priority() == "hardcoded-fallback-value"
    """))
    result = analyze_test_composition(tmp_path)
    assert result["oracle_risk"] == 1
    assert result["oracle_risk_ratio"] == 1.0
