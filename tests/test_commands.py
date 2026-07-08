import sys
import json
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

    acc = {"criteria": [{"id": "A001", "check": "manual", "status": "pending"}]}
    warnings = _normalize_acceptance(acc, "add")
    assert warnings == []
    assert acc["criteria"][0]["check"] == "manual"


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

    # Mock git status and changes
    with patch("subprocess.run") as mock_run, \
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
