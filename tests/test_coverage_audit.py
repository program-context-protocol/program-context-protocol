from pcp import coverage_audit


def test_record_appends_and_returns_no_findings_when_consistent(tmp_path):
    findings = coverage_audit.record(tmp_path, 0.5, [{"area": "x"}], "obj text", {"a": {"description": "x"}})
    assert findings == []
    records = coverage_audit.load(tmp_path)
    assert len(records) == 1
    assert records[0]["coverage_score"] == 0.5
    assert records[0]["gap_count"] == 1


def test_load_returns_empty_when_no_file(tmp_path):
    assert coverage_audit.load(tmp_path) == []


def test_flags_high_score_with_open_gaps_as_inconsistent(tmp_path):
    findings = coverage_audit.record(tmp_path, 0.9, [{"area": "missing feature"}], "obj", {"a": {}})
    assert len(findings) == 1
    assert "internally inconsistent" in findings[0]


def test_no_inconsistency_flag_when_score_low_with_gaps(tmp_path):
    findings = coverage_audit.record(tmp_path, 0.5, [{"area": "x"}], "obj", {"a": {}})
    assert findings == []


def test_no_inconsistency_flag_when_high_score_and_no_gaps(tmp_path):
    findings = coverage_audit.record(tmp_path, 0.95, [], "obj", {"a": {}})
    assert findings == []


def test_flags_drift_on_unchanged_inputs(tmp_path):
    objective = "Build a calculator."
    modules = {"add": {"description": "adds numbers"}}
    coverage_audit.record(tmp_path, 0.5, [{"area": "x"}], objective, modules)
    findings = coverage_audit.record(tmp_path, 0.9, [], objective, modules)
    assert len(findings) == 1
    assert "drifted" in findings[0]
    assert "50%" in findings[0] and "90%" in findings[0]


def test_no_drift_flag_when_inputs_changed(tmp_path):
    coverage_audit.record(tmp_path, 0.5, [{"area": "x"}], "objective v1", {"add": {"description": "a"}})
    findings = coverage_audit.record(tmp_path, 0.9, [], "objective v2 (changed)", {"add": {"description": "a"}})
    assert findings == []


def test_no_drift_flag_when_delta_below_threshold(tmp_path):
    objective = "Build a calculator."
    modules = {"add": {"description": "adds numbers"}}
    coverage_audit.record(tmp_path, 0.80, [], objective, modules)
    findings = coverage_audit.record(tmp_path, 0.85, [], objective, modules)
    assert findings == []


def test_drift_check_uses_most_recent_matching_run(tmp_path):
    objective = "Build a calculator."
    modules = {"add": {"description": "adds numbers"}}
    coverage_audit.record(tmp_path, 0.3, [], objective, modules)
    coverage_audit.record(tmp_path, 0.9, [], objective, modules)  # drift vs 0.3, flagged
    findings = coverage_audit.record(tmp_path, 0.95, [], objective, modules)  # small delta vs 0.9
    assert findings == []
