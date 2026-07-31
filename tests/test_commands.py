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


def test_pm_context_projects_out_fields_it_never_reads():
    """`pcp pm` pasted every module's acceptance.yaml verbatim, so a healthy
    27-module project assembled 396k chars and hit the guard -- pm was dead on
    4 of 8 local PCP-managed projects. The fix is a by-name field projection,
    not a truncation: existing criteria's build_vs_buy/design_justification/QA
    evidence go, ids/descriptions/scheduling fields stay."""
    from pcp.commands.pm import _slim_acceptance

    raw = yaml.dump({
        "version": "2.0", "module": "billing",
        "criteria": [{
            "id": "A001", "description": "keep me", "check": "manual",
            "status": "complete", "logic_tier": 1, "depends_on": [],
            "target": "src/billing/charge.py", "pattern": "def charge",
            "build_vs_buy": {"decision": "build_fresh", "rationale": "z" * 500,
                             "candidates_considered": []},
            "design_justification": {"jtbd_framing": "y" * 500,
                                     "checklist_passed": ["both-themes"]},
            "test": "t" * 500, "notes": "n" * 500, "verified_by": "v" * 100,
        }],
    })
    slim = _slim_acceptance(raw)
    parsed = yaml.safe_load(slim)
    c = parsed["criteria"][0]

    assert len(slim) < len(raw) / 3
    assert parsed["module"] == "billing"
    # every field pm actually uses survives, untruncated
    assert c["id"] == "A001" and c["description"] == "keep me"
    assert c["check"] == "manual" and c["status"] == "complete"
    assert c["logic_tier"] == 1 and c["depends_on"] == []
    assert c["target"] == "src/billing/charge.py" and c["pattern"] == "def charge"
    # and only those
    for gone in ("build_vs_buy", "design_justification", "test", "notes", "verified_by"):
        assert gone not in c


def test_pm_context_projection_fails_open_on_unparseable_acceptance():
    """A mangled spec handed to the LLM is worse than a large one."""
    from pcp.commands.pm import _slim_acceptance

    assert _slim_acceptance("criteria: [unclosed") == "criteria: [unclosed"
    assert _slim_acceptance("criteria: not-a-list\n") == "criteria: not-a-list\n"


def test_pm_reports_how_much_the_projection_dropped(temp_project):
    """The projection must be visible. The whole reason the old code pasted
    everything was a refusal to cut silently; an invisible projection would
    repeat that mistake in reverse."""
    pcp_dir = temp_project / ".pcp"
    mod = pcp_dir / "strategy" / "modules" / "billing"
    mod.mkdir(parents=True)
    (pcp_dir / "objective.md").write_text("# Objective")
    (mod / "spec.yaml").write_text(yaml.dump({"version": "2.0", "module": "billing"}))
    (mod / "acceptance.yaml").write_text(yaml.dump({
        "version": "2.0", "module": "billing",
        "criteria": [{"id": "A001", "description": "d", "check": "manual",
                      "status": "pending", "notes": "q" * 5000}],
    }))

    runner = CliRunner()
    # limit set below the *projected* size so it still exits on the guard,
    # after having printed the projection line
    with patch("pcp.commands.pm.llm.call_json") as mock_llm:
        mock_llm.side_effect = AssertionError("must not reach the LLM")
        result = runner.invoke(cli, ["pm", "add refunds", "--path", str(temp_project)],
                               env={"PCP_PM_MAX_CONTEXT_CHARS": "50"})

    assert "Context projection:" in result.output
    assert "chars of existing criteria" in result.output
    assert result.exit_code == 2


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
        "depends_on": [],
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


def test_normalize_acceptance_defaults_missing_depends_on_to_independent():
    """2026-07-20 parallelism fix: depends_on missing entirely (generator
    never supplied it) defaults to [] (independent) -- the safe direction
    to err in, since a false-independence guess costs a merge check while a
    false-dependency guess costs real, silently-lost parallelism."""
    from pcp.commands.kickoff import _normalize_acceptance

    acc = {"criteria": [{
        "id": "A001", "check": "manual", "status": "pending", "logic_tier": 1,
        "build_vs_buy": {"decision": "build_fresh", "rationale": "trivial"},
    }]}
    warnings = _normalize_acceptance(acc, "add")
    assert any("depends_on" in w for w in warnings)
    assert acc["criteria"][0]["depends_on"] == []


