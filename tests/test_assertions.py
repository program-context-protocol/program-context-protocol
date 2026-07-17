from pcp.assertions import parse_assertions, compute_coverage


# ── parse_assertions ──

def test_parses_numbered_list_under_success_heading():
    text = (
        "# Objective\n\n## What Success Looks Like\n"
        "1. Users can create an account\n"
        "2. Users can reset their password\n"
        "3. Admins can view usage reports\n"
    )
    assertions = parse_assertions(text)
    assert [a["id"] for a in assertions] == ["A1", "A2", "A3"]
    assert assertions[0]["text"] == "Users can create an account"


def test_no_numbered_list_returns_empty_not_error():
    text = "# Objective\n\nBuild a calculator that adds and subtracts numbers.\n"
    assert parse_assertions(text) == []


def test_ignores_numbers_not_at_line_start():
    text = "# Objective\n\nSupports up to 1. million users concurrently.\n"
    # "1." mid-sentence, not a real list item at line start -- shouldn't match
    assert parse_assertions(text) == []


def test_numbered_list_anywhere_in_doc_not_just_specific_heading():
    text = "# Objective\n\n## Out of Scope\n1. No mobile app\n2. No offline mode\n"
    assertions = parse_assertions(text)
    assert len(assertions) == 2


# ── compute_coverage ──

def test_covered_assertion_via_keyword_overlap():
    assertions = [{"id": "A1", "text": "Users can create an account with email and password"}]
    modules = {"auth": {"objective_coverage": ["Handles account creation with email and password validation"]}}
    result = compute_coverage(assertions, modules)
    assert result["coverage_score"] == 1.0
    assert result["coverage_gaps"] == []
    assert result["assertion_coverage_map"]["A1"] == ["auth"]


def test_uncovered_assertion_becomes_a_gap():
    assertions = [{"id": "A1", "text": "Admins can export usage reports as CSV"}]
    modules = {"auth": {"objective_coverage": ["Handles login and password reset"]}}
    result = compute_coverage(assertions, modules)
    assert result["coverage_score"] == 0.0
    assert len(result["coverage_gaps"]) == 1
    assert result["coverage_gaps"][0]["area"] == "Admins can export usage reports as CSV"


def test_partial_coverage_score():
    assertions = [
        {"id": "A1", "text": "Users can create an account"},
        {"id": "A2", "text": "Admins can export usage reports"},
    ]
    modules = {"auth": {"objective_coverage": ["Handles account creation and login"]}}
    result = compute_coverage(assertions, modules)
    assert result["coverage_score"] == 0.5
    assert result["assertions_covered"] == 1
    assert result["assertions_total"] == 2


def test_empty_assertions_scores_zero_not_error():
    assert compute_coverage([], {"auth": {"objective_coverage": ["x"]}})["coverage_score"] == 0.0


def test_module_with_no_objective_coverage_field_never_matches():
    assertions = [{"id": "A1", "text": "Users can reset their password"}]
    modules = {"auth": {}}
    result = compute_coverage(assertions, modules)
    assert result["coverage_score"] == 0.0
