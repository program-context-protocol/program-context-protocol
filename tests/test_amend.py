"""pcp amend + spec_write — the human-authorized write path for every protected
.pcp/ file that previously had none (2026-07-25).

Covers the propose/diff/approve/write mechanic, the governance-file guardrails
(schema validation + weakening refusal), and the target-resolution table.
"""

from unittest.mock import patch

import yaml
from click.testing import CliRunner

from pcp.cli import cli
from pcp.commands.amend import resolve_target_key
from pcp.spec_write import detect_weakening, render_diff


def _project(tmp_path, **files):
    pcp = tmp_path / ".pcp"
    (pcp / "strategy" / "modules").mkdir(parents=True)
    (pcp / "objective.md").write_text("# Objective\n\nShip a thing.\n")
    for rel, content in files.items():
        path = pcp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return pcp


CI_RULES = """\
version: "1.0"
rules:
  - id: R001
    name: "no eval"
    check: ast_pattern
    pattern: "eval\\\\("
    severity: hard_block
  - id: R002
    name: "advisory thing"
    check: llm_semantic
    description: "be nice"
    severity: advisory
"""


# ── target resolution ──────────────────────────────────────────────────────

def test_resolve_target_key_accepts_short_names_and_paths():
    assert resolve_target_key("architecture") == "architecture"
    assert resolve_target_key("architecture.md") == "architecture"
    assert resolve_target_key(".pcp/strategy/decomposition.md") == "decomposition"
    assert resolve_target_key("dependency-map") == "dependency_map"
    assert resolve_target_key("SDLC_phase.yaml") == "sdlc_phase"
    assert resolve_target_key("ci_rules.yaml") == "ci_rules"


def test_resolve_target_key_rejects_unknown():
    assert resolve_target_key("current_state.md") is None
    assert resolve_target_key("nonsense") is None


def test_amend_redirects_objective_to_correct_objective(tmp_path):
    _project(tmp_path)
    result = CliRunner().invoke(cli, ["amend", "objective.md", "x", "--path", str(tmp_path)])
    assert result.exit_code == 2
    assert "correct-objective" in result.output


def test_amend_redirects_module_spec_to_pm(tmp_path):
    _project(tmp_path)
    result = CliRunner().invoke(
        cli, ["amend", "strategy/modules/foo/spec.yaml", "x", "--path", str(tmp_path)]
    )
    assert result.exit_code == 2
    assert "pcp pm" in result.output


# ── the core write path ────────────────────────────────────────────────────

def test_amend_writes_architecture_on_approval(tmp_path):
    pcp = _project(tmp_path, **{"architecture.md": "# Architecture\n\nPostgres.\n"})
    proposal = {"content": "# Architecture\n\nPostgres.\nRedis for the cache.\n", "summary": "added redis"}
    with patch("pcp.llm.client.call_json", return_value=proposal):
        result = CliRunner().invoke(
            cli, ["amend", "architecture", "Use Redis for the cache", "--path", str(tmp_path)],
            input="y\n",
        )
    assert result.exit_code == 0, result.output
    assert "Redis for the cache" in (pcp / "architecture.md").read_text()


def test_amend_aborts_without_approval_leaves_file_unchanged(tmp_path):
    pcp = _project(tmp_path, **{"architecture.md": "# Architecture\n\nPostgres.\n"})
    proposal = {"content": "# Architecture\n\nMongoDB.\n", "summary": "swap db"}
    with patch("pcp.llm.client.call_json", return_value=proposal):
        result = CliRunner().invoke(
            cli, ["amend", "architecture", "Swap to Mongo", "--path", str(tmp_path)],
            input="n\n",
        )
    assert result.exit_code == 0
    assert (pcp / "architecture.md").read_text() == "# Architecture\n\nPostgres.\n"
    assert "MongoDB" not in (pcp / "architecture.md").read_text()


def test_amend_records_a_decision_log_entry(tmp_path):
    pcp = _project(tmp_path, **{"architecture.md": "# Architecture\n\nPostgres.\n"})
    proposal = {"content": "# Architecture\n\nPostgres.\nRedis.\n", "summary": "added redis"}
    with patch("pcp.llm.client.call_json", return_value=proposal):
        CliRunner().invoke(
            cli, ["amend", "architecture", "Use Redis", "--path", str(tmp_path)], input="y\n"
        )
    log = (pcp / "decision_log.jsonl").read_text()
    assert "architecture.md amended" in log
    assert "Use Redis" in log


