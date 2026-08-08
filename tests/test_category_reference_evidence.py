"""check_category_reference_evidence -- mirrors check_prior_art_evidence's
own posture exactly (see test_prior_art_evidence.py): deterministic, no LLM,
advisory (flags, never blocks). Closes the gap `pcp inspiration-art` exists
for -- inert until a project has actually done category research."""

from pcp.commands.kickoff import check_category_reference_evidence


def test_inert_when_inspiration_art_does_not_exist():
    specs = {"device-inventory": {"description": "tracks enrolled devices"}}
    assert check_category_reference_evidence(specs, inspiration_art_exists=False) == []


def test_flags_module_missing_category_reference_when_inspiration_art_exists():
    specs = {"device-inventory": {"description": "tracks enrolled devices"}}
    findings = check_category_reference_evidence(specs, inspiration_art_exists=True)
    assert any("device-inventory" in f for f in findings)


def test_silent_when_category_reference_declared():
    specs = {
        "device-inventory": {
            "description": "tracks enrolled devices",
            "category_reference": {
                "category": "MDM/UEM",
                "classification": "adopted",
                "rationale": "standard shape",
            },
        }
    }
    assert check_category_reference_evidence(specs, inspiration_art_exists=True) == []
