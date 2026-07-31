"""A wave block caused by another module must not revert this wave's work.

Measured twice on Project O. A036-A039 were reverted on five blockers,
none from that build. Then 2026-07-30: core-data-model A022/A030/A033/A038 --
$30.04 spent, all four branches merged into main, all four marked `pending`.
The wave gate requires dependencies 100% complete, so a module downstream of an
incomplete dependency can never pass, and every attempt reverts merged work.
"""
import yaml

from pcp.commands.build import _finding_blames_outside_wave, _reopen_wave_criteria


def _module(pcp_dir, name, ids, status="complete"):
    d = pcp_dir / "strategy" / "modules" / name
    d.mkdir(parents=True)
    (d / "acceptance.yaml").write_text(yaml.dump({
        "version": "2.0", "module": name,
        "criteria": [{"id": i, "description": "d", "check": "manual", "status": status} for i in ids],
    }))
    return {"name": name, "pending_criteria": [{"id": i} for i in ids]}


def _status(pcp_dir, name):
    d = yaml.safe_load((pcp_dir / "strategy" / "modules" / name / "acceptance.yaml").read_text())
    return {c["id"]: c["status"] for c in d["criteria"]}


# ── the classifier ────────────────────────────────────────────────────────────

def test_dependency_outside_the_wave_is_external():
    f = "Contract: 'agent-query-interface' depends on 'core-data-model', which has incomplete criteria: A022, A030"
    assert _finding_blames_outside_wave(f, {"agent-query-interface"}) is True


def test_dependency_inside_the_wave_is_not_external():
    """If the dependency WAS built in this wave, the wave really is implicated."""
    f = "Contract: 'app-kit' depends on 'web-server', which has incomplete criteria: A009"
    assert _finding_blames_outside_wave(f, {"app-kit", "web-server"}) is False


def test_a_finding_naming_no_dependency_is_never_external():
    """Security findings, test failures, review blocks — all about the wave's diff."""
    for f in ("path traversal: ../../etc/passwd escapes root",
              "Wave integration suite (pytest) FAILED",
              "coupling violation: circular dependency"):
        assert _finding_blames_outside_wave(f, {"anything"}) is False


# ── the reopen decision ───────────────────────────────────────────────────────

def test_purely_external_block_leaves_merged_criteria_complete(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    mod = _module(pcp_dir, "agent-query-interface", ["A036", "A037"])
    _reopen_wave_criteria(pcp_dir, [mod], 2, [
        "Contract: 'agent-query-interface' depends on 'core-data-model', which has incomplete criteria: A022",
    ])
    assert _status(pcp_dir, "agent-query-interface") == {"A036": "complete", "A037": "complete"}


def test_external_block_still_records_an_escalation(tmp_path):
    """The block is real. Only the false 'not built' claim is withdrawn."""
    pcp_dir = tmp_path / ".pcp"
    mod = _module(pcp_dir, "agent-query-interface", ["A036"])
    _reopen_wave_criteria(pcp_dir, [mod], 2, [
        "Contract: 'agent-query-interface' depends on 'core-data-model', which has incomplete criteria: A022",
    ])
    esc = yaml.safe_load((pcp_dir / "escalations.yaml").read_text())
    items = esc.get("escalations", esc) if isinstance(esc, dict) else esc
    assert len(items) == 1
    assert items[0]["route"] == "wave-block"


def test_a_real_finding_still_reopens_everything(tmp_path):
    """The 2026-07-27 path-traversal case must keep working unchanged."""
    pcp_dir = tmp_path / ".pcp"
    mod = _module(pcp_dir, "web-ui", ["A013", "A016"])
    _reopen_wave_criteria(pcp_dir, [mod], 0, ["path traversal: arbitrary local file read"])
    assert _status(pcp_dir, "web-ui") == {"A013": "pending", "A016": "pending"}


def test_one_real_finding_among_external_ones_still_reopens(tmp_path):
    """Mixed findings are not a licence to skip — any implicated work reopens."""
    pcp_dir = tmp_path / ".pcp"
    mod = _module(pcp_dir, "web-ui", ["A013"])
    _reopen_wave_criteria(pcp_dir, [mod], 0, [
        "Contract: 'web-ui' depends on 'core-data-model', which has incomplete criteria: A022",
        "Wave integration suite (pytest) FAILED",
    ])
    assert _status(pcp_dir, "web-ui") == {"A013": "pending"}


# ── CTRL-018 must not record a block it never performed ───────────────────────

def test_scope_guard_records_advisory_not_block_in_warn_mode(tmp_path, monkeypatch):
    """110 of Project O's 259 `block` records were this check in warn
    mode — 42.5% of every block ever recorded there never blocked anything."""
    import json
    from pcp.commands import build as B

    pcp_dir = tmp_path / ".pcp"
    (pcp_dir / "evidence").mkdir(parents=True)
    monkeypatch.setenv("PCP_BUILD_SCOPE_MODE", "warn")
    monkeypatch.setattr(B, "_scope_allowlist_violations", lambda *a: [".mcp.json"])

    ctx = {"module": "billing", "criterion_id": "A001", "attempt": 1}
    returned = B._run_scope_check(
        pcp_dir, {"name": "billing"}, {"id": "A001"}, [".mcp.json"], ctx,
    )

    assert returned == []          # warn mode does not block the attempt
    rec = [json.loads(l) for l in (pcp_dir / "telemetry.jsonl").read_text().splitlines() if l.strip()]
    scope = [r for r in rec if r.get("control_id") == "CTRL-018"]
    assert len(scope) == 1
    assert scope[0]["result"] == "advisory"
    assert scope[0]["errors"]      # the finding is still recorded, not discarded


def test_scope_guard_still_records_block_when_mode_is_block(tmp_path, monkeypatch):
    import json
    from pcp.commands import build as B

    pcp_dir = tmp_path / ".pcp"
    (pcp_dir / "evidence").mkdir(parents=True)
    monkeypatch.setenv("PCP_BUILD_SCOPE_MODE", "block")
    monkeypatch.setattr(B, "_scope_allowlist_violations", lambda *a: ["src/other/thing.py"])

    ctx = {"module": "billing", "criterion_id": "A001", "attempt": 1}
    returned = B._run_scope_check(
        pcp_dir, {"name": "billing"}, {"id": "A001"}, ["src/other/thing.py"], ctx,
    )

    assert returned                # it really does block
    rec = [json.loads(l) for l in (pcp_dir / "telemetry.jsonl").read_text().splitlines() if l.strip()]
    assert [r for r in rec if r.get("control_id") == "CTRL-018"][0]["result"] == "block"