def test_amend_no_change_is_not_an_error(tmp_path):
    _project(tmp_path, **{"architecture.md": "# Architecture\n\nPostgres.\n"})
    proposal = {"content": "# Architecture\n\nPostgres.\n", "summary": "nothing"}
    with patch("pcp.llm.client.call_json", return_value=proposal):
        result = CliRunner().invoke(
            cli, ["amend", "architecture", "no-op", "--path", str(tmp_path)]
        )
    assert result.exit_code == 0
    assert "No changes" in result.output


def test_amend_exits_2_when_llm_omits_content(tmp_path):
    _project(tmp_path, **{"architecture.md": "# Architecture\n"})
    with patch("pcp.llm.client.call_json", return_value={"summary": "oops"}):
        result = CliRunner().invoke(
            cli, ["amend", "architecture", "x", "--path", str(tmp_path)]
        )
    assert result.exit_code == 2


def test_amend_decomposition_reruns_validate_strategy(tmp_path):
    _project(tmp_path, **{"strategy/decomposition.md": "# Decomposition\n\n- auth\n"})
    proposal = {"content": "# Decomposition\n\n- auth\n- billing\n", "summary": "added billing"}
    val = {"coverage_score": 0.9, "gaps": [], "overlaps": [], "missing_modules": [], "verdict": "PASS"}
    calls = []

    def fake(system, user, **kw):
        calls.append(kw.get("command"))
        return val if kw.get("command") == "amend-validate" else proposal

    with patch("pcp.llm.client.call_json", side_effect=fake):
        result = CliRunner().invoke(
            cli, ["amend", "decomposition", "Add a billing module", "--path", str(tmp_path)],
            input="y\n",
        )
    assert result.exit_code == 0, result.output
    assert "amend-validate" in calls


# ── governance guardrails ──────────────────────────────────────────────────

def test_amend_ci_rules_refuses_rule_removal_without_flag(tmp_path):
    pcp = _project(tmp_path, **{"ci_rules.yaml": CI_RULES})
    weakened = CI_RULES.replace(
        '  - id: R001\n    name: "no eval"\n    check: ast_pattern\n    pattern: "eval\\\\("\n    severity: hard_block\n', ""
    )
    with patch("pcp.llm.client.call_json", return_value={"content": weakened, "summary": "drop R001"}):
        result = CliRunner().invoke(
            cli, ["amend", "ci_rules", "Drop the eval rule", "--path", str(tmp_path)], input="y\n"
        )
    assert result.exit_code == 2
    assert "weakens the gates" in result.output
    assert "R001 removed" in result.output
    assert (pcp / "ci_rules.yaml").read_text() == CI_RULES  # untouched


def test_amend_ci_rules_allows_removal_with_flag_and_logs_it(tmp_path):
    pcp = _project(tmp_path, **{"ci_rules.yaml": CI_RULES})
    weakened = CI_RULES.replace(
        '  - id: R001\n    name: "no eval"\n    check: ast_pattern\n    pattern: "eval\\\\("\n    severity: hard_block\n', ""
    )
    with patch("pcp.llm.client.call_json", return_value={"content": weakened, "summary": "drop R001"}):
        result = CliRunner().invoke(
            cli, ["amend", "ci_rules", "Drop the eval rule", "--allow-weakening", "--path", str(tmp_path)],
            input="y\n",
        )
    assert result.exit_code == 0, result.output
    assert "R001" not in (pcp / "ci_rules.yaml").read_text()
    assert "GATE WEAKENING" in (pcp / "decision_log.jsonl").read_text()


def test_amend_ci_rules_rejects_schema_violation_before_asking(tmp_path):
    pcp = _project(tmp_path, **{"ci_rules.yaml": CI_RULES})
    invalid = 'version: "1.0"\nrules:\n  - name: "missing id and check"\n'
    with patch("pcp.llm.client.call_json", return_value={"content": invalid, "summary": "broke it"}):
        result = CliRunner().invoke(
            cli, ["amend", "ci_rules", "restructure", "--path", str(tmp_path)], input="y\n"
        )
    assert result.exit_code == 2
    assert "fails its schema" in result.output
    assert (pcp / "ci_rules.yaml").read_text() == CI_RULES


