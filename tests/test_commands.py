import re
import sys
import json
import tempfile
import yaml
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from click.testing import CliRunner

from pcp.cli import cli


@pytest.fixture
def temp_project(tmp_path):
    # Create a basic structure
    return tmp_path


def test_kickoff_success(temp_project):
    vision_file = temp_project / "vision.md"
    vision_file.write_text("Build a simple calculator app with add and subtract modules.")

    mock_kickoff_response = {
        "objective": "# Program Objective\n\nCalculator app.",
        "target_state": "# Target State\n\nCalculator done.",
        "architecture": "# Architecture\n\nPython.",
        "decomposition": "# Strategy Decomposition\n\nModules: add, subtract.",
        "sdlc_phase": {
            "version": "1.0",
            "current_phase": "planning",
            "phases": [
                {
                    "name": "planning",
                    "exit_criteria": [
                        {
                            "id": "E001",
                            "description": "Strategy decomposition approved by PM",
                            "check": "manual",
                            "status": "pending"
                        }
                    ]
                },
                {
                    "name": "alpha",
                    "exit_criteria": [
                        {
                            "id": "E001",
                            "description": "Core features implemented",
                            "check": "manual",
                            "status": "pending"
                        }
                    ]
                }
            ]
        },
        "modules": [
            {
                "name": "add",
                "spec": {
                    "version": "1.0",
                    "module": "add",
                    "description": "Performs addition of two numbers.",
                    "objective_coverage": ["Calculator app addition."],
                    "dependencies": [],
                    "constraints": []
                },
                "acceptance": {
                    "version": "1.0",
                    "module": "add",
                    "criteria": [
                        {
                            "id": "A001",
                            "description": "Add function works.",
                            "check": "manual",
                            "status": "pending"
                        }
                    ]
                }
            }
        ],
        "ci_rules": {
            "version": "1.0",
            "rules": []
        },
        "architect_persona": "# Architect Persona"
    }

    mock_val_response = {
        "coverage_gaps": [],
        "contradictions": [],
        "overlaps": [],
        "missing_modules": [],
        "coverage_score": 1.0
    }

    with patch("pcp.llm.client.call_json") as mock_call_json:
        # First call is kickoff generation, second is validate-strategy
        mock_call_json.side_effect = [mock_kickoff_response, mock_val_response]

        runner = CliRunner()
        # Simulate PM approval 'y'
        result = runner.invoke(cli, ["kickoff", str(vision_file), "--path", str(temp_project)], input="y\n")

        assert result.exit_code == 0
        assert "Generated PCP files under" in result.output
        assert "Strategy approved! Transitioned current phase to: alpha." in result.output

        # Check files exist
        assert (temp_project / ".pcp" / "objective.md").exists()
        assert (temp_project / ".pcp" / "strategy" / "modules" / "add" / "spec.yaml").exists()
        assert (temp_project / ".pcp" / "strategy" / "modules" / "add" / "acceptance.yaml").exists()

        # Check SDLC phase updated to alpha
        sdlc = yaml.safe_load((temp_project / ".pcp" / "SDLC_phase.yaml").read_text())
        assert sdlc["current_phase"] == "alpha"
        assert sdlc["phases"][0]["exit_criteria"][0]["status"] == "complete"


def test_kickoff_rejects_oversized_vision_doc(temp_project, monkeypatch):
    monkeypatch.setenv("PCP_KICKOFF_MAX_VISION_CHARS", "100")
    vision_file = temp_project / "vision.md"
    vision_file.write_text("x" * 200)

    runner = CliRunner()
    result = runner.invoke(cli, ["kickoff", str(vision_file), "--path", str(temp_project)])

    assert result.exit_code == 2
    assert "over the 100-char kickoff limit" in result.output


def test_pm_rejects_oversized_project_context(temp_project, monkeypatch):
    monkeypatch.setenv("PCP_PM_MAX_CONTEXT_CHARS", "100")
    pcp_dir = temp_project / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("x" * 200)

    runner = CliRunner()
    result = runner.invoke(cli, ["pm", "some intent", "--path", str(temp_project)])

    assert result.exit_code == 2
    assert "over the 100-char pm limit" in result.output


