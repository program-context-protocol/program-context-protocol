"""pcp diff-reduce — Loop 2. Five gates, each reusing an existing PCP
mechanism (see diff_reduce.py's module docstring). Every test mocks the
real build/pm callbacks -- this suite verifies the LOOP's own gating
behavior, not `pcp build`/`pcp pm` themselves (those have their own suites)."""

from unittest.mock import patch

import yaml

from pcp import escalations
from pcp import run_log
from pcp.commands.diff_reduce import run_diff_reduce


def _project(tmp_path, module="billing", criteria=None):
    root = tmp_path / "p"
    mod = root / ".pcp" / "strategy" / "modules" / module
    mod.mkdir(parents=True)
    (mod / "acceptance.yaml").write_text(yaml.dump({
        "version": "2.0", "module": module, "criteria": criteria or [],
    }))
    (mod / "spec.yaml").write_text(yaml.dump({"version": "2.0", "module": module, "description": "d"}))
    (root / ".pcp" / "objective.md").write_text("# objective\ncover billing")
    return root


def _acc(root, module="billing"):
    return yaml.safe_load((root / ".pcp" / "strategy" / "modules" / module / "acceptance.yaml").read_text())


def test_dry_run_stops_immediately_with_nothing_to_do(tmp_path):
    root = _project(tmp_path, criteria=[])
    pcp_dir = root / ".pcp"
    with patch("pcp.commands.build.build.callback") as mock_build:
        result = run_diff_reduce(pcp_dir, root, module_name="billing", yes=True)
    assert result["stopped_reason"] == "dry -- nothing open"
    assert result["rounds_run"] == 1
    assert result["gap_still_open"] is False
    mock_build.assert_not_called()


def test_pending_criterion_triggers_one_build_call_then_goes_dry(tmp_path):
    root = _project(tmp_path, criteria=[
        {"id": "A001", "description": "charge endpoint", "check": "manual", "status": "pending"},
    ])
    pcp_dir = root / ".pcp"

    def _fake_build(module_name=None, project_path=None, yes=False):
        # Simulate a successful build marking the criterion complete --
        # real pcp build would do this via _mark_criterion_complete.
        acc = _acc(root)
        acc["criteria"][0]["status"] = "complete"
        acc["criteria"][0]["verified_by"] = "pcp_build"
        (root / ".pcp" / "strategy" / "modules" / "billing" / "acceptance.yaml").write_text(
            yaml.dump(acc, default_flow_style=False)
        )

    with patch("pcp.commands.build.build.callback", side_effect=_fake_build) as mock_build:
        result = run_diff_reduce(pcp_dir, root, module_name="billing", yes=True, max_rounds=3)
    assert mock_build.call_count == 1
    assert result["gap_still_open"] is False
    assert result["stopped_reason"] == "dry -- nothing open"
    assert result["rounds_run"] == 2  # round 1 built it, round 2 sees it's dry


def test_round_cap_hit_with_open_gap_records_escalation(tmp_path):
    root = _project(tmp_path, criteria=[
        {"id": "A001", "description": "charge endpoint", "check": "manual", "status": "pending"},
    ])
    pcp_dir = root / ".pcp"

    with patch("pcp.commands.build.build.callback") as mock_build:  # never actually completes it
        result = run_diff_reduce(pcp_dir, root, module_name="billing", yes=True, max_rounds=2)

    assert mock_build.call_count == 2
    assert result["stopped_reason"] == "round cap (2) reached"
    assert result["gap_still_open"] is True
    escs = escalations.load(pcp_dir)
    assert any(e["route"] == "diff-reduce-cap-hit" for e in escs)


def test_spot_check_reopens_a_criterion_whose_file_no_longer_exists(tmp_path):
    root = _project(tmp_path, criteria=[
        {"id": "A001", "description": "charge endpoint", "check": "file_exists",
         "target": "src/missing.py", "status": "complete", "verified_by": "pcp_build"},
    ])
    pcp_dir = root / ".pcp"

    with patch("pcp.commands.build.build.callback") as mock_build:
        result = run_diff_reduce(pcp_dir, root, module_name="billing", yes=True, max_rounds=1)

    # File never existed -> spot-check reopens it -> becomes this round's gap
    # -> build gets called for it.
    assert mock_build.call_count == 1
    acc = _acc(root)
    assert acc["criteria"][0]["status"] == "pending"
    assert "verified_by" not in acc["criteria"][0] or acc["criteria"][0].get("verified_by") is None
    escs = escalations.load(pcp_dir)
    assert any(e["route"] == "diff-reduce-reopen" for e in escs)


