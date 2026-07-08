from click.testing import CliRunner

from pcp.cli import cli
from pcp.commands.init import upsert_pcp_claude_block, PCP_CLAUDE_BLOCK_START, PCP_CLAUDE_BLOCK_END
from pcp.schema.validator import validate_file


def test_init_scaffolds_expected_files(tmp_path):
    runner = CliRunner()
    result = runner.invoke(cli, ["init", "--path", str(tmp_path)])
    assert result.exit_code == 0

    pcp = tmp_path / ".pcp"
    for rel in [
        "objective.md", "target_state.md", "architecture.md", "ci_rules.yaml",
        "controls.yaml", "SDLC_phase.yaml", "strategy/decomposition.md",
        "architect_persona.md", "kb/adr/ADR-001-example.md", "kb/domain/general.md",
        "policies/escalation.rego", "policies/bypass_approval.rego", "policies/coupling_threshold.rego",
    ]:
        assert (pcp / rel).exists(), f"missing {rel}"

    assert (tmp_path / "CLAUDE.md").exists()
    assert (tmp_path / ".gitattributes").exists()


def test_init_scaffolded_policies_are_valid_rego(tmp_path):
    """Real opa parse check, not just 'the file exists' -- a syntax error in a
    scaffolded policy would silently degrade to {"available": True, "undefined":
    True} everywhere it's queried, never surfacing as an error."""
    import shutil
    import subprocess
    import pytest
    if not shutil.which("opa"):
        pytest.skip("opa binary not installed")
    runner = CliRunner()
    runner.invoke(cli, ["init", "--path", str(tmp_path)])
    policies_dir = tmp_path / ".pcp" / "policies"
    result = subprocess.run(["opa", "eval", "-d", str(policies_dir), "data.pcp"],
                             capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_init_generated_ci_rules_is_schema_valid(tmp_path):
    runner = CliRunner()
    runner.invoke(cli, ["init", "--path", str(tmp_path)])
    errors = validate_file(tmp_path / ".pcp" / "ci_rules.yaml", "ci_rules")
    assert errors == []


def test_init_generated_sdlc_phase_is_schema_valid(tmp_path):
    runner = CliRunner()
    runner.invoke(cli, ["init", "--path", str(tmp_path)])
    errors = validate_file(tmp_path / ".pcp" / "SDLC_phase.yaml", "sdlc_phase")
    assert errors == []


def test_init_generated_ci_rules_do_not_self_match_their_own_file(tmp_path):
    """Regression: MOD_001/MOD_002 previously had no `scope`, so their own
    pattern text inside ci_rules.yaml matched their own ast_pattern rule --
    every fresh `pcp init` scaffold hard-blocked on its own first commit."""
    from pcp.commands.check import _run_ast_rule
    from pcp.schema.validator import load_yaml
    runner = CliRunner()
    runner.invoke(cli, ["init", "--path", str(tmp_path)])
    pcp_dir = tmp_path / ".pcp"
    data = load_yaml(pcp_dir / "ci_rules.yaml")
    ast_rules = [r for r in data["rules"] if r["check"] == "ast_pattern"]
    for rule in ast_rules:
        violations = _run_ast_rule(rule, [".pcp/ci_rules.yaml"], tmp_path)
        assert violations == [], f"{rule['id']} self-matches its own ci_rules.yaml definition"


def test_init_generated_controls_yaml_parses_and_has_twelve_controls(tmp_path):
    import yaml
    runner = CliRunner()
    runner.invoke(cli, ["init", "--path", str(tmp_path)])
    data = yaml.safe_load((tmp_path / ".pcp" / "controls.yaml").read_text())
    assert len(data["controls"]) == 12
    assert {c["id"] for c in data["controls"]} == {f"CTRL-{i:03d}" for i in range(1, 13)}


def test_init_skips_existing_files_without_force(tmp_path):
    runner = CliRunner()
    runner.invoke(cli, ["init", "--path", str(tmp_path)])
    objective_path = tmp_path / ".pcp" / "objective.md"
    objective_path.write_text("# My real objective\nCustom content.")

    result = runner.invoke(cli, ["init", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "skipped" in result.output
    assert objective_path.read_text() == "# My real objective\nCustom content."


def test_init_force_overwrites_existing_files(tmp_path):
    runner = CliRunner()
    runner.invoke(cli, ["init", "--path", str(tmp_path)])
    objective_path = tmp_path / ".pcp" / "objective.md"
    objective_path.write_text("# My real objective\nCustom content.")

    result = runner.invoke(cli, ["init", "--path", str(tmp_path), "--force"])
    assert result.exit_code == 0
    assert "Program Objective" in objective_path.read_text()


def test_init_with_module_scaffolds_spec_and_acceptance(tmp_path):
    runner = CliRunner()
    result = runner.invoke(cli, ["init", "--path", str(tmp_path), "--module", "add"])
    assert result.exit_code == 0

    mod_dir = tmp_path / ".pcp" / "strategy" / "modules" / "add"
    assert (mod_dir / "spec.yaml").exists()
    assert (mod_dir / "acceptance.yaml").exists()
    assert "module: add" in (mod_dir / "spec.yaml").read_text()

    acceptance_errors = validate_file(mod_dir / "acceptance.yaml", "module_acceptance")
    assert acceptance_errors == []


def test_init_module_acceptance_has_modularity_criteria_baked_in(tmp_path):
    import yaml
    runner = CliRunner()
    runner.invoke(cli, ["init", "--path", str(tmp_path), "--module", "add"])
    data = yaml.safe_load((tmp_path / ".pcp" / "strategy" / "modules" / "add" / "acceptance.yaml").read_text())
    ids = {c["id"] for c in data["criteria"]}
    assert {"MOD_A001", "MOD_A002", "MOD_A003", "MOD_A004", "A001"} <= ids


# ── CLAUDE.md governance block ──

def test_upsert_creates_claude_md_when_absent(tmp_path):
    claude_md = tmp_path / "CLAUDE.md"
    changed = upsert_pcp_claude_block(claude_md)
    assert changed is True
    content = claude_md.read_text()
    assert PCP_CLAUDE_BLOCK_START in content
    assert "PCP Governance" in content


def test_upsert_preserves_human_content_outside_block(tmp_path):
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("# My Custom Project Rules\nDon't touch this.\n")
    upsert_pcp_claude_block(claude_md)
    content = claude_md.read_text()
    assert "Don't touch this." in content
    assert PCP_CLAUDE_BLOCK_START in content


def test_upsert_refreshes_existing_block_without_duplicating(tmp_path):
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text(f"# Rules\nCustom.\n\n{PCP_CLAUDE_BLOCK_START}\nSTALE OLD CONTENT\n{PCP_CLAUDE_BLOCK_END}\n")
    changed = upsert_pcp_claude_block(claude_md)
    assert changed is True
    content = claude_md.read_text()
    assert "STALE OLD CONTENT" not in content
    assert content.count(PCP_CLAUDE_BLOCK_START) == 1
    assert "Custom." in content


def test_upsert_no_op_when_already_current(tmp_path):
    claude_md = tmp_path / "CLAUDE.md"
    upsert_pcp_claude_block(claude_md)
    changed_again = upsert_pcp_claude_block(claude_md)
    assert changed_again is False


# ── .gitattributes ──

def test_gitattributes_gets_merge_directives(tmp_path):
    runner = CliRunner()
    runner.invoke(cli, ["init", "--path", str(tmp_path)])
    content = (tmp_path / ".gitattributes").read_text()
    assert ".pcp/current_state.md merge=ours" in content
    assert ".pcp/bypass_log.yaml merge=union" in content


def test_gitattributes_not_duplicated_on_second_init(tmp_path):
    runner = CliRunner()
    runner.invoke(cli, ["init", "--path", str(tmp_path)])
    runner.invoke(cli, ["init", "--path", str(tmp_path)])
    content = (tmp_path / ".gitattributes").read_text()
    assert content.count(".pcp/current_state.md merge=ours") == 1
