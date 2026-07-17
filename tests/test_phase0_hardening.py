"""Phase 0 launch-hardening (2026-07-17 build plan): verifier pre-check +
decorrelation, spend ceiling, escalation ack/MTTA, heartbeat, fresh-session
escalation."""

import json
import time
from unittest.mock import MagicMock, patch

import yaml

from pcp import escalations, spend
from pcp.commands.build import _build_one_criterion, _verify_block_findings, _BuildBudget
from pcp.commands.watch import check_notify_heartbeat
from pcp.llm import client as llm

CTX = {"attempt": 1, "module": "m", "criterion_id": "A1", "files": ["real.py"]}


# ── 0.1 deterministic pre-check ──

def test_precheck_drops_finding_citing_unknown_file_without_llm_call(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    with patch("pcp.commands.build.llm.call_json") as mock_call:
        kept, dropped = _verify_block_findings(
            pcp_dir, "diff touching real.py only",
            ["ghost.py: fabricated issue in a file that does not exist"],
            CTX, "gate", "CTRL-006",
        )
    mock_call.assert_not_called()  # all findings died in the pre-check — no LLM spend
    assert kept == []
    assert len(dropped) == 1
    assert "deterministic pre-check" in dropped[0]


def test_precheck_keeps_finding_citing_changed_file(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    with patch("pcp.commands.build.llm.call_json",
               return_value=({"verdicts": [{"index": 0, "refuted": False}]}, {})):
        kept, dropped = _verify_block_findings(
            pcp_dir, "unrelated diff text", ["real.py: genuine issue"], CTX, "gate", "CTRL-006",
        )
    assert kept == ["real.py: genuine issue"]


# ── 0.2 verifier decorrelation ──

def test_verifier_uses_different_model_than_judge(tmp_path, monkeypatch):
    monkeypatch.delenv("PCP_VERIFIER_MODEL", raising=False)
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    captured = {}

    def fake_call(system, prompt, **kw):
        captured["model"] = kw.get("model")
        return {"verdicts": [{"index": 0, "refuted": False}]}, {}

    with patch("pcp.commands.build.llm.call_json", side_effect=fake_call):
        _verify_block_findings(pcp_dir, "diff", ["conceptual finding, no file cited"], CTX, "gate", "CTRL-006")
    assert captured["model"] == llm.BUILD_MODEL
    assert captured["model"] != llm.JUDGE_MODEL


def test_verifier_model_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("PCP_VERIFIER_MODEL", "some-cross-vendor-model")
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    captured = {}

    def fake_call(system, prompt, **kw):
        captured["model"] = kw.get("model")
        return {"verdicts": []}, {}

    with patch("pcp.commands.build.llm.call_json", side_effect=fake_call):
        _verify_block_findings(pcp_dir, "diff", ["conceptual finding"], CTX, "gate", "CTRL-006")
    assert captured["model"] == "some-cross-vendor-model"


# ── 0.3 spend ceiling ──

def _ledger(pcp_dir, costs):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    calls = [{"timestamp": now, "cost_usd": c} for c in costs]
    (pcp_dir / "token_ledger.yaml").write_text(yaml.dump({"calls": calls}))


def test_no_ceiling_configured_always_allowed(tmp_path, monkeypatch):
    monkeypatch.delenv("PCP_PROJECT_BUDGET_USD", raising=False)
    monkeypatch.delenv("PCP_PROJECT_DAILY_BUDGET_USD", raising=False)
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _ledger(pcp_dir, [999.0])
    allowed, _ = spend.check_ceiling(pcp_dir)
    assert allowed


def test_total_ceiling_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("PCP_PROJECT_BUDGET_USD", "10")
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _ledger(pcp_dir, [6.0, 5.0])
    allowed, reason = spend.check_ceiling(pcp_dir)
    assert not allowed
    assert "ceiling" in reason


def test_daily_ceiling_blocks_today_only(tmp_path, monkeypatch):
    monkeypatch.delenv("PCP_PROJECT_BUDGET_USD", raising=False)
    monkeypatch.setenv("PCP_PROJECT_DAILY_BUDGET_USD", "5")
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "token_ledger.yaml").write_text(yaml.dump({"calls": [
        {"timestamp": "2020-01-01T00:00:00Z", "cost_usd": 100.0},  # old — ignored by daily cap
        {"timestamp": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "cost_usd": 1.0},
    ]}))
    allowed, _ = spend.check_ceiling(pcp_dir)
    assert allowed  # only $1 today


# ── 0.4 escalation ack / MTTA / stakes-scaled staleness ──

def _esc_project(tmp_path, deps=False):
    pcp_dir = tmp_path / ".pcp"
    mod = pcp_dir / "strategy" / "modules" / "core"
    mod.mkdir(parents=True)
    (mod / "acceptance.yaml").write_text(yaml.dump({"criteria": [{"id": "A1", "status": "pending"}]}))
    (mod / "spec.yaml").write_text(yaml.dump({"name": "core", "dependencies": []}))
    if deps:
        other = pcp_dir / "strategy" / "modules" / "web"
        other.mkdir(parents=True)
        (other / "spec.yaml").write_text(yaml.dump({"name": "web", "dependencies": ["core"]}))
    return pcp_dir


def _backdate_all(pcp_dir, hours):
    from datetime import datetime, timedelta, timezone
    path = pcp_dir / escalations.ESCALATIONS_FILE
    data = yaml.safe_load(path.read_text())
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for e in data["escalations"]:
        e["timestamp"] = ts
    path.write_text(yaml.dump(data))


def test_ack_and_mtta(tmp_path):
    pcp_dir = _esc_project(tmp_path)
    escalations.record(pcp_dir, "core", "A1", findings=["Test suite failed"])
    assert escalations.acknowledge(pcp_dir, "core", "A1") == 1
    assert escalations.mtta_hours(pcp_dir) is not None
    entry = escalations.load(pcp_dir)[0]
    assert entry["acknowledged_at"]
    assert entry["category"] == "action"


def test_acked_but_stalled_rescreams(tmp_path):
    pcp_dir = _esc_project(tmp_path)
    escalations.record(pcp_dir, "core", "A1")
    escalations.acknowledge(pcp_dir, "core", "A1")
    # backdate both timestamp and ack far past 2x threshold
    path = pcp_dir / escalations.ESCALATIONS_FILE
    data = yaml.safe_load(path.read_text())
    data["escalations"][0]["timestamp"] = "2020-01-01T00:00:00Z"
    data["escalations"][0]["acknowledged_at"] = "2020-01-01T01:00:00Z"
    path.write_text(yaml.dump(data))
    stale = escalations.find_stale(pcp_dir)
    assert len(stale) == 1
    assert stale[0]["state"] == "acked-stalled"


def test_stakes_scaled_threshold_for_depended_on_module(tmp_path, monkeypatch):
    monkeypatch.setenv("PCP_ESCALATION_STALE_HOURS", "24")
    pcp_dir = _esc_project(tmp_path, deps=True)  # web depends on core
    escalations.record(pcp_dir, "core", "A1")
    _backdate_all(pcp_dir, hours=13)  # past 12h (24/2) but under 24h
    stale = escalations.find_stale(pcp_dir)
    assert len(stale) == 1 and stale[0]["state"] == "unacked"


# ── 0.5 heartbeat dead-man's-switch ──

def test_heartbeat_flags_attempts_without_success(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "notify_heartbeat.yaml").write_text(yaml.dump({
        "last_attempt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "last_success": "2020-01-01T00:00:00Z",
    }))
    warning = check_notify_heartbeat(pcp_dir)
    assert warning and "NOT being reached" in warning


def test_heartbeat_quiet_when_deliveries_succeed(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (pcp_dir / "notify_heartbeat.yaml").write_text(yaml.dump({"last_attempt": now, "last_success": now}))
    assert check_notify_heartbeat(pcp_dir) is None


# ── 0.6 fresh-session escalation on attempt 3 ──

def test_attempt_three_gets_fresh_session_with_failure_summary(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    acc_path = pcp_dir / "acc.yaml"
    acc_path.write_text(yaml.dump({"criteria": [{"id": "A001", "description": "impl"}]}))
    mod = {"name": "widgets", "spec": {}, "acc_path": acc_path}
    c = {"id": "A001", "description": "impl"}
    budget = _BuildBudget(max_sessions=10)
    calls = []

    def fake_run(cmd, **kwargs):
        if "--session-id" in cmd or "--resume" in cmd:
            calls.append({"cmd": list(cmd), "prompt": kwargs.get("input", "")})
        result = MagicMock()
        result.returncode = 0
        result.stdout = json.dumps({"is_error": False, "result": "done", "session_id": "s",
                                    "usage": {}, "total_cost_usd": 0.0, "duration_ms": 1})
        return result

    with patch("pcp.commands.build.subprocess.run", side_effect=fake_run), \
         patch("pcp.commands.build._git_head", return_value="REF"), \
         patch("pcp.commands.build._get_changed_files_since", return_value=[]), \
         patch("pcp.commands.build._get_working_diff", return_value=""), \
         patch("pcp.commands.build._run_test_suite_check", side_effect=[["f1"], ["f2"], []]), \
         patch("pcp.commands.build._run_lint_check", return_value=[]), \
         patch("pcp.commands.build._run_sast_check", return_value=[]), \
         patch("pcp.commands.build._run_layer1_check", return_value=[]), \
         patch("pcp.commands.build._run_scope_check", return_value=[]), \
         patch("pcp.commands.build._run_architect_review", return_value=[]), \
         patch("pcp.commands.build._run_gate_check", return_value=[]), \
         patch("pcp.commands.build._run_design_justification_check", return_value=[]), \
         patch("pcp.commands.build._run_build_vs_buy_justification_check", return_value=[]), \
         patch("pcp.commands.build.find_transcript_for_session", return_value=None), \
         patch("pcp.commands.build.spend.check_ceiling", return_value=(True, "ok")):
        success, _ = _build_one_criterion(pcp_dir, tmp_path, mod, c, llm.BUILD_MODEL, False, budget)

    assert success is True
    assert len(calls) == 3
    assert "--session-id" in calls[0]["cmd"]
    assert "--resume" in calls[1]["cmd"]
    # attempt 3: FRESH session (CCRM contamination fix), not resume
    assert "--resume" not in calls[2]["cmd"]
    assert "--session-id" in calls[2]["cmd"]
    assert calls[2]["cmd"][calls[2]["cmd"].index("--session-id") + 1] != calls[0]["cmd"][calls[0]["cmd"].index("--session-id") + 1]
    assert "Prior attempts on this criterion FAILED" in calls[2]["prompt"]
    assert "blocked by gates" in calls[2]["prompt"]
