"""Escalation ledger + staleness watchdog (escalations.py)."""

from datetime import datetime, timedelta, timezone

import yaml

from pcp import escalations


def _init_pcp(tmp_path, criterion_status="pending"):
    pcp_dir = tmp_path / ".pcp"
    mod_dir = pcp_dir / "strategy" / "modules" / "auth"
    mod_dir.mkdir(parents=True)
    (mod_dir / "acceptance.yaml").write_text(yaml.dump({
        "version": "2.0",
        "criteria": [{"id": "A1", "description": "x", "status": criterion_status}],
    }))
    return pcp_dir


def _backdate(pcp_dir, hours):
    """Rewrite every ledger entry's timestamp to `hours` ago."""
    path = pcp_dir / escalations.ESCALATIONS_FILE
    data = yaml.safe_load(path.read_text())
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for e in data["escalations"]:
        e["timestamp"] = ts
    path.write_text(yaml.dump(data))


def test_record_and_load_roundtrip(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    escalations.record(pcp_dir, "auth", "A1", findings=["gate: broke", "lint: bad"])
    entries = escalations.load(pcp_dir)
    assert len(entries) == 1
    assert entries[0]["module"] == "auth"
    assert entries[0]["criterion_id"] == "A1"
    assert entries[0]["route"] == "human"
    assert entries[0]["findings_count"] == 2


def test_fresh_escalation_is_not_stale(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    escalations.record(pcp_dir, "auth", "A1")
    assert escalations.find_stale(pcp_dir) == []


def test_old_unresolved_escalation_is_stale(tmp_path):
    pcp_dir = _init_pcp(tmp_path, criterion_status="pending")
    escalations.record(pcp_dir, "auth", "A1")
    _backdate(pcp_dir, hours=25)
    stale = escalations.find_stale(pcp_dir)
    assert len(stale) == 1
    assert stale[0]["age_hours"] >= 24


def test_completed_criterion_resolves_escalation(tmp_path):
    """Completion of the criterion is the deterministic ack proxy — the stale
    alert must clear once someone acted."""
    pcp_dir = _init_pcp(tmp_path, criterion_status="complete")
    escalations.record(pcp_dir, "auth", "A1")
    _backdate(pcp_dir, hours=25)
    assert escalations.find_stale(pcp_dir) == []


def test_removed_module_resolves_escalation(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    escalations.record(pcp_dir, "auth", "A1")
    _backdate(pcp_dir, hours=25)
    (pcp_dir / "strategy" / "modules" / "auth" / "acceptance.yaml").unlink()
    assert escalations.find_stale(pcp_dir) == []


def test_stale_threshold_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("PCP_ESCALATION_STALE_HOURS", "1")
    pcp_dir = _init_pcp(tmp_path)
    escalations.record(pcp_dir, "auth", "A1")
    _backdate(pcp_dir, hours=2)
    assert len(escalations.find_stale(pcp_dir)) == 1


def test_corrupt_ledger_never_raises(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    (pcp_dir / escalations.ESCALATIONS_FILE).write_text(": not [ yaml {")
    assert escalations.load(pcp_dir) == []
    assert escalations.find_stale(pcp_dir) == []
    escalations.record(pcp_dir, "auth", "A1")  # must not raise either
