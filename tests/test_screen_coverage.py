"""check_screen_coverage -- immediate warning at kickoff/pm generation time
for a UI-facing criterion missing `screen`, mirroring capabilities_
enumerated/shared_entities_enumerated's existing immediate-warning posture
instead of leaving this to surface only much later via a separate
`pcp design-audit` run. Added 2026-08-08 alongside wiring `screen` into
the generation prompt itself."""

from pcp.commands.kickoff import check_screen_coverage


def test_flags_ui_facing_criterion_missing_screen():
    acceptances = {
        "workbench": {
            "criteria": [
                {"id": "A001", "description": "Grading dashboard shows code diff", "check": "manual"},
            ]
        }
    }
    findings = check_screen_coverage(acceptances)
    assert any("workbench" in f and "A001" in f for f in findings)


def test_silent_when_screen_declared():
    acceptances = {
        "workbench": {
            "criteria": [
                {"id": "A001", "description": "Grading dashboard shows code diff", "check": "manual", "screen": "Code grading"},
            ]
        }
    }
    assert check_screen_coverage(acceptances) == []


def test_silent_for_non_ui_criterion():
    acceptances = {
        "backend": {
            "criteria": [
                {"id": "A001", "description": "API validates the payment webhook signature", "check": "manual"},
            ]
        }
    }
    assert check_screen_coverage(acceptances) == []


def test_handles_empty_or_missing_criteria():
    assert check_screen_coverage({"empty": {}}) == []
    assert check_screen_coverage({"empty": {"criteria": []}}) == []
