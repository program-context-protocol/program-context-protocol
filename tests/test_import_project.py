from unittest.mock import patch

import yaml
from click.testing import CliRunner

from pcp.cli import cli
from pcp.commands.import_project import _generate_module, _default_module_shape
from pcp.schema.validator import validate_file


# ── _default_module_shape: v2.0-shaped fallback, used both on LLM failure and --skip-specs ──

def test_default_module_shape_is_v2_gated():
    spec, acceptance = _default_module_shape("widgets", ["a.py", "b.py"], deps=["core"])
    assert spec["module"] == "widgets"
    assert spec["dependencies"] == ["core"]
    crit_ids = {c["id"] for c in acceptance["criteria"]}
    assert "BF_001" in crit_ids
    for c in acceptance["criteria"]:
        assert c["logic_tier"] == 1
        assert c["build_vs_buy"]["decision"] == "build_fresh"


def test_default_module_shape_adds_decouple_criteria_for_deps():
    spec, acceptance = _default_module_shape("widgets", ["a.py"], deps=["core", "auth", "billing", "extra"])
    crit_ids = {c["id"] for c in acceptance["criteria"]}
    # BF_001 + up to 3 decouple criteria, deps beyond 3 dropped (not silently duplicated)
    assert crit_ids == {"BF_001", "BF_002", "BF_003", "BF_004"}


# ── _generate_module: LLM success path, normalized+versioned ──

def test_generate_module_llm_success_is_normalized_and_v2(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    llm_response = {
        "spec": {
            "module": "widgets", "description": "handles widget rendering",
            "dependencies": ["core"], "constraints": [],
            "build_vs_buy": {"decision": "not_applicable", "rationale": "r", "candidates_considered": []},
        },
        "acceptance": {
            "criteria": [
                {"id": "BF_001", "description": "characterize widgets", "check": "test_passes",
                 "status": "pending", "logic_tier": 1,
                 "build_vs_buy": {"decision": "build_fresh", "rationale": "r", "candidates_considered": []}},
            ]
        },
    }
    with patch("pcp.commands.import_project.llm.call_json", return_value=llm_response):
        spec, acceptance, warnings = _generate_module(
            "widgets", ["src/widgets.py"], {("widgets", "core"): 3}, "a widget app", pcp_dir=pcp_dir,
        )
    assert spec["version"] == "2.0"
    assert acceptance["version"] == "2.0"
    assert acceptance["module"] == "widgets"
    assert warnings == []


def test_generate_module_llm_failure_falls_back_to_default_shape(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    with patch("pcp.commands.import_project.llm.call_json", side_effect=RuntimeError("timeout")):
        spec, acceptance, warnings = _generate_module(
            "widgets", ["src/widgets.py"], {}, "a widget app", pcp_dir=pcp_dir,
        )
    assert spec["version"] == "2.0"
    assert acceptance["version"] == "2.0"
    assert any(c["id"] == "BF_001" for c in acceptance["criteria"])


def test_generate_module_coerces_invalid_llm_fields(tmp_path):
    """An LLM-invented logic_tier/status outside the schema enum gets coerced
    and flagged, same posture kickoff.py already applies -- not silently
    written to disk to hard-block the first real commit."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    llm_response = {
        "spec": {"module": "widgets", "description": "x", "dependencies": [], "constraints": []},
        "acceptance": {
            "criteria": [
                {"id": "BF_001", "description": "x", "check": "automated", "status": "done", "logic_tier": 99},
            ]
        },
    }
    with patch("pcp.commands.import_project.llm.call_json", return_value=llm_response):
        spec, acceptance, warnings = _generate_module(
            "widgets", ["src/widgets.py"], {}, "a widget app", pcp_dir=pcp_dir,
        )
    assert acceptance["criteria"][0]["check"] == "manual"
    assert acceptance["criteria"][0]["status"] == "complete"
    assert acceptance["criteria"][0]["logic_tier"] == 6
    assert len(warnings) >= 3


# ── CLI end-to-end: generated acceptance.yaml passes real schema validation ──

def _write_tiny_python_project(root):
    (root / "widgets").mkdir(parents=True)
    (root / "widgets" / "__init__.py").write_text("")
    (root / "widgets" / "core.py").write_text("def render():\n    pass\n")
    (root / "core").mkdir(parents=True)
    (root / "core" / "__init__.py").write_text("")
    (root / "core" / "util.py").write_text("def helper():\n    pass\n")


def test_import_cli_writes_schema_valid_v2_acceptance(tmp_path):
    _write_tiny_python_project(tmp_path)
    llm_response = {
        "spec": {"module": "PLACEHOLDER", "description": "x", "dependencies": [], "constraints": []},
        "acceptance": {"criteria": [
            {"id": "BF_001", "description": "x", "check": "test_passes", "status": "pending", "logic_tier": 1,
             "build_vs_buy": {"decision": "build_fresh", "rationale": "r", "candidates_considered": []}},
        ]},
    }
    with patch("pcp.commands.import_project.llm.call_json", return_value=llm_response):
        runner = CliRunner()
        result = runner.invoke(cli, ["import", "a tiny test app", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output

    modules_dir = tmp_path / ".pcp" / "strategy" / "modules"
    assert modules_dir.exists()
    found_any = False
    for mod_dir in modules_dir.iterdir():
        acc_path = mod_dir / "acceptance.yaml"
        assert acc_path.exists()
        data = yaml.safe_load(acc_path.read_text())
        assert data["version"] == "2.0"
        errors = validate_file(acc_path, "module_acceptance")
        assert errors == [], errors
        spec_data = yaml.safe_load((mod_dir / "spec.yaml").read_text())
        assert spec_data["version"] == "2.0"
        found_any = True
    assert found_any


def test_import_cli_skip_specs_still_produces_v2_shape(tmp_path):
    _write_tiny_python_project(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["import", "a tiny test app", "--path", str(tmp_path), "--skip-specs"])
    assert result.exit_code == 0, result.output

    modules_dir = tmp_path / ".pcp" / "strategy" / "modules"
    for mod_dir in modules_dir.iterdir():
        acc_path = mod_dir / "acceptance.yaml"
        data = yaml.safe_load(acc_path.read_text())
        assert data["version"] == "2.0"
        errors = validate_file(acc_path, "module_acceptance")
        assert errors == [], errors
