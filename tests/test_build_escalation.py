import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from pcp.commands.build import _record_escalation
from pcp import telemetry

HAS_OPA = shutil.which("opa") is not None


def _scaffold_escalation_policy(pcp_dir):
    policies_dir = pcp_dir / "policies"
    policies_dir.mkdir(parents=True, exist_ok=True)
    real = Path(".pcp") / "policies" / "escalation.rego"
    (policies_dir / "escalation.rego").write_text(real.read_text())


def test_record_escalation_noop_without_policy(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _record_escalation(pcp_dir, "add", "A001", ["some violation"])
    assert telemetry.load(pcp_dir) == []


@pytest.mark.skipif(not HAS_OPA, reason="opa binary not installed")
def test_record_escalation_routes_to_human_on_many_violations(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _scaffold_escalation_policy(pcp_dir)
    # 6 distinct violations -> confidence_score 0.0 -> route "human"
    violations = [f"violation {i}" for i in range(6)]
    _record_escalation(pcp_dir, "add", "A001", violations)
    records = telemetry.load(pcp_dir)
    assert len(records) == 1
    assert records[0]["check"] == "escalation"
    assert "route=human" in records[0]["errors"]


@pytest.mark.skipif(not HAS_OPA, reason="opa binary not installed")
def test_record_escalation_routes_to_agent_on_single_violation(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _scaffold_escalation_policy(pcp_dir)
    # 1 violation out of 6 -> confidence_score ~0.83 -> route "agent"
    _record_escalation(pcp_dir, "add", "A001", ["Lint (ruff) found issues:\nsome issue"])
    records = telemetry.load(pcp_dir)
    assert "route=agent" in records[0]["errors"]


@pytest.mark.skipif(not HAS_OPA, reason="opa binary not installed")
def test_record_escalation_high_stakes_on_sec_finding_routes_to_human(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _scaffold_escalation_policy(pcp_dir)
    # Only 1 violation (would normally route to "agent"), but it's a SEC_* finding.
    _record_escalation(pcp_dir, "add", "A001", ["AST Rule [SEC_001] No hardcoded secrets violation: x.py:1"])
    records = telemetry.load(pcp_dir)
    assert "route=human" in records[0]["errors"]
