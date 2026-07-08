import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from pcp import policy

HAS_OPA = shutil.which("opa") is not None
REAL_POLICIES_DIR = policy.get_policies_dir(Path(".pcp"))


def _copy_real_policies(pcp_dir):
    policies_dir = policy.get_policies_dir(pcp_dir)
    policies_dir.mkdir(parents=True)
    for rego in REAL_POLICIES_DIR.glob("*.rego"):
        (policies_dir / rego.name).write_text(rego.read_text())


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