def test_kickoff_coerces_invalid_check_and_status_values(temp_project):
    """Real bug found dogfooding kickoff against a real, complex vision doc:
    the LLM invented plausible-but-invalid enum values ('automated', 'done')
    that aren't in module_acceptance's schema. Confirms the fix coerces them
    to a safe default and warns, instead of silently writing invalid YAML
    that only surfaces opaquely on the next `pcp scan`."""
    vision_file = temp_project / "vision.md"
    vision_file.write_text("Build a simple calculator app.")

    mock_kickoff_response = {
        "objective": "# Program Objective", "target_state": "# Target State",
        "architecture": "# Architecture", "decomposition": "# Strategy Decomposition",
        "sdlc_phase": {"version": "1.0", "current_phase": "planning", "phases": [
            {"name": "planning", "exit_criteria": [{"id": "E001", "description": "d", "check": "manual", "status": "pending"}]},
            {"name": "alpha", "exit_criteria": [{"id": "E001", "description": "d", "check": "manual", "status": "pending"}]},
        ]},
        "modules": [{
            "name": "add",
            "spec": {"version": "1.0", "module": "add", "description": "Adds numbers.",
                     "objective_coverage": ["addition"], "dependencies": [], "constraints": []},
            "acceptance": {"version": "1.0", "module": "add", "criteria": [
                {"id": "A001", "description": "Add works.", "check": "automated", "status": "done"},
                {"id": "A002", "description": "Subtract works.", "check": "manual", "status": "pending"},
            ]},
        }],
        "ci_rules": {"version": "1.0", "rules": []},
        "architect_persona": "# Architect Persona",
    }
    mock_val_response = {"coverage_gaps": [], "contradictions": [], "overlaps": [], "missing_modules": [], "coverage_score": 1.0}

    with patch("pcp.llm.client.call_json") as mock_call_json:
        mock_call_json.side_effect = [mock_kickoff_response, mock_val_response]
        runner = CliRunner()
        result = runner.invoke(cli, ["kickoff", str(vision_file), "--path", str(temp_project)], input="y\n")

    assert result.exit_code == 0
    assert "didn't match the schema, coerced" in result.output
    assert "check 'automated' is not valid, coerced to 'manual'" in result.output
    assert "status 'done' is not valid, coerced to 'complete'" in result.output

    acc = yaml.safe_load((temp_project / ".pcp" / "strategy" / "modules" / "add" / "acceptance.yaml").read_text())
    assert acc["criteria"][0]["check"] == "manual"
    assert acc["criteria"][0]["status"] == "complete"
    assert acc["criteria"][1]["check"] == "manual"  # untouched, already valid

    # No leftover schema errors after coercion.
    assert "still has schema issues after coercion" not in result.output


def test_normalize_acceptance_returns_no_warnings_when_already_valid():
    from pcp.commands.kickoff import _normalize_acceptance

    acc = {"criteria": [{
        "id": "A001", "check": "manual", "status": "pending",
        "logic_tier": 1,
        "build_vs_buy": {"decision": "build_fresh", "rationale": "trivial, no dependency warranted"},
    }]}
    warnings = _normalize_acceptance(acc, "add")
    assert warnings == []
    assert acc["criteria"][0]["check"] == "manual"


def test_normalize_acceptance_coerces_missing_logic_tier_and_build_vs_buy():
    from pcp.commands.kickoff import _normalize_acceptance

    acc = {"criteria": [{"id": "A001", "check": "manual", "status": "pending"}]}
    warnings = _normalize_acceptance(acc, "add")
    assert any("logic_tier" in w for w in warnings)
    assert any("build_vs_buy" in w for w in warnings)
    assert acc["criteria"][0]["logic_tier"] == 6
    assert acc["criteria"][0]["build_vs_buy"]["decision"] == "build_fresh"


def test_normalize_acceptance_coerces_invalid_build_vs_buy_decision():
    from pcp.commands.kickoff import _normalize_acceptance

    acc = {"criteria": [{
        "id": "A001", "check": "manual", "status": "pending", "logic_tier": 3,
        "build_vs_buy": {"decision": "buy_a_subscription", "rationale": "made up value"},
    }]}
    warnings = _normalize_acceptance(acc, "add")
    assert any("build_vs_buy decision" in w for w in warnings)
    assert acc["criteria"][0]["build_vs_buy"]["decision"] == "build_fresh"


def test_normalize_spec_coerces_missing_module_level_build_vs_buy():
    from pcp.commands.kickoff import _normalize_spec

    spec = {"module": "auth", "description": "Handles authentication."}
    warnings = _normalize_spec(spec, "auth")
    assert warnings
    assert spec["build_vs_buy"]["decision"] == "build_fresh"


