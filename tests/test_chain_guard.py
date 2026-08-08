"""chain_guard.py -- auto-verify hash chains at the start of build/verify/scan
instead of only on-demand via `pcp provenance`. See chain_guard.py's docstring."""

import json
import platform

import yaml
from click.testing import CliRunner

from pcp import chain_guard, decision_log, telemetry
from pcp.evidence_chain import set_append_only, clear_append_only
from pcp.cli import cli


def test_clean_project_has_no_breaks(tmp_path):
    pcp = tmp_path / ".pcp"
    pcp.mkdir()
    telemetry.record(pcp, cycle="qa", check="lint", result="pass")
    decision_log.record(pcp, source="test", category="architecture", summary="s", evidence="e")
    chain_guard.assert_chain_integrity(pcp)  # must not raise


def test_tampered_telemetry_is_detected(tmp_path):
    pcp = tmp_path / ".pcp"
    pcp.mkdir()
    telemetry.record(pcp, cycle="qa", check="lint", result="pass")
    path = pcp / "telemetry.jsonl"
    clear_append_only(path)  # so the direct rewrite below succeeds even on macOS
    entry = json.loads(path.read_text().strip())
    entry["result"] = "block"  # tamper: flip a real finding to a clean pass-shaped edit
    path.write_text(json.dumps(entry) + "\n")

    breaks = chain_guard.check_all_chains(pcp)
    assert breaks["telemetry.jsonl"]

    try:
        chain_guard.assert_chain_integrity(pcp)
        assert False, "expected ChainIntegrityError"
    except chain_guard.ChainIntegrityError as e:
        assert "telemetry.jsonl" in str(e)


def test_deleted_entry_breaks_the_chain(tmp_path):
    pcp = tmp_path / ".pcp"
    pcp.mkdir()
    decision_log.record(pcp, source="test", category="architecture", summary="one", evidence="e")
    decision_log.record(pcp, source="test", category="architecture", summary="two", evidence="e")
    path = pcp / "decision_log.jsonl"
    clear_append_only(path)
    lines = path.read_text().splitlines()
    path.write_text(lines[1] + "\n")  # drop the first entry, keep the second

    breaks = chain_guard.check_all_chains(pcp)
    assert breaks["decision_log.jsonl"]


def test_build_refuses_on_broken_chain(tmp_path):
    pcp = tmp_path / ".pcp"
    (pcp / "strategy" / "modules").mkdir(parents=True)
    telemetry.record(pcp, cycle="qa", check="lint", result="pass")
    path = pcp / "telemetry.jsonl"
    clear_append_only(path)
    entry = json.loads(path.read_text().strip())
    entry["result"] = "block"
    path.write_text(json.dumps(entry) + "\n")

    result = CliRunner().invoke(cli, ["build", "--path", str(tmp_path)])
    assert result.exit_code == 2
    assert "chain integrity" in result.output.lower() or "tampered" in result.output.lower()


def test_scan_refuses_on_broken_chain(tmp_path):
    pcp = tmp_path / ".pcp"
    (pcp / "strategy" / "modules").mkdir(parents=True)
    decision_log.record(pcp, source="test", category="architecture", summary="s", evidence="e")
    path = pcp / "decision_log.jsonl"
    clear_append_only(path)
    entry = json.loads(path.read_text().strip())
    entry["summary"] = "tampered"
    path.write_text(json.dumps(entry) + "\n")

    result = CliRunner().invoke(cli, ["scan", "--path", str(tmp_path)])
    assert result.exit_code == 2


def test_verify_refuses_on_broken_chain(tmp_path):
    pcp = tmp_path / ".pcp"
    mod = pcp / "strategy" / "modules" / "x"
    mod.mkdir(parents=True)
    (mod / "acceptance.yaml").write_text(yaml.dump({
        "version": "2.0", "module": "x",
        "criteria": [{"id": "A001", "description": "d", "check": "manual", "status": "pending"}],
    }))
    telemetry.record(pcp, cycle="qa", check="lint", result="pass")
    path = pcp / "telemetry.jsonl"
    clear_append_only(path)
    entry = json.loads(path.read_text().strip())
    entry["result"] = "block"
    path.write_text(json.dumps(entry) + "\n")

    result = CliRunner().invoke(
        cli, ["verify", "x", "A001", "--reason", "done", "--path", str(tmp_path)],
    )
    assert result.exit_code == 2


def test_bypass_log_write_survives_append_only_flag(tmp_path):
    """_log_bypass does a read-modify-rewrite (whole YAML doc), not a pure
    append -- confirms clear_append_only/set_append_only around it means a
    SECOND bypass write still succeeds even after the first one flagged the
    file append-only."""
    from pcp.commands.check import _log_bypass

    pcp = tmp_path / ".pcp"
    pcp.mkdir()
    _log_bypass(pcp, "first reason", ["R001"])
    _log_bypass(pcp, "second reason", ["R002"])  # would raise on macOS if the flag weren't cleared first

    data = yaml.safe_load((pcp / "bypass_log.yaml").read_text())
    assert len(data["bypasses"]) == 2
    assert chain_guard.check_all_chains(pcp)["bypass_log.yaml"] == []


def test_append_only_is_noop_off_macos(tmp_path, monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    p = tmp_path / "f.txt"
    p.write_text("x")
    set_append_only(p)  # must not raise
    clear_append_only(p)
