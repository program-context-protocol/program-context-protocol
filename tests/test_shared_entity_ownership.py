"""check_shared_entity_ownership -- checks REAL enforcement (module
ownership + dependency wiring), not just documentation presence. Real gap
this closes, 2026-08-08 (Magellan-DataOps dogfood, worse than the page-
inventory gap): a domain entity referenced across multiple modules'
inter-module contracts with no module owning a schema for it -- each
module's build agent would invent its own shape independently."""

from pcp.commands.kickoff import check_shared_entity_ownership


def test_flags_entity_with_no_owning_module():
    specs = {
        "workbench": {"description": "handles task grading"},
        "quality-engine": {"description": "adjudicates flagged tasks"},
    }
    findings = check_shared_entity_ownership(["Task"], specs)
    assert any("Task" in f and "no module declaring ownership" in f for f in findings)


def test_flags_module_referencing_entity_without_declaring_dependency():
    specs = {
        "core-data-model": {
            "description": "owns shared domain entities",
            "owns_entities": ["Task"],
        },
        "quality-engine": {
            "description": "adjudicates flagged Task submissions",
            "objective_coverage": ["reviews Task records for quality"],
            "dependencies": [],
        },
    }
    findings = check_shared_entity_ownership(["Task"], specs)
    assert any("quality-engine" in f and "core-data-model" in f and "does not declare a dependency" in f for f in findings)


def test_silent_when_owner_declared_and_dependency_wired():
    specs = {
        "core-data-model": {
            "description": "owns shared domain entities",
            "owns_entities": ["Task"],
        },
        "quality-engine": {
            "description": "adjudicates flagged Task submissions",
            "objective_coverage": ["reviews Task records for quality"],
            "dependencies": ["core-data-model"],
        },
    }
    assert check_shared_entity_ownership(["Task"], specs) == []


def test_module_that_does_not_mention_entity_is_not_flagged_for_missing_dependency():
    specs = {
        "core-data-model": {"description": "owns shared domain entities", "owns_entities": ["Task"]},
        "unrelated-module": {"description": "handles billing invoices only", "dependencies": []},
    }
    findings = check_shared_entity_ownership(["Task"], specs)
    assert not any("unrelated-module" in f for f in findings)


def test_empty_shared_entities_is_silent():
    assert check_shared_entity_ownership([], {"x": {"description": "d"}}) == []


def test_owner_module_itself_never_flagged_for_its_own_entity():
    specs = {
        "core-data-model": {
            "description": "owns and defines the Task entity schema",
            "objective_coverage": ["Task schema definition"],
            "owns_entities": ["Task"],
        },
    }
    assert check_shared_entity_ownership(["Task"], specs) == []