def test_normalize_spec_accepts_not_applicable_for_business_logic_module():
    from pcp.commands.kickoff import _normalize_spec

    spec = {"module": "add", "description": "Adds numbers.",
            "build_vs_buy": {"decision": "not_applicable", "rationale": "pure business logic"}}
    warnings = _normalize_spec(spec, "add")
    assert warnings == []
    assert spec["build_vs_buy"]["decision"] == "not_applicable"


# ── _normalize_ci_rules: real bug found live in a kicked-off project (agentberg) ──
# ── -- kickoff wrote result["ci_rules"] with zero validation, unlike acceptance.yaml ──

def test_normalize_ci_rules_returns_no_warnings_when_already_valid():
    from pcp.commands.kickoff import _normalize_ci_rules

    ci_rules = {"version": "1.0", "rules": [
        {"id": "R001", "name": "No secrets", "check": "ast_pattern", "pattern": "x", "severity": "hard_block"},
    ]}
    assert _normalize_ci_rules(ci_rules) == []


def test_normalize_ci_rules_coerces_grep_alias_to_ast_pattern():
    from pcp.commands.kickoff import _normalize_ci_rules

    ci_rules = {"version": "1.0", "rules": [
        {"id": "R001", "name": "n", "check": "grep", "pattern": "x", "severity": "hard_block"},
    ]}
    warnings = _normalize_ci_rules(ci_rules)
    assert any("check 'grep'" in w for w in warnings)
    assert ci_rules["rules"][0]["check"] == "ast_pattern"


def test_normalize_ci_rules_coerces_unmappable_check_to_llm_semantic():
    from pcp.commands.kickoff import _normalize_ci_rules

    ci_rules = {"version": "1.0", "rules": [
        {"id": "R002", "name": "Version lockstep", "check": "file_pair_diff",
         "pattern": "pyproject.toml version must match kit_manifest.json", "severity": "hard_block"},
    ]}
    warnings = _normalize_ci_rules(ci_rules)
    assert any("file_pair_diff" in w for w in warnings)
    assert ci_rules["rules"][0]["check"] == "llm_semantic"
    assert ci_rules["rules"][0]["description"]  # must have something llm_semantic requires


def test_normalize_ci_rules_coerces_warn_severity_to_advisory():
    from pcp.commands.kickoff import _normalize_ci_rules

    ci_rules = {"version": "1.0", "rules": [
        {"id": "R003", "name": "n", "check": "ast_pattern", "pattern": "x", "severity": "warn"},
    ]}
    warnings = _normalize_ci_rules(ci_rules)
    assert any("severity 'warn'" in w for w in warnings)
    assert ci_rules["rules"][0]["severity"] == "advisory"


def test_normalize_ci_rules_coerces_invalid_and_duplicate_ids():
    from pcp.commands.kickoff import _normalize_ci_rules

    ci_rules = {"version": "1.0", "rules": [
        {"id": "not-valid", "name": "a", "check": "ast_pattern", "pattern": "x", "severity": "hard_block"},
        {"id": "not-valid", "name": "b", "check": "ast_pattern", "pattern": "y", "severity": "hard_block"},
    ]}
    warnings = _normalize_ci_rules(ci_rules)
    assert len(warnings) >= 2  # both the invalid pattern and the duplicate get fixed
    ids = [r["id"] for r in ci_rules["rules"]]
    assert len(set(ids)) == 2  # no longer duplicates
    for rid in ids:
        assert re.match(r"^[A-Z]+_?[0-9]+$", rid)


def test_normalize_ci_rules_matches_real_agentberg_bug_exactly():
    """Real, confirmed-live data shape (agentberg's actual generated
    ci_rules.yaml) -- not a synthetic example."""
    from pcp.commands.kickoff import _normalize_ci_rules
    from pcp.schema.validator import validate_file
    import yaml as _yaml

    ci_rules = {
        "version": "1.0",
        "rules": [
            {"check": "ast_pattern", "id": "R001", "name": "No hardcoded secrets",
             "pattern": "(password|secret|api_key)\\s*=\\s*['\"][^'\"]{8,}['\"]", "severity": "hard_block"},
            {"check": "file_pair_diff", "id": "R002", "name": "Kit version and manifest lockstep",
             "pattern": "pyproject.toml version bump requires matching kit_manifest.json version entry",
             "severity": "hard_block"},
            {"check": "ast_pattern", "id": "R003", "name": "No sync analytics in hot path",
             "pattern": "analytics\\.(capture|log)\\(", "severity": "warn"},
            {"check": "grep", "id": "R006", "name": "No test refs in public docs",
             "pattern": "tests/test_api\\.py", "severity": "warn"},
        ],
    }
    warnings = _normalize_ci_rules(ci_rules)
    assert warnings  # real coercions happened

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "ci_rules.yaml"
        p.write_text(_yaml.dump(ci_rules))
        assert validate_file(p, "ci_rules") == []


