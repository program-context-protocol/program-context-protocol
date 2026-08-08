"""check_capability_criterion_coverage -- sharper than check_capability_coverage:
checks each enumerated capability against actual CRITERION descriptions, not
just a module's broader objective_coverage prose. Real gap this closes,
2026-08-08 (Magellan-DataOps dogfood): "preference-ranking: A/B compare two
AI responses" read as covered by Task Workbench's broad description, but had
zero criteria implementing it -- design_audit.md's Feature Exposure Ladder
can't catch this by construction, it only scores criteria that exist."""

from pcp.commands.kickoff import check_capability_criterion_coverage


def test_flags_capability_with_no_matching_criterion():
    acceptances = {
        "workbench": {
            "criteria": [
                {"id": "A001", "description": "Multi-turn conversation review panel"},
            ]
        }
    }
    findings = check_capability_criterion_coverage(
        ["Preference ranking: side-by-side comparison of two AI responses"], acceptances,
    )
    assert any("Preference ranking" in f for f in findings)


def test_silent_when_a_criterion_keyword_matches():
    acceptances = {
        "workbench": {
            "criteria": [
                {"id": "A001", "description": "Preference ranking screen shows two responses side by side"},
            ]
        }
    }
    findings = check_capability_criterion_coverage(
        ["Preference ranking: side-by-side comparison of two AI responses"], acceptances,
    )
    assert findings == []


def test_matches_across_modules_not_just_one():
    acceptances = {
        "workbench": {"criteria": [{"id": "A001", "description": "Grading queue"}]},
        "quality-engine": {"criteria": [{"id": "A002", "description": "Adjudication and appeal review"}]},
    }
    findings = check_capability_criterion_coverage(["Appeal review workflow"], acceptances)
    assert findings == []


def test_empty_capabilities_is_silent():
    assert check_capability_criterion_coverage([], {"x": {"criteria": []}}) == []


def test_handles_module_with_no_criteria_key():
    acceptances = {"expert-directory": {}}
    findings = check_capability_criterion_coverage(["Expert onboarding form"], acceptances)
    assert any("Expert onboarding" in f for f in findings)