def test_spot_check_leaves_a_genuinely_complete_criterion_alone(tmp_path):
    root = _project(tmp_path, criteria=[
        {"id": "A001", "description": "charge endpoint", "check": "file_exists",
         "target": "src/f.py", "status": "complete", "verified_by": "pcp_build"},
    ])
    (root / "src").mkdir()
    (root / "src" / "f.py").write_text("x")
    pcp_dir = root / ".pcp"

    with patch("pcp.commands.build.build.callback") as mock_build:
        result = run_diff_reduce(pcp_dir, root, module_name="billing", yes=True)

    mock_build.assert_not_called()
    assert result["stopped_reason"] == "dry -- nothing open"
    acc = _acc(root)
    assert acc["criteria"][0]["status"] == "complete"


def test_concurrent_open_run_blocks_a_round(tmp_path):
    root = _project(tmp_path, criteria=[
        {"id": "A001", "description": "charge endpoint", "check": "manual", "status": "pending"},
    ])
    pcp_dir = root / ".pcp"
    run_log.start_run(pcp_dir, module="billing", feature="A001: charge endpoint",
                       run_type="dev", actor="someone-else")  # PRE only, never closed

    with patch("pcp.commands.build.build.callback") as mock_build:
        result = run_diff_reduce(pcp_dir, root, module_name="billing", yes=True)

    mock_build.assert_not_called()
    assert "another run is open" in result["stopped_reason"]


def test_new_scope_never_proposed_when_not_interactive(tmp_path):
    root = _project(tmp_path, criteria=[])
    pcp_dir = root / ".pcp"
    val_result = {
        "coverage_score": 0.2, "coverage_gaps": [{"area": "refunds", "quote": "must support refunds"}],
        "missing_modules": [],
    }
    with patch("pcp.commands.build.build.callback") as mock_build, \
         patch("pcp.commands.validate_strategy.run_validate_strategy", return_value=val_result), \
         patch("pcp.commands.pm.pm.callback") as mock_pm, \
         patch("sys.stdin.isatty", return_value=False):
        result = run_diff_reduce(pcp_dir, root, yes=True)

    mock_pm.assert_not_called()
    mock_build.assert_not_called()
    assert result["gap_still_open"] is True  # coverage gap real, just not acted on unattended


def test_coverage_gap_routed_through_pm_when_interactive(tmp_path):
    root = _project(tmp_path, criteria=[])
    pcp_dir = root / ".pcp"
    val_result_first = {
        "coverage_score": 0.2, "coverage_gaps": [{"area": "refunds", "quote": "must support refunds"}],
        "missing_modules": [],
    }
    val_result_after = {"coverage_score": 1.0, "coverage_gaps": [], "missing_modules": []}
    call_count = {"n": 0}

    def _fake_validate(pcp_dir_arg, command="validate-strategy"):
        call_count["n"] += 1
        return val_result_first if call_count["n"] == 1 else val_result_after

    with patch("pcp.commands.build.build.callback") as mock_build, \
         patch("pcp.commands.validate_strategy.run_validate_strategy", side_effect=_fake_validate), \
         patch("pcp.commands.pm.pm.callback") as mock_pm, \
         patch("sys.stdin.isatty", return_value=True):
        result = run_diff_reduce(pcp_dir, root, yes=True)

    mock_pm.assert_called_once()
    assert "refunds" in mock_pm.call_args.kwargs["intent"]
    # No new pending criteria actually landed (pm mocked, doesn't write) --
    # loop still stops cleanly rather than spinning on it.
    mock_build.assert_not_called()


def test_freshness_drift_mid_round_aborts_and_escalates(tmp_path):
    root = _project(tmp_path, criteria=[
        {"id": "A001", "description": "charge endpoint", "check": "manual", "status": "pending"},
    ])
    pcp_dir = root / ".pcp"

    def _fake_build(module_name=None, project_path=None, yes=False):
        # Simulate a concurrent human edit to this module's spec mid-build --
        # doesn't complete the criterion, just perturbs the file.
        spec_path = root / ".pcp" / "strategy" / "modules" / "billing" / "spec.yaml"
        spec = yaml.safe_load(spec_path.read_text())
        spec["description"] = "changed mid-round by someone else"
        spec_path.write_text(yaml.dump(spec, default_flow_style=False))

    with patch("pcp.commands.build.build.callback", side_effect=_fake_build) as mock_build:
        result = run_diff_reduce(pcp_dir, root, module_name="billing", yes=True, max_rounds=3)

    assert mock_build.call_count == 1  # aborted after round 1, never reached round 2
    assert "changed mid-round" in result["stopped_reason"]
    escs = escalations.load(pcp_dir)
    assert any(e["route"] == "diff-reduce-drift" for e in escs)