def test_normalize_acceptance_never_overrides_a_real_depends_on_value():
    """A genuinely declared dependency (even referencing another criterion)
    must never be silently replaced by the missing-key safety net."""
    from pcp.commands.kickoff import _normalize_acceptance

    acc = {"criteria": [{
        "id": "A002", "check": "manual", "status": "pending", "logic_tier": 1,
        "build_vs_buy": {"decision": "build_fresh", "rationale": "trivial"},
        "depends_on": ["A001"],
    }]}
    warnings = _normalize_acceptance(acc, "add")
    assert not any("depends_on" in w for w in warnings)
    assert acc["criteria"][0]["depends_on"] == ["A001"]
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


# ── _normalize_ci_rules: real bug found live in a kicked-off project (Project A) ──
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


def test_normalize_ci_rules_matches_real_project_a_bug_exactly():
    """Real, confirmed-live data shape (Project A's actual generated
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
        "capabilities_enumerated": ["support multiple arguments in add"],
        "overall_explanation": "Adding support for multiple arguments.",
        "modules": [
            {
                "module_action": "modify",
                "module_name": "add",
                "module_explanation": "Adding support for multiple arguments.",
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
        ],
    }
    mock_val_response = {
        "coverage_gaps": [], "contradictions": [], "overlaps": [],
        "missing_modules": [], "coverage_score": 1.0,
    }

    with patch("pcp.llm.client.call_json") as mock_call_json, \
            patch("pcp.commands.scan.scan") as mock_scan:
        mock_call_json.side_effect = [mock_pm_response, mock_val_response]

        runner = CliRunner()
        result = runner.invoke(cli, ["pm", "Support multiple arguments in add module", "--path", str(temp_project)], input="y\n")

        assert result.exit_code == 0
        assert "Intent spans 1 module(s)." in result.output
        assert "MODIFY module" in result.output and "'add'" in result.output
        assert "1 module(s) updated." in result.output

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
        "capabilities_enumerated": ["password reset flow"],
        "overall_explanation": "Add password reset flow.",
        "modules": [
            {
                "module_action": "modify",
                "module_name": "auth",
                "module_explanation": "Add password reset flow.",
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
        ],
    }
    mock_val_response = {
        "coverage_gaps": [], "contradictions": [], "overlaps": [],
        "missing_modules": [], "coverage_score": 1.0,
    }

    with patch("pcp.llm.client.call_json") as mock_call_json, \
            patch("pcp.commands.scan.scan"):
        mock_call_json.side_effect = [mock_pm_response, mock_val_response]
        runner = CliRunner()
        result = runner.invoke(cli, ["pm", "Add password reset", "--path", str(temp_project)], input="y\n")

    assert result.exit_code == 0
    spec = yaml.safe_load((mod_dir / "spec.yaml").read_text())
    assert spec["build_vs_buy"]["decision"] == "reuse_whole"
    assert spec["build_vs_buy"]["rationale"] == "Auth0 fits cleanly"


def test_pm_preserves_existing_module_logic_breakdown_on_modify(temp_project):
    """Same preservation rule as module-level build_vs_buy: the prompt tells
    the LLM to OMIT module_logic_breakdown for a small, same-shape addition,
    so an omitted key must mean 'unchanged', not 'delete the prior breakdown'."""
    pcp_dir = temp_project / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("# Objective\nAuth service.")
    (pcp_dir / "strategy").mkdir()
    (pcp_dir / "strategy" / "decomposition.md").write_text("# Decomp")

    mod_dir = pcp_dir / "strategy" / "modules" / "auth"
    mod_dir.mkdir(parents=True)
    (mod_dir / "spec.yaml").write_text(yaml.dump({
        "version": "2.0", "module": "auth", "description": "Handles authentication.",
        "objective_coverage": ["Auth"],
        "module_logic_breakdown": ["session token issuance", "password hashing and verification"],
        "dependencies": [], "constraints": [],
        "build_vs_buy": {"decision": "reuse_whole", "rationale": "Auth0 fits cleanly", "candidates_considered": []},
    }))
    (mod_dir / "acceptance.yaml").write_text(yaml.dump({"version": "2.0", "module": "auth", "criteria": []}))

    mock_pm_response = {
        "capabilities_enumerated": ["minor UI copy tweak"],
        "overall_explanation": "Tweak login button text.",
        "modules": [
            {
                "module_action": "modify",
                "module_name": "auth",
                "module_explanation": "Tweak login button text.",
                "spec_changes": {
                    "version": "2.0", "module": "auth", "description": "Handles authentication and login copy.",
                    "objective_coverage": ["Auth"], "dependencies": [], "constraints": [],
                    "build_vs_buy": {"decision": "reuse_whole", "rationale": "Auth0 fits cleanly", "candidates_considered": []},
                    # deliberately omits module_logic_breakdown -- small same-shape addition
                },
                "acceptance_changes": {
                    "version": "2.0", "module": "auth", "criteria": [
                        {"id": "A001", "description": "Login button says Sign In.", "check": "manual", "status": "pending",
                         "logic_tier": 1, "build_vs_buy": {"decision": "build_fresh", "rationale": "trivial copy"}},
                    ],
                },
            }
        ],
    }
    mock_val_response = {
        "coverage_gaps": [], "contradictions": [], "overlaps": [],
        "missing_modules": [], "coverage_score": 1.0,
    }

    with patch("pcp.llm.client.call_json") as mock_call_json, \
            patch("pcp.commands.scan.scan"):
        mock_call_json.side_effect = [mock_pm_response, mock_val_response]
        runner = CliRunner()
        result = runner.invoke(cli, ["pm", "Tweak login copy", "--path", str(temp_project)], input="y\n")

    assert result.exit_code == 0
    spec = yaml.safe_load((mod_dir / "spec.yaml").read_text())
    assert spec["module_logic_breakdown"] == ["session token issuance", "password hashing and verification"]


def test_pm_spans_multiple_modules_without_truncation(temp_project):
    """The real root-cause fix, 2026-07-20: pm's output used to be a single
    module_action/module_name -- a feature intent spanning 2+ modules got
    silently truncated to whichever one the LLM picked. modules is now a
    list; this confirms an intent touching two modules writes both."""
    pcp_dir = temp_project / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("# Objective\nCommerce platform.")
    (pcp_dir / "strategy").mkdir()
    (pcp_dir / "strategy" / "decomposition.md").write_text("# Decomp")

    mock_pm_response = {
        "capabilities_enumerated": ["charge card on checkout", "email receipt after payment"],
        "overall_explanation": "Add payments end to end.",
        "modules": [
            {
                "module_action": "create",
                "module_name": "billing",
                "module_explanation": "Handles the actual charge.",
                "spec_changes": {
                    "version": "2.0", "module": "billing", "description": "Charges a card via the payment processor.",
                    "objective_coverage": ["Commerce checkout"], "dependencies": [], "constraints": [],
                },
                "acceptance_changes": {
                    "version": "2.0", "module": "billing", "criteria": [
                        {"id": "A001", "description": "Card is charged on checkout.", "check": "manual", "status": "pending",
                         "logic_tier": 1, "build_vs_buy": {"decision": "reuse_whole", "rationale": "Stripe"}},
                    ],
                },
            },
            {
                "module_action": "create",
                "module_name": "notifications",
                "module_explanation": "Sends the receipt.",
                "spec_changes": {
                    "version": "2.0", "module": "notifications", "description": "Sends transactional email receipts.",
                    "objective_coverage": ["Commerce checkout"], "dependencies": ["billing"], "constraints": [],
                },
                "acceptance_changes": {
                    "version": "2.0", "module": "notifications", "criteria": [
                        {"id": "A001", "description": "Email receipt sent after payment.", "check": "manual", "status": "pending",
                         "logic_tier": 1, "build_vs_buy": {"decision": "reuse_whole", "rationale": "SendGrid"}},
                    ],
                },
            },
        ],
    }
    mock_val_response = {
        "coverage_gaps": [], "contradictions": [], "overlaps": [],
        "missing_modules": [], "coverage_score": 1.0,
    }

    with patch("pcp.llm.client.call_json") as mock_call_json, \
            patch("pcp.commands.scan.scan"):
        mock_call_json.side_effect = [mock_pm_response, mock_val_response]
        runner = CliRunner()
        result = runner.invoke(cli, ["pm", "Add payment processing with email receipts", "--path", str(temp_project)], input="y\n")

    assert result.exit_code == 0
    assert "Intent spans 2 module(s)." in result.output
    billing_acc = yaml.safe_load((pcp_dir / "strategy" / "modules" / "billing" / "acceptance.yaml").read_text())
    notif_acc = yaml.safe_load((pcp_dir / "strategy" / "modules" / "notifications" / "acceptance.yaml").read_text())
    assert billing_acc["criteria"][0]["description"] == "Card is charged on checkout."
    assert notif_acc["criteria"][0]["description"] == "Email receipt sent after payment."


def test_pm_runs_validate_strategy_and_flags_capability_gap(temp_project):
    """The other real root-cause fix: pm previously had ZERO strategy
    verification at all (unlike kickoff, which always ran validate-strategy).
    Confirms both the deterministic capability cross-check and the
    LLM-judged validate-strategy call actually fire."""
    pcp_dir = temp_project / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("# Objective\nCommerce platform.")
    (pcp_dir / "strategy").mkdir()
    (pcp_dir / "strategy" / "decomposition.md").write_text("# Decomp")

    mock_pm_response = {
        "capabilities_enumerated": ["completely unrelated inventory sync capability"],
        "overall_explanation": "Add billing.",
        "modules": [
            {
                "module_action": "create",
                "module_name": "billing",
                "module_explanation": "Handles charges.",
                "spec_changes": {
                    "version": "2.0", "module": "billing", "description": "Charges a card via the payment processor.",
                    "objective_coverage": ["Commerce checkout"], "dependencies": [], "constraints": [],
                },
                "acceptance_changes": {
                    "version": "2.0", "module": "billing", "criteria": [
                        {"id": "A001", "description": "Card is charged.", "check": "manual", "status": "pending",
                         "logic_tier": 1, "build_vs_buy": {"decision": "reuse_whole", "rationale": "Stripe"}},
                    ],
                },
            },
        ],
    }
    mock_val_response = {
        "coverage_gaps": [{"area": "inventory sync not covered"}], "contradictions": [], "overlaps": [],
        "missing_modules": [], "coverage_score": 0.5,
    }

    with patch("pcp.llm.client.call_json") as mock_call_json, \
            patch("pcp.commands.scan.scan"):
        mock_call_json.side_effect = [mock_pm_response, mock_val_response]
        runner = CliRunner()
        result = runner.invoke(cli, ["pm", "Add payment processing", "--path", str(temp_project)], input="y\n")

    assert result.exit_code == 0
    # Deterministic capability cross-check fired (keyword overlap miss).
    assert "may not be covered by any module" in result.output
    assert "inventory sync" in result.output
    # LLM-judged validate-strategy actually ran, not skipped.
    assert mock_call_json.call_count == 2
    assert "Running validate-strategy" in result.output


def test_pm_system_prompt_has_decompose_first_and_multi_module_shape():
    """Sanity check the actual prompt text, not just behavior -- the
    DECOMPOSE FIRST instruction and the modules-list shape are what make
    the fix real rather than incidental to the mocked test data above."""
    from pcp.commands.pm import SYSTEM_PROMPT
    assert "DECOMPOSE FIRST" in SYSTEM_PROMPT
    assert "capabilities_enumerated" in SYSTEM_PROMPT
    assert '"modules": [' in SYSTEM_PROMPT
    assert "more than one existing or new module" in SYSTEM_PROMPT.lower()


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