def test_amend_ci_rules_allows_a_non_weakening_addition(tmp_path):
    pcp = _project(tmp_path, **{"ci_rules.yaml": CI_RULES})
    added = CI_RULES + (
        '  - id: R003\n    name: "no print"\n    check: ast_pattern\n'
        '    pattern: "print\\\\("\n    severity: advisory\n'
    )
    with patch("pcp.llm.client.call_json", return_value={"content": added, "summary": "added R003"}):
        result = CliRunner().invoke(
            cli, ["amend", "ci_rules", "Add a no-print advisory rule", "--path", str(tmp_path)],
            input="y\n",
        )
    assert result.exit_code == 0, result.output
    assert "R003" in (pcp / "ci_rules.yaml").read_text()


# ── weakening detector, direct ─────────────────────────────────────────────

def test_detect_weakening_flags_severity_downgrade():
    old = 'version: "1.0"\nrules:\n  - id: R001\n    severity: hard_block\n'
    new = 'version: "1.0"\nrules:\n  - id: R001\n    severity: advisory\n'
    assert detect_weakening(old, new) == ["rules: R001 severity hard_block -> advisory (downgrade)"]


def test_detect_weakening_ignores_severity_upgrade():
    old = 'version: "1.0"\nrules:\n  - id: R001\n    severity: advisory\n'
    new = 'version: "1.0"\nrules:\n  - id: R001\n    severity: hard_block\n'
    assert detect_weakening(old, new) == []


def test_detect_weakening_flags_dropped_control():
    old = 'version: "1.0"\ncontrols:\n  - id: CTRL-001\n  - id: CTRL-002\n'
    new = 'version: "1.0"\ncontrols:\n  - id: CTRL-001\n'
    assert detect_weakening(old, new) == ["controls: CTRL-002 removed"]


def test_detect_weakening_flags_dropped_exit_criterion():
    old = (
        'version: "1.0"\ncurrent_phase: alpha\nphases:\n  - name: alpha\n'
        "    exit_criteria:\n      - id: E001\n      - id: E002\n"
    )
    new = (
        'version: "1.0"\ncurrent_phase: alpha\nphases:\n  - name: alpha\n'
        "    exit_criteria:\n      - id: E001\n"
    )
    assert detect_weakening(old, new) == ["phases.alpha.exit_criteria: E002 removed"]


def test_detect_weakening_flags_dropped_phase():
    old = 'version: "1.0"\ncurrent_phase: alpha\nphases:\n  - name: alpha\n  - name: beta\n'
    new = 'version: "1.0"\ncurrent_phase: alpha\nphases:\n  - name: alpha\n'
    assert detect_weakening(old, new) == ["phases: beta removed"]


def test_detect_weakening_tolerates_unparseable_old():
    assert detect_weakening("::: not yaml :::\n\t- x", 'version: "1.0"\nrules: []\n') == []


def test_render_diff_is_empty_for_identical_content():
    assert render_diff("a\nb\n", "a\nb\n", "x.md") == ""


# ── the protected_path scope pcp init now scaffolds ────────────────────────

def test_init_scaffolds_a_protected_path_rule_covering_every_amendable_file(tmp_path):
    result = CliRunner().invoke(cli, ["init", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    rules = yaml.safe_load((tmp_path / ".pcp" / "ci_rules.yaml").read_text())["rules"]
    protected = [r for r in rules if r.get("check") == "protected_path"]
    assert protected, "pcp init must scaffold a protected_path rule"
    scope = protected[0]["scope"]
    for expected in [
        ".pcp/objective.md", ".pcp/target_state.md", ".pcp/architecture.md",
        ".pcp/ci_rules.yaml", ".pcp/controls.yaml", ".pcp/SDLC_phase.yaml",
        ".pcp/strategy/decomposition.md", ".pcp/strategy/dependency_map.md",
        ".pcp/strategy/modules/*/spec.yaml", ".pcp/strategy/modules/*/acceptance.yaml",
    ]:
        assert expected in scope, f"{expected} missing from scaffolded protected_path scope"
