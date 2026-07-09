from pathlib import Path

from pcp.schema import validator
from pcp.schema.validator import SCHEMA_DIR, validate_file

EXPECTED_SCHEMAS = {
    "ci_rules", "controls", "module_acceptance", "module_spec", "sdlc_phase",
}


def test_schema_dir_resolves_relative_to_validator_itself():
    """Real bug, found 2026-07-08: SCHEMA_DIR used to be
    Path(__file__).parent.parent.parent.parent / "schema" -- four hard-coded
    parent-hops assuming a repo-root schema/ dir sits exactly there. That's
    only true in this exact dev-repo layout; a real installed package
    (site-packages/pcp/schema/validator.py, editable or not) has nothing
    four levels up, so every validate_file() call silently pointed at a
    nonexistent path for anyone using PCP as an actually-installed
    dependency (confirmed against a real, non-editable wheel install).
    SCHEMA_DIR must resolve relative to validator.py's own location, not a
    fixed hop count, so it works identically regardless of how or where
    the package ends up installed."""
    assert SCHEMA_DIR == Path(validator.__file__).parent


def test_all_expected_schema_files_exist_alongside_validator():
    for name in EXPECTED_SCHEMAS:
        path = SCHEMA_DIR / f"{name}.schema.json"
        assert path.exists(), f"missing schema file: {path}"


def test_pyproject_wheel_artifacts_include_schema_json():
    """Closes the other half of the same bug: hatchling does not bundle
    non-.py files under `packages` automatically (confirmed -- the
    skill_data/*.md files needed an explicit `artifacts` entry too). A
    schema file that exists on disk in the dev repo but isn't declared here
    would build a wheel that's STILL broken despite SCHEMA_DIR being
    correct, since the file simply wouldn't be in the package at all."""
    import tomllib

    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())
    artifacts = data["tool"]["hatch"]["build"]["targets"]["wheel"]["artifacts"]
    assert any("schema" in a and a.endswith(".schema.json") for a in artifacts), (
        f"no artifacts entry covers schema/*.schema.json: {artifacts}"
    )


def test_validate_file_works_against_a_real_module_acceptance_file(tmp_path):
    import yaml

    p = tmp_path / "acceptance.yaml"
    p.write_text(yaml.dump({
        "version": "1.0", "module": "test",
        "criteria": [{"id": "A001", "description": "d", "check": "manual", "status": "pending"}],
    }))
    assert validate_file(p, "module_acceptance") == []


def test_validate_file_reports_real_schema_violations(tmp_path):
    import yaml

    p = tmp_path / "acceptance.yaml"
    p.write_text(yaml.dump({
        "version": "1.0", "module": "test",
        "criteria": [{"id": "A001", "description": "d", "check": "not-a-real-check", "status": "pending"}],
    }))
    errors = validate_file(p, "module_acceptance")
    assert errors
    assert any("check" in e for e in errors)


# ── logic_tier / build_vs_buy: version-gated (2.0 requires them, 1.0 doesn't) ──

def test_module_acceptance_v1_does_not_require_logic_tier(tmp_path):
    import yaml

    p = tmp_path / "acceptance.yaml"
    p.write_text(yaml.dump({
        "version": "1.0", "module": "test",
        "criteria": [{"id": "A001", "description": "d", "check": "manual", "status": "pending"}],
    }))
    assert validate_file(p, "module_acceptance") == []


def test_module_acceptance_v2_requires_logic_tier_and_build_vs_buy(tmp_path):
    import yaml

    p = tmp_path / "acceptance.yaml"
    p.write_text(yaml.dump({
        "version": "2.0", "module": "test",
        "criteria": [{"id": "A001", "description": "d", "check": "manual", "status": "pending"}],
    }))
    errors = validate_file(p, "module_acceptance")
    assert errors, "version 2.0 criteria missing logic_tier/build_vs_buy should fail"


def test_module_acceptance_v2_passes_with_logic_tier_and_build_vs_buy(tmp_path):
    import yaml

    p = tmp_path / "acceptance.yaml"
    p.write_text(yaml.dump({
        "version": "2.0", "module": "test",
        "criteria": [{
            "id": "A001", "description": "d", "check": "manual", "status": "pending",
            "logic_tier": 1,
            "build_vs_buy": {"decision": "build_fresh", "rationale": "small enough to write directly"},
        }],
    }))
    assert validate_file(p, "module_acceptance") == []


def test_module_spec_v1_does_not_require_build_vs_buy(tmp_path):
    import yaml

    p = tmp_path / "spec.yaml"
    p.write_text(yaml.dump({"version": "1.0", "module": "test", "description": "A test module for coverage."}))
    assert validate_file(p, "module_spec") == []


def test_module_spec_v2_requires_build_vs_buy(tmp_path):
    import yaml

    p = tmp_path / "spec.yaml"
    p.write_text(yaml.dump({"version": "2.0", "module": "test", "description": "A test module for coverage."}))
    errors = validate_file(p, "module_spec")
    assert errors, "version 2.0 module spec missing build_vs_buy should fail"


def test_module_spec_v2_passes_with_not_applicable_build_vs_buy(tmp_path):
    import yaml

    p = tmp_path / "spec.yaml"
    p.write_text(yaml.dump({
        "version": "2.0", "module": "test", "description": "A test module for coverage.",
        "build_vs_buy": {"decision": "not_applicable", "rationale": "pure business-logic module"},
    }))
    assert validate_file(p, "module_spec") == []
