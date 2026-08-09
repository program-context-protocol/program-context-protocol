import yaml
from click.testing import CliRunner

from pcp.cli import cli
from pcp.commands.traceability import build_traceability, write_traceability


def _write_module(pcp_dir, name, spec, acceptance):
    mod_dir = pcp_dir / "strategy" / "modules" / name
    mod_dir.mkdir(parents=True)
    (mod_dir / "spec.yaml").write_text(yaml.dump(spec))
    (mod_dir / "acceptance.yaml").write_text(yaml.dump(acceptance))


def test_build_traceability_empty_project(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    result = build_traceability(pcp_dir)
    assert result["modules"] == []
    assert result["totals"] == {"total": 0, "complete": 0, "pending": 0, "other": 0}


def test_build_traceability_aggregates_coverage_and_criteria(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_module(
        pcp_dir, "auth",
        spec={"module": "auth", "description": "Auth module.",
              "objective_coverage": ["User login", "Password reset"],
              "dependencies": []},
        acceptance={"module": "auth", "criteria": [
            {"id": "A001", "description": "Password check", "check": "test_passes",
             "target": "src/auth/password.py", "status": "complete", "verified_by": "pcp_build"},
            {"id": "A002", "description": "Reset flow", "check": "manual", "status": "pending"},
        ]},
    )
    result = build_traceability(pcp_dir)
    assert len(result["modules"]) == 1
    m = result["modules"][0]
    assert m["module"] == "auth"
    assert m["objective_coverage"] == ["User login", "Password reset"]
    assert m["totals"] == {"total": 2, "complete": 1}
    assert result["totals"]["total"] == 2
    assert result["totals"]["complete"] == 1
    assert result["totals"]["pending"] == 1


def test_build_traceability_counts_non_pending_non_complete_as_other(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_module(
        pcp_dir, "billing",
        spec={"module": "billing", "description": "Billing.", "objective_coverage": []},
        acceptance={"module": "billing", "criteria": [
            {"id": "A001", "description": "x", "check": "manual", "status": "blocked-ci"},
        ]},
    )
    result = build_traceability(pcp_dir)
    assert result["totals"]["other"] == 1


def test_write_traceability_renders_markdown(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_module(
        pcp_dir, "auth",
        spec={"module": "auth", "description": "Auth.", "objective_coverage": ["User login"], "dependencies": ["core"]},
        acceptance={"module": "auth", "criteria": [
            {"id": "A001", "description": "Password check", "check": "test_passes",
             "target": "src/auth/password.py", "status": "complete", "verified_by": "pcp_build"},
        ]},
    )
    out = write_traceability(pcp_dir)
    assert out.exists()
    content = out.read_text()
    assert "Module: `auth`" in content
    assert "User login" in content
    assert "A001" in content
    assert "core" in content
    assert "Never hand-edit" in content


def test_traceability_cli_json(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["traceability", "--path", str(tmp_path), "--json"])
    assert result.exit_code == 0
    assert '"modules"' in result.output


def test_traceability_cli_writes_file(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["traceability", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert (pcp_dir / "traceability.md").exists()


def test_traceability_cli_no_pcp_dir_exits(tmp_path):
    runner = CliRunner()
    result = runner.invoke(cli, ["traceability", "--path", str(tmp_path)])
    assert result.exit_code == 2
