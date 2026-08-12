"""C-language test-suite composition audit (2026-08-09, backlog #1 --
fake-test detection was Python-only). Real incident this closes: Project W's
avrt adapter had a Mach call stripped to a hardcoded fallback literal, and
its test asserted against that exact same literal -- written in C, invisible
to test_composition.py's Python-ast-only pass."""

import textwrap
from collections import Counter

import pytest

from pcp.test_composition_c import (
    TREE_SITTER_AVAILABLE, has_any_assertion_c, assertion_literals_c, source_literal_pool_c,
    oracle_traceability_risk_c, analyze_c_test_file, analyze_c_test_composition,
)

pytestmark = pytest.mark.skipif(not TREE_SITTER_AVAILABLE, reason="tree-sitter/tree-sitter-c not installed")


def _parse(src: str):
    from pcp.test_composition_c import _PARSER
    source = textwrap.dedent(src).encode()
    tree = _PARSER.parse(source)
    func = next(n for n in tree.root_node.children if n.type == "function_definition")
    return func, source


def test_has_any_assertion_true_for_plain_assert():
    func, source = _parse("""
        void test_x(void) {
            assert(1 == 1);
        }
    """)
    assert has_any_assertion_c(func, source) is True


def test_has_any_assertion_false_for_empty_test():
    func, source = _parse("""
        void test_x(void) {
            int x = compute();
        }
    """)
    assert has_any_assertion_c(func, source) is False


def test_has_any_assertion_true_for_framework_macro_call():
    """CU_ASSERT/TEST_ASSERT_EQUAL/ck_assert style calls -- broad recall by
    design, not a hardcoded framework allowlist."""
    func, source = _parse("""
        void test_x(void) {
            CU_ASSERT_EQUAL(compute(), 5);
        }
    """)
    assert has_any_assertion_c(func, source) is True


def test_assertion_literals_extracts_direct_equality():
    func, source = _parse("""
        void test_x(void) {
            assert(get_value() == 12345);
        }
    """)
    assert "12345" in assertion_literals_c(func, source)


def test_assertion_literals_extracts_strcmp_idiom():
    func, source = _parse("""
        void test_x(void) {
            assert(strcmp(get_name(), "expected-name-value") == 0);
        }
    """)
    assert "expected-name-value" in assertion_literals_c(func, source)


def test_assertion_literals_ignores_trivial_numbers():
    func, source = _parse("""
        void test_x(void) {
            assert(ok() == 1);
            assert(count() == 0);
        }
    """)
    assert assertion_literals_c(func, source) == set()


def test_source_literal_pool_collects_string_and_number_literals():
    func, source = _parse("""
        int get_priority(void) {
            return 0xDEADBEEF;
        }
    """)
    pool = source_literal_pool_c(func, source)
    assert pool["0xDEADBEEF"] == 1


def test_oracle_traceability_risk_flags_shared_literal():
    func, source = _parse("""
        void test_thread_priority(void) {
            assert(strcmp(get_label(), "hardcoded-fallback-value") == 0);
        }
    """)
    risk = oracle_traceability_risk_c(func, source, Counter({"hardcoded-fallback-value": 1}))
    assert risk == {"hardcoded-fallback-value"}


def test_oracle_traceability_risk_clean_when_no_overlap():
    func, source = _parse("""
        void test_x(void) {
            assert(strcmp(get_label(), "genuinely-computed-value") == 0);
        }
    """)
    risk = oracle_traceability_risk_c(func, source, Counter({"some-other-unrelated-literal": 1}))
    assert risk == set()


def test_oracle_traceability_risk_ignores_widely_reused_literal():
    """Same real dogfood correction as the Python module -- a literal
    appearing many times in the source pool is shared vocabulary, not a
    fabricated fallback."""
    func, source = _parse("""
        void test_status(void) {
            assert(strcmp(get_status(), "complete-status") == 0);
        }
    """)
    risk = oracle_traceability_risk_c(func, source, Counter({"complete-status": 50}))
    assert risk == set()


def test_oracle_traceability_risk_ignores_short_numbers():
    func, source = _parse("""
        void test_x(void) {
            assert(get_retry_count() == 50);
        }
    """)
    risk = oracle_traceability_risk_c(func, source, Counter({"50": 1}))
    assert risk == set()


def test_analyze_c_test_file_end_to_end(tmp_path):
    """The exact avrt incident shape: implementation stripped to a hardcoded
    fallback, test asserts against that same literal."""
    (tmp_path / "thing.c").write_text(textwrap.dedent("""
        const char *get_priority_label(void) {
            return "hardcoded-fallback-value";
        }
    """))
    test_file = tmp_path / "test_thing.c"
    test_file.write_text(textwrap.dedent("""
        void test_get_priority_label(void) {
            assert(strcmp(get_priority_label(), "hardcoded-fallback-value") == 0);
        }
    """))
    result = analyze_c_test_file(test_file, project_root=tmp_path)
    assert result["available"] is True
    assert result["real_execution"] == 1
    assert result["oracle_risk"] == 1
    assert result["oracle_risk_functions"][0]["test_name"] == "test_get_priority_label"


def test_analyze_c_test_file_reports_assertion_free(tmp_path):
    test_file = tmp_path / "test_thing.c"
    test_file.write_text(textwrap.dedent("""
        void test_empty(void) {
            int x = 1;
        }

        void test_real(void) {
            assert(1 == 1);
        }
    """))
    result = analyze_c_test_file(test_file)
    assert result["real_execution"] == 2
    assert result["assertion_free"] == 1
    assert result["assertion_free_functions"] == ["test_empty"]


def test_analyze_c_test_file_skips_non_test_functions(tmp_path):
    test_file = tmp_path / "test_thing.c"
    test_file.write_text(textwrap.dedent("""
        int helper(void) {
            return 1;
        }

        void test_real(void) {
            assert(1 == 1);
        }
    """))
    result = analyze_c_test_file(test_file)
    assert result["real_execution"] == 1


def test_analyze_c_test_composition_rolls_up(tmp_path):
    (tmp_path / "thing.c").write_text(textwrap.dedent("""
        const char *get_priority_label(void) {
            return "hardcoded-fallback-value";
        }
    """))
    (tmp_path / "test_thing.c").write_text(textwrap.dedent("""
        void test_get_priority_label(void) {
            assert(strcmp(get_priority_label(), "hardcoded-fallback-value") == 0);
        }
    """))
    result = analyze_c_test_composition(tmp_path)
    assert result["available"] is True
    assert result["total_test_functions"] == 1
    assert result["oracle_risk"] == 1
    assert "macro-registered tests" in result["scope_note"]


def test_analyze_c_test_composition_empty_project_is_zero_not_error(tmp_path):
    result = analyze_c_test_composition(tmp_path)
    assert result["available"] is True
    assert result["total_test_functions"] == 0
    assert result["oracle_risk_ratio"] == 0.0
