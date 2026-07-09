import yaml
from click.testing import CliRunner

from pcp.cli import cli
from pcp.commands.dashboard import build_dashboard_data, render_html, _module_status


def _init_pcp(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    return pcp_dir


def _write_module(pcp_dir, name, deps, criteria):
    mod_dir = pcp_dir / "strategy" / "modules" / name
    mod_dir.mkdir(parents=True)
    spec = {"version": "1.0", "module": name, "description": "d", "objective_coverage": ["x"], "dependencies": deps}
    (mod_dir / "spec.yaml").write_text(yaml.dump(spec))
    acc = {"version": "1.0", "module": name, "criteria": [
        {"id": cid, "description": desc, "check": "manual", "status": status}
        for cid, desc, status in criteria
    ]}
    (mod_dir / "acceptance.yaml").write_text(yaml.dump(acc))


def test_module_status_helper():
    assert _module_status(0, 0) == "pending"
    assert _module_status(0, 3) == "pending"
    assert _module_status(1, 3) == "in_progress"
    assert _module_status(3, 3) == "complete"


def test_build_dashboard_data_no_modules(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    data = build_dashboard_data(pcp_dir)
    assert data["modules"] == []
    assert data["total"] == 0
    assert data["score_pct"] == 0.0


def test_build_dashboard_data_computes_waves_and_status(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    _write_module(pcp_dir, "core", [], [("C001", "d", "complete")])
    _write_module(pcp_dir, "auth", ["core"], [("A001", "d", "complete"), ("A002", "d", "pending")])

    data = build_dashboard_data(pcp_dir)
    by_name = {m["name"]: m for m in data["modules"]}
    assert by_name["core"]["wave"] == 0
    assert by_name["auth"]["wave"] == 1
    assert by_name["core"]["status"] == "complete"
    assert by_name["auth"]["status"] == "in_progress"
    assert by_name["auth"]["dependencies"] == ["core"]
    assert data["total"] == 3
    assert data["complete"] == 2


def test_render_html_is_well_formed_and_escapes_content(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    _write_module(pcp_dir, "add", [], [("A001", "<script>alert(1)</script>", "pending")])
    data = build_dashboard_data(pcp_dir)
    html = render_html(data)

    assert html.startswith("<!doctype html>")
    assert html.rstrip().endswith("</html>")
    assert "<script>alert(1)</script>" not in html  # must be escaped, not injected raw
    assert "&lt;script&gt;" in html


def test_render_html_shows_phase_and_milestones(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    (pcp_dir / "SDLC_phase.yaml").write_text(yaml.dump({
        "version": "1.0", "current_phase": "alpha",
        "phases": [{"name": "alpha", "exit_criteria": [
            {"id": "E001", "description": "core done", "check": "manual", "status": "complete"},
            {"id": "E002", "description": "auth done", "check": "manual", "status": "pending"},
        ]}],
    }))
    data = build_dashboard_data(pcp_dir)
    assert data["phase_name"] == "alpha"
    assert data["phases"][0]["done"] == 1
    assert data["phases"][0]["total"] == 2
    html = render_html(data)
    assert "alpha" in html
    assert "1/2 exit criteria" in html


def test_render_html_reflects_chain_integrity_break(tmp_path):
    import json
    from pcp import telemetry

    pcp_dir = _init_pcp(tmp_path)
    telemetry.record(pcp_dir, cycle="qa", control_id="CTRL-001", result="block", files=["a.py"])
    path = pcp_dir / "telemetry.jsonl"
    entry = json.loads(path.read_text().strip())
    entry["result"] = "pass"  # tampered
    path.write_text(json.dumps(entry) + "\n")

    data = build_dashboard_data(pcp_dir)
    html = render_html(data)
    assert "chain-break" in html
    assert "1 break(s)" in html


def test_dashboard_cli_writes_file_at_project_root(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    _write_module(pcp_dir, "add", [], [("A001", "core", "pending")])

    runner = CliRunner()
    result = runner.invoke(cli, ["dashboard", "--path", str(tmp_path)])
    assert result.exit_code == 0
    out = tmp_path / "dashboard.html"
    assert out.exists()
    assert "add" in out.read_text()


def test_dashboard_cli_no_pcp_dir_exits_2(tmp_path):
    runner = CliRunner()
    result = runner.invoke(cli, ["dashboard", "--path", str(tmp_path)])
    assert result.exit_code == 2


# ── QA status + evidence links per criterion ──

def test_criterion_carries_qa_records_keyed_by_check(tmp_path):
    from pcp import telemetry, evidence

    pcp_dir = _init_pcp(tmp_path)
    _write_module(pcp_dir, "add", [], [("A001", "core add", "complete")])
    ev = evidence.store(pcp_dir, "add", "A001", 1, "test-suite", "5 passed")
    telemetry.record(pcp_dir, cycle="qa", module="add", criterion_id="A001",
                      check="test-suite", control_id="CTRL-001", result="pass", evidence_path=ev)

    data = build_dashboard_data(pcp_dir)
    crit = data["modules"][0]["criteria"][0]
    assert "test-suite" in crit["qa"]
    assert crit["qa"]["test-suite"]["evidence_path"] == ev


def test_qa_lookup_keeps_latest_record_per_check(tmp_path):
    from pcp import telemetry, evidence

    pcp_dir = _init_pcp(tmp_path)
    _write_module(pcp_dir, "add", [], [("A001", "core add", "pending")])
    ev1 = evidence.store(pcp_dir, "add", "A001", 1, "test-suite", "attempt 1 failed")
    telemetry.record(pcp_dir, cycle="qa", module="add", criterion_id="A001",
                      check="test-suite", control_id="CTRL-001", result="block", evidence_path=ev1)
    ev2 = evidence.store(pcp_dir, "add", "A001", 2, "test-suite", "attempt 2 passed")
    telemetry.record(pcp_dir, cycle="qa", module="add", criterion_id="A001",
                      check="test-suite", control_id="CTRL-001", result="pass", evidence_path=ev2)

    data = build_dashboard_data(pcp_dir)
    crit = data["modules"][0]["criteria"][0]
    assert crit["qa"]["test-suite"]["result"] == "pass"
    assert crit["qa"]["test-suite"]["evidence_path"] == ev2


def test_render_html_links_to_evidence_file(tmp_path):
    from pcp import telemetry, evidence

    pcp_dir = _init_pcp(tmp_path)
    _write_module(pcp_dir, "add", [], [("A001", "core add", "complete")])
    ev = evidence.store(pcp_dir, "add", "A001", 1, "test-suite", "5 passed")
    telemetry.record(pcp_dir, cycle="qa", module="add", criterion_id="A001",
                      check="test-suite", control_id="CTRL-001", result="pass", evidence_path=ev)

    data = build_dashboard_data(pcp_dir)
    html = render_html(data)
    assert f'href=".pcp/{ev}"' in html
    assert "qa-chip complete" in html


def test_render_html_marks_blocked_check_distinctly(tmp_path):
    from pcp import telemetry, evidence

    pcp_dir = _init_pcp(tmp_path)
    _write_module(pcp_dir, "add", [], [("A001", "core add", "pending")])
    ev = evidence.store(pcp_dir, "add", "A001", 1, "lint", "issue found")
    telemetry.record(pcp_dir, cycle="qa", module="add", criterion_id="A001",
                      check="lint", control_id="CTRL-002", result="block", evidence_path=ev)

    html = render_html(build_dashboard_data(pcp_dir))
    assert "qa-chip blocked" in html


def test_wave_gates_section_shows_latest_wave_check(tmp_path):
    from pcp import telemetry, evidence

    pcp_dir = _init_pcp(tmp_path)
    ev = evidence.store(pcp_dir, "_wave", "wave_0", 0, "test-suite", "wave suite output")
    telemetry.record(pcp_dir, cycle="qa", check="wave-test-suite", control_id="CTRL-001",
                      result="pass", evidence_path=ev, cycle_number=0)

    data = build_dashboard_data(pcp_dir)
    assert len(data["wave_gates"]) == 1
    assert data["wave_gates"][0]["check"] == "wave-test-suite"
    html = render_html(data)
    assert "Wave Gates" in html
    assert f'href=".pcp/{ev}"' in html


def test_no_wave_gates_section_when_none_recorded(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    html = render_html(build_dashboard_data(pcp_dir))
    assert "Wave Gates" not in html


# ── Unified tabs: Overview / Objective & Gaps / Audit Trail / Architecture Justification ──

def test_dashboard_data_includes_objective_provenance_and_architecture_justification(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    (pcp_dir / "objective.md").write_text("# Program Objective\n\nWhy this exists: real business reason.")
    data = build_dashboard_data(pcp_dir)
    assert "real business reason" in data["objective_text"]
    assert data["pending_gaps"] == []
    assert "controls" in data["provenance"]
    assert "modules" in data["architecture_justification"]


def test_render_html_has_all_four_tabs(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    html = render_html(build_dashboard_data(pcp_dir))
    for tab_id, label in [
        ("tab-overview", "Overview"), ("tab-objective", "Objective &amp; Gaps"),
        ("tab-audit", "Audit Trail"), ("tab-arch", "Architecture Justification"),
    ]:
        assert f'id="{tab_id}"' in html
        assert label in html
    assert 'id="tab-overview" checked' in html, "Overview must be the default active tab"


def test_objective_tab_shows_real_objective_text_and_escapes_content(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    (pcp_dir / "objective.md").write_text("## Why\n\n<script>evil</script> real reason here.")
    html = render_html(build_dashboard_data(pcp_dir))
    assert "real reason here" in html
    assert "<script>evil</script>" not in html
    assert "&lt;script&gt;evil&lt;/script&gt;" in html


def test_objective_tab_shows_pending_gaps_from_current_state(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    (pcp_dir / "current_state.md").write_text("- [ ] MOD/A001: something not done yet\n- [x] MOD/A002: done\n")
    html = render_html(build_dashboard_data(pcp_dir))
    assert "MOD/A001: something not done yet" in html


def test_audit_trail_tab_shows_ssdf_crosswalk(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    (pcp_dir / "controls.yaml").write_text(yaml.dump({"controls": [
        {"id": "CTRL-001", "name": "Test Suite", "ssdf_practice": ["PW.7"]},
    ]}))
    html = render_html(build_dashboard_data(pcp_dir))
    assert "SSDF Crosswalk" in html
    assert "CTRL-001" in html
    assert "Test Suite" in html
    assert "GAP" in html  # never invoked, no telemetry recorded


def test_audit_trail_tab_shows_bypass_ledger(tmp_path):
    from pcp.commands import check as check_mod

    pcp_dir = _init_pcp(tmp_path)
    check_mod._log_bypass(pcp_dir, "known false positive, verified safe", ["SEC_001"])
    html = render_html(build_dashboard_data(pcp_dir))
    assert "Bypass Ledger" in html
    assert "known false positive, verified safe" in html


def test_architecture_justification_tab_shows_tier_distribution_and_module_decisions(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    mod_dir = pcp_dir / "strategy" / "modules" / "auth"
    mod_dir.mkdir(parents=True)
    (mod_dir / "spec.yaml").write_text(yaml.dump({
        "module": "auth", "description": "d",
        "build_vs_buy": {"decision": "reuse_whole", "rationale": "Auth0 fits cleanly"},
    }))
    (mod_dir / "acceptance.yaml").write_text(yaml.dump({"module": "auth", "criteria": [
        {"id": "A001", "description": "Password check", "check": "manual", "status": "pending",
         "logic_tier": 1, "build_vs_buy": {"decision": "build_fresh", "rationale": "trivial"}},
    ]}))
    html = render_html(build_dashboard_data(pcp_dir))
    assert "Logic-Tier Distribution" in html
    assert "Deterministic" in html
    assert "auth" in html.lower()
    assert "reuse_whole" in html
    assert "Auth0 fits cleanly" in html
    assert "build_fresh" in html


def test_architecture_justification_tab_shows_dash_for_v1_modules_without_tier_data(tmp_path):
    """A module still on the old (ungated) schema has no logic_tier/build_vs_buy
    -- must render as an honest dash, not a raw 'None'."""
    pcp_dir = _init_pcp(tmp_path)
    _write_module(pcp_dir, "legacy", [], [("A001", "old criterion", "pending")])
    html = render_html(build_dashboard_data(pcp_dir))
    assert "None (?)" not in html
    assert ">—<" in html or "—" in html