def test_pm_command(temp_project):
    pcp_dir = temp_project / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("# Objective\nCalc app.")
    (pcp_dir / "strategy").mkdir()
    (pcp_dir / "strategy" / "decomposition.md").write_text("# Decomp")

    # Add existing module
    mod_dir = pcp_dir / "strategy" / "modules" / "add"
    mod_dir.mkdir(parents=True)
    existing_spec = {
        "version": "1.0",
        "module": "add",
        "description": "Performs addition of two numbers.",
        "objective_coverage": ["Addition"],
        "dependencies": [],
        "constraints": []
    }
    (mod_dir / "spec.yaml").write_text(yaml.dump(existing_spec))
    existing_acc = {
        "version": "1.0",
        "module": "add",
        "criteria": [
            {
                "id": "A001",
                "description": "Basic add works",
                "check": "manual",
                "status": "complete"
            }
        ]
    }
    (mod_dir / "acceptance.yaml").write_text(yaml.dump(existing_acc))

    mock_pm_response = {
        "module_action": "modify",
        "module_name": "add",
        "explanation": "Adding support for multiple arguments.",
        "spec_changes": {
            "version": "1.0",
            "module": "add",
            "description": "Performs addition of multiple numbers.",
            "objective_coverage": ["Addition"],
            "dependencies": [],
            "constraints": []
        },
        "acceptance_changes": {
            "version": "1.0",
            "module": "add",
            "criteria": [
                {
                    "id": "A002",
                    "description": "Supports adding list of numbers.",
                    "check": "manual",
                    "status": "pending"
                }
            ]
        }
    }

    with patch("pcp.llm.client.call_json") as mock_call_json, \
            patch("pcp.commands.scan.scan") as mock_scan:
        mock_call_json.return_value = mock_pm_response

        runner = CliRunner()
        result = runner.invoke(cli, ["pm", "Support multiple arguments in add module", "--path", str(temp_project)], input="y\n")

        assert result.exit_code == 0
        assert "Planned Action: MODIFY module 'add'" in result.output
        assert "Module 'add' spec and acceptance criteria updated." in result.output

        # Verify spec updated
        spec = yaml.safe_load((mod_dir / "spec.yaml").read_text())
        assert "multiple numbers" in spec["description"]

        # Verify criteria merged
        acc = yaml.safe_load((mod_dir / "acceptance.yaml").read_text())
        assert len(acc["criteria"]) == 2
        assert acc["criteria"][0]["id"] == "A001"
        assert acc["criteria"][0]["status"] == "complete"
        assert acc["criteria"][1]["id"] == "A002"
        assert acc["criteria"][1]["status"] == "pending"

        # pm must force version 2.0 on both files -- never leave/revert a
        # module to the ungated 1.0 shape -- and coerce logic_tier/
        # build_vs_buy for both the pre-existing and the newly-added criterion
        # since the mock LLM response above didn't supply either.
        spec = yaml.safe_load((mod_dir / "spec.yaml").read_text())
        assert spec["version"] == "2.0"
        assert acc["version"] == "2.0"
        assert acc["criteria"][0]["logic_tier"] == 6
        assert acc["criteria"][1]["build_vs_buy"]["decision"] == "build_fresh"
        assert "didn't match the schema, coerced" in result.output


