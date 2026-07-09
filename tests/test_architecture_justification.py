import yaml
from click.testing import CliRunner

from pcp.cli import cli
from pcp.commands.architecture_justification import build_architecture_justification, write_architecture_justification


def _write_module(pcp_dir, name, spec, acceptance):
    mod_dir = pcp_dir / "strategy" / "modules" / name
    mod_dir.mkdir(parents=True)
    (mod_dir / "spec.yaml").write_text(yaml.dump(spec))
    (mod_dir / "acceptance.yaml").write_text(yaml.dump(acceptance))


def test_build_architecture_justification_empty_project(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    result = build_architecture_justification(pcp_dir)
    assert result["modules"] == []
    assert result["flagged_count"] == 0


def test_build_architecture_justification_aggregates_module_and_criteria(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_module(
        pcp_dir, "auth",
        spec={"module": "auth", "description": "Auth module.",
              "build_vs_buy": {"decision": "reuse_whole", "rationale": "Auth0 fits cleanly", "candidates_considered": ["Auth0", "Clerk"]}},
        acceptance={"module": "auth", "criteria": [
            {"id": "A001", "description": "Password check", "check": "manual", "status": "pending",
             "logic_tier": 1, "build_vs_buy": {"decision": "build_fresh", "rationale": "trivial"}},
        ]},
    )
    result = build_architecture_justification(pcp_dir)
    assert len(result["modules"]) == 1
    m = result["modules"][0]
    assert m["module"] == "auth"
    assert m["module_build_vs_buy"]["decision"] == "reuse_whole"
    assert m["criteria"][0]["logic_tier"] == 1
    assert result["tier_counts"][1] == 1
    assert result["flagged_count"] == 0


def test_build_architecture_justification_flags_coerced_placeholders(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_module(
        pcp_dir, "add",
        spec={"module": "add", "description": "Adds numbers.",
              "build_vs_buy": {"decision": "not_applicable", "rationale": "pure business logic"}},
        acceptance={"module": "add", "criteria": [
            {"id": "A001", "description": "Add works", "check": "manual", "status": "pending",
             "logic_tier": 6,
             "build_vs_buy": {"decision": "build_fresh",
                              "rationale": "Not specified by generator -- coerced placeholder, review before treating as a real decision."}},
        ]},
    )
    result = build_architecture_justification(pcp_dir)
    assert result["flagged_count"] == 1
    assert result["modules"][0]["criteria"][0]["flagged"] is True


def test_write_architecture_justification_renders_markdown(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_module(
        pcp_dir, "add",
        spec={"module": "add", "description": "Adds numbers.",
              "build_vs_buy": {"decision": "not_applicable", "rationale": "pure business logic"}},
        acceptance={"module": "add", "criteria": [
            {"id": "A001", "description": "Add works", "check": "manual", "status": "pending",
             "logic_tier": 1, "build_vs_buy": {"decision": "build_fresh", "rationale": "trivial"}},
        ]},
    )
    out = write_architecture_justification(pcp_dir)
    assert out.exists()
    content = out.read_text()
    assert "Module: `add`" in content
    assert "A001" in content
    assert "Never hand-edit" in content


def test_architecture_justification_cli_json(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["architecture-justification", "--path", str(tmp_path), "--json"])
    assert result.exit_code == 0
    assert '"modules"' in result.output


def test_architecture_justification_cli_writes_file(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["architecture-justification", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert (pcp_dir / "architecture_justification.md").exists()


def test_architecture_justification_cli_no_pcp_dir_exits(tmp_path):
    runner = CliRunner()
    result = runner.invoke(cli, ["architecture-justification", "--path", str(tmp_path)])
    assert result.exit_code == 2
