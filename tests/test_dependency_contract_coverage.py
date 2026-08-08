"""check_dependency_contract_documented -- does dependency_map.md actually
document each declared spec.yaml dependency edge, not just list module
names? CTRL-007 already checks a dependency is BUILT; this checks whether
its shape was ever written down. Real gap this closes, 2026-08-08:
dependency_map.md was previously only ever written by `pcp import`, never
by `pcp kickoff` -- a fresh project's file stayed the raw placeholder."""

from pcp.commands.kickoff import check_dependency_contract_documented


def test_flags_undocumented_dependency_edge():
    specs = {
        "quality-engine": {"description": "d", "dependencies": ["core-data-model"]},
        "core-data-model": {"description": "d"},
    }
    # Blunt co-occurrence check, not pairing-aware -- a doc missing one side
    # of the edge entirely is the clear-cut case that must be flagged.
    findings = check_dependency_contract_documented(specs, "# Dependency Map\n\nModules: quality-engine only.")
    assert any("quality-engine" in f and "core-data-model" in f for f in findings)


def test_silent_when_both_names_documented():
    specs = {
        "quality-engine": {"description": "d", "dependencies": ["core-data-model"]},
    }
    text = "# Dependency Map\n\n## Inter-Module Contracts\n- quality-engine -> core-data-model: reads Task records."
    assert check_dependency_contract_documented(specs, text) == []


def test_no_dependencies_is_silent():
    specs = {"solo-module": {"description": "d", "dependencies": []}}
    assert check_dependency_contract_documented(specs, "") == []


def test_empty_dependency_map_flags_every_edge():
    specs = {"a": {"description": "d", "dependencies": ["b"]}}
    findings = check_dependency_contract_documented(specs, "")
    assert any("'a'" in f and "'b'" in f for f in findings)