def test_pm_preserves_existing_module_level_build_vs_buy_on_modify(temp_project):
    """A real prior module-level build_vs_buy decision (e.g. from kickoff)
    must not be silently discarded just because this pm call's LLM response
    omitted it."""
    pcp_dir = temp_project / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("# Objective\nAuth service.")
    (pcp_dir / "strategy").mkdir()
    (pcp_dir / "strategy" / "decomposition.md").write_text("# Decomp")

    mod_dir = pcp_dir / "strategy" / "modules" / "auth"
    mod_dir.mkdir(parents=True)
    (mod_dir / "spec.yaml").write_text(yaml.dump({
        "version": "2.0", "module": "auth", "description": "Handles authentication.",
        "objective_coverage": ["Auth"], "dependencies": [], "constraints": [],
        "build_vs_buy": {"decision": "reuse_whole", "rationale": "Auth0 fits cleanly", "candidates_considered": ["Auth0", "Clerk"]},
    }))
    (mod_dir / "acceptance.yaml").write_text(yaml.dump({
        "version": "2.0", "module": "auth", "criteria": [],
    }))

    mock_pm_response = {
        "module_action": "modify",
        "module_name": "auth",
        "explanation": "Add password reset flow.",
        "spec_changes": {
            "version": "2.0", "module": "auth", "description": "Handles authentication and password reset.",
            "objective_coverage": ["Auth"], "dependencies": [], "constraints": [],
            # deliberately omits build_vs_buy -- simulates an LLM response that dropped it
        },
        "acceptance_changes": {
            "version": "2.0", "module": "auth", "criteria": [
                {"id": "A001", "description": "Password reset works.", "check": "manual", "status": "pending",
                 "logic_tier": 1, "build_vs_buy": {"decision": "build_fresh", "rationale": "trivial flow"}},
            ],
        },
    }

    with patch("pcp.llm.client.call_json") as mock_call_json, \
            patch("pcp.commands.scan.scan"):
        mock_call_json.return_value = mock_pm_response
        runner = CliRunner()
        result = runner.invoke(cli, ["pm", "Add password reset", "--path", str(temp_project)], input="y\n")

    assert result.exit_code == 0
    spec = yaml.safe_load((mod_dir / "spec.yaml").read_text())
    assert spec["build_vs_buy"]["decision"] == "reuse_whole"
    assert spec["build_vs_buy"]["rationale"] == "Auth0 fits cleanly"


def test_build_command(temp_project):
    pcp_dir = temp_project / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("# Objective")

    mod_dir = pcp_dir / "strategy" / "modules" / "add"
    mod_dir.mkdir(parents=True)
    spec = {
        "version": "1.0",
        "module": "add",
        "description": "Addition module",
        "objective_coverage": ["Addition"]
    }
    (mod_dir / "spec.yaml").write_text(yaml.dump(spec))

    acc = {
        "version": "1.0",
        "module": "add",
        "criteria": [
            {
                "id": "A001",
                "description": "Core function",
                "check": "manual",
                "status": "pending"
            }
        ]
    }
    (mod_dir / "acceptance.yaml").write_text(yaml.dump(acc))

    # Mock git status and changes. check_environment is mocked too -- this
    # test doesn't exercise the real environment preflight, and CI runners
    # genuinely lack a `claude` binary on PATH (found 2026-07-18: this test
    # only ever passed locally, where a real `claude` happens to be
    # installed -- see doctor.py's _claude_bin_for_detection docstring).
    with patch("subprocess.run") as mock_run, \
            patch("pcp.commands.doctor.check_environment", return_value={}), \
            patch("pcp.commands.build._get_staged_files") as mock_staged, \
            patch("pcp.commands.build._get_unstaged_files") as mock_unstaged, \
            patch("pcp.commands.build._get_working_diff") as mock_diff, \
            patch("pcp.commands.build._run_layer1_check") as mock_l1, \
            patch("pcp.commands.build._run_architect_review") as mock_arch, \
            patch("pcp.commands.build._run_gate_check") as mock_gate:

        # Agent ran successfully
        agent_proc = MagicMock()
        agent_proc.returncode = 0
        mock_run.return_value = agent_proc

        mock_staged.return_value = ["add.py"]
        mock_unstaged.return_value = []
        mock_diff.return_value = "diff content"

        # Gates pass
        mock_l1.return_value = []
        mock_arch.return_value = []
        mock_gate.return_value = []

        runner = CliRunner()
        result = runner.invoke(cli, ["build", "--path", str(temp_project)])

        assert result.exit_code == 0
        assert "Building Module: 'add'" in result.output
        assert "passed all gates successfully!" in result.output

        # Verify acceptance updated to complete
        updated_acc = yaml.safe_load((mod_dir / "acceptance.yaml").read_text())
        assert updated_acc["criteria"][0]["status"] == "complete"


def test_status_pm_command(temp_project):
    pcp_dir = temp_project / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("# Objective\nCalc app.")
    (pcp_dir / "current_state.md").write_text("- [ ] ADD/A001: Add function works.")
    (pcp_dir / "SDLC_phase.yaml").write_text("current_phase: alpha")

    with patch("pcp.llm.client.call") as mock_call, \
            patch("pcp.commands.status._get_recent_commits") as mock_commits:
        mock_call.return_value = "### PM Status Report\n\nPhase: alpha\nEverything looks good."
        mock_commits.return_value = "commit1\ncommit2"

        runner = CliRunner()
        result = runner.invoke(cli, ["status", "--pm", "--path", str(temp_project)])

        assert result.exit_code == 0
        assert "Generating plain-English PM status report..." in result.output
        assert "### PM Status Report" in result.output
