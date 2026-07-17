import json
from unittest.mock import patch, MagicMock

import yaml

from pcp.commands.build import _build_one_criterion, _BuildBudget
from pcp.llm import client as llm


def _envelope(session_id="s1"):
    return json.dumps({
        "is_error": False, "result": "done", "session_id": session_id,
        "usage": {}, "total_cost_usd": 0.0, "duration_ms": 1,
    })


def _run_build_one_criterion(tmp_path, build_model, build_model_explicit, test_suite_results):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    acc_path = pcp_dir / "acc.yaml"
    acc_path.write_text(yaml.dump({"criteria": [{"id": "A001", "description": "impl thing"}]}))
    mod = {"name": "widgets", "spec": {}, "acc_path": acc_path}
    c = {"id": "A001", "description": "impl thing"}
    budget = _BuildBudget(max_sessions=10)

    captured_models = []

    def fake_run(cmd, **kwargs):
        captured_models.append(cmd[cmd.index("--model") + 1] if "--model" in cmd else None)
        result = MagicMock()
        result.returncode = 0
        result.stdout = _envelope()
        return result

    with patch("pcp.commands.build.subprocess.run", side_effect=fake_run), \
         patch("pcp.commands.build._get_staged_files", return_value=[]), \
         patch("pcp.commands.build._get_unstaged_files", return_value=[]), \
         patch("pcp.commands.build._get_working_diff", return_value=""), \
         patch("pcp.commands.build._run_test_suite_check", side_effect=test_suite_results), \
         patch("pcp.commands.build._run_lint_check", return_value=[]), \
         patch("pcp.commands.build._run_sast_check", return_value=[]), \
         patch("pcp.commands.build._run_layer1_check", return_value=[]), \
         patch("pcp.commands.build._run_architect_review", return_value=[]), \
         patch("pcp.commands.build._run_gate_check", return_value=[]), \
         patch("pcp.commands.build.find_transcript_for_session", return_value=None):
        success, findings = _build_one_criterion(pcp_dir, tmp_path, mod, c, build_model, build_model_explicit, budget)

    return success, findings, captured_models


# ── pcp build coding-agent model selection ──

def test_default_build_model_is_sonnet_for_first_two_attempts(tmp_path):
    success, _findings, models = _run_build_one_criterion(
        tmp_path, llm.BUILD_MODEL, False,
        test_suite_results=[["fail 1"], []],
    )
    assert success is True
    assert models == ["sonnet", "sonnet"]


def test_default_build_model_escalates_to_opus_on_final_attempt(tmp_path):
    success, findings, models = _run_build_one_criterion(
        tmp_path, llm.BUILD_MODEL, False,
        test_suite_results=[["fail 1"], ["fail 2"], []],
    )
    assert success is True
    assert models == ["sonnet", "sonnet", "opus"]


def test_explicit_build_model_override_never_escalates(tmp_path):
    """A human's explicit PCP_BUILD_MODEL wins on every attempt -- attempt 3
    must not silently switch to Opus behind their back."""
    success, findings, models = _run_build_one_criterion(
        tmp_path, "custom-model-x", True,
        test_suite_results=[["fail 1"], ["fail 2"], ["fail 3"]],
    )
    assert success is False
    assert models == ["custom-model-x", "custom-model-x", "custom-model-x"]


# ── model-selection constants ──

def test_model_constants_match_reviewed_strategy():
    assert llm.JUDGE_MODEL == "haiku"
    assert llm.BUILD_MODEL == "sonnet"
    assert llm.ESCALATION_MODEL == "opus"


# ── wave-level architect-review promoted to Opus ──

def test_wave_architect_review_uses_escalation_model_not_judge_model(tmp_path):
    from pcp.commands.build import _run_wave_merge

    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    mod_dir = pcp_dir / "strategy" / "modules" / "widgets"
    mod_dir.mkdir(parents=True)
    (mod_dir / "spec.yaml").write_text(yaml.dump({"dependencies": []}))
    (mod_dir / "acceptance.yaml").write_text(yaml.dump({"criteria": []}))

    captured = {}

    def fake_call_json(system, user, model=None, **kwargs):
        if kwargs.get("command") == "wave-architect-review":
            captured["model"] = model
        return {"findings": []}

    with patch("pcp.qa.run_test_suite", return_value={"tool": None, "passed": True, "output": ""}), \
         patch("pcp.commands.build.llm.call_json", side_effect=fake_call_json), \
         patch("pcp.commands.architect_review._get_diff", return_value="diff --git a/x.py\n+x"), \
         patch("pcp.commands.architect_review._load_persona", return_value="persona"), \
         patch("pcp.commands.architect_review._load_kb", return_value=""):
        _run_wave_merge(pcp_dir, [{"name": "widgets", "spec": {"dependencies": []}}], "HEAD~1", wave_number=0)

    assert captured["model"] == llm.ESCALATION_MODEL
    assert captured["model"] != llm.JUDGE_MODEL


# ── kickoff/pm generation calls promoted to explicit Sonnet default ──

def test_pm_generation_call_uses_build_model(tmp_path):
    from click.testing import CliRunner
    from pcp.cli import cli

    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("# Objective\nBuild things.")

    captured = {}

    def fake_call_json(system, user, model=None, **kwargs):
        captured["model"] = model
        raise RuntimeError("stop after capturing model -- rest of pm() is out of scope here")

    with patch("pcp.commands.pm.llm.call_json", side_effect=fake_call_json):
        runner = CliRunner()
        runner.invoke(cli, ["pm", "add a widget", "--path", str(tmp_path)])

    assert captured["model"] == llm.BUILD_MODEL


def test_kickoff_generation_call_uses_build_model(tmp_path):
    from click.testing import CliRunner
    from pcp.cli import cli

    vision_file = tmp_path / "vision.md"
    vision_file.write_text("Build a todo app.")

    captured = {}

    def fake_call_json(system, user, model=None, **kwargs):
        captured["model"] = model
        raise RuntimeError("stop after capturing model -- rest of kickoff() is out of scope here")

    with patch("pcp.commands.kickoff.llm.call_json", side_effect=fake_call_json):
        runner = CliRunner()
        runner.invoke(cli, ["kickoff", str(vision_file), "--path", str(tmp_path), "--force"])

    assert captured["model"] == llm.BUILD_MODEL
