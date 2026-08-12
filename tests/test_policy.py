import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from pcp import policy
from pcp.commands.init import (
    POLICY_ESCALATION_TEMPLATE,
    POLICY_BYPASS_TEMPLATE,
    POLICY_COUPLING_TEMPLATE,
    POLICY_DEPLOY_TEMPLATE,
)

HAS_OPA = shutil.which("opa") is not None

# Sourced from the same templates `pcp init` actually ships, not a local
# `.pcp/policies/` on disk -- that dir is gitignored (project-specific
# dogfood state, never distributed), so a fresh clone has none and these
# tests silently broke/degraded for anyone who wasn't the maintainer's own
# machine. Real incident, cold-clone review 2026-08-12.
_SHIPPED_POLICIES = {
    "escalation.rego": POLICY_ESCALATION_TEMPLATE,
    "bypass_approval.rego": POLICY_BYPASS_TEMPLATE,
    "coupling_threshold.rego": POLICY_COUPLING_TEMPLATE,
    "deploy_policy.rego": POLICY_DEPLOY_TEMPLATE,
}


def _copy_real_policies(pcp_dir):
    policies_dir = policy.get_policies_dir(pcp_dir)
    policies_dir.mkdir(parents=True)
    for name, content in _SHIPPED_POLICIES.items():
        (policies_dir / name).write_text(content)


def test_opa_unavailable_returns_false_flag(tmp_path):
    with patch("shutil.which", return_value=None):
        result = policy.evaluate(tmp_path, "data.pcp.escalation.route", {})
    assert result == {"available": False}


def test_no_policies_dir_is_undefined_not_error(tmp_path):
    if not HAS_OPA:
        pytest.skip("opa binary not installed")
    result = policy.evaluate(tmp_path, "data.pcp.escalation.route", {"confidence_score": 0.9})
    assert result["available"] is True
    assert result["undefined"] is True
    assert result["value"] is None


@pytest.mark.skipif(not HAS_OPA, reason="opa binary not installed")
def test_escalation_routes_low_confidence_to_human(tmp_path):
    _copy_real_policies(tmp_path)
    result = policy.evaluate(tmp_path, "data.pcp.escalation.route", {"confidence_score": 0.3, "high_stakes": False})
    assert result == {"available": True, "value": "human", "undefined": False}


@pytest.mark.skipif(not HAS_OPA, reason="opa binary not installed")
def test_escalation_routes_high_confidence_to_agent(tmp_path):
    _copy_real_policies(tmp_path)
    result = policy.evaluate(tmp_path, "data.pcp.escalation.route", {"confidence_score": 0.9, "high_stakes": False})
    assert result["value"] == "agent"


@pytest.mark.skipif(not HAS_OPA, reason="opa binary not installed")
def test_escalation_high_stakes_always_routes_to_human(tmp_path):
    _copy_real_policies(tmp_path)
    result = policy.evaluate(tmp_path, "data.pcp.escalation.route", {"confidence_score": 0.99, "high_stakes": True})
    assert result["value"] == "human"


@pytest.mark.skipif(not HAS_OPA, reason="opa binary not installed")
def test_bypass_rejects_placeholder_reasons(tmp_path):
    _copy_real_policies(tmp_path)
    for placeholder in ("reason", "todo", "TEST", "FixMe", "  "):
        result = policy.evaluate(tmp_path, "data.pcp.bypass.approved", {"reason": placeholder})
        assert result["value"] is False, f"expected {placeholder!r} to be rejected"


@pytest.mark.skipif(not HAS_OPA, reason="opa binary not installed")
def test_bypass_approves_real_reason(tmp_path):
    _copy_real_policies(tmp_path)
    result = policy.evaluate(tmp_path, "data.pcp.bypass.approved",
                              {"reason": "SEC_002 self-match on rule description text, not real eval/exec"})
    assert result["value"] is True


@pytest.mark.skipif(not HAS_OPA, reason="opa binary not installed")
def test_coupling_color_bands(tmp_path):
    _copy_real_policies(tmp_path)
    assert policy.evaluate(tmp_path, "data.pcp.coupling.coupling_color", {"coupling_score": 0.9})["value"] == "green"
    assert policy.evaluate(tmp_path, "data.pcp.coupling.coupling_color", {"coupling_score": 0.7})["value"] == "yellow"
    assert policy.evaluate(tmp_path, "data.pcp.coupling.coupling_color", {"coupling_score": 0.3})["value"] == "red"


def test_evaluate_never_raises_on_bad_query(tmp_path):
    if not HAS_OPA:
        pytest.skip("opa binary not installed")
    _copy_real_policies(tmp_path)
    result = policy.evaluate(tmp_path, "not a valid query!!!", {})
    assert result["available"] is True
    assert "error" in result
