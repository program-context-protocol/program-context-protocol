"""Generic lazy-marker scan (CTRL-029), 2026-07-20 -- deterministic regex
scan of ALL changed files for TODO/FIXME/placeholder-style markers and stub
function bodies. Advisory only, never blocks."""

from pcp import telemetry
from pcp.commands.build import _run_lazy_marker_check


def _ctx(module="widgets", criterion_id="A001", attempt=1, files=None):
    return {"module": module, "submodule": None, "criterion_id": criterion_id,
            "attempt": attempt, "files": files or []}


def _qa_records(pcp_dir):
    return [r for r in telemetry.load(pcp_dir) if r.get("cycle") == "qa"]


def test_lazy_marker_check_clean_file_records_no_findings(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (tmp_path / "app.py").write_text("def render():\n    return compute_total()\n")
    _run_lazy_marker_check(pcp_dir, tmp_path, ["app.py"], _ctx())
    record = [r for r in _qa_records(pcp_dir) if r["check"] == "lazy-marker"][0]
    assert record["control_id"] == "CTRL-029"
    assert record["result"] == "pass"
    assert record["error_count"] == 0


def test_lazy_marker_check_flags_todo_marker(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (tmp_path / "app.py").write_text("def render():\n    # TODO: handle edge case\n    return None\n")
    _run_lazy_marker_check(pcp_dir, tmp_path, ["app.py"], _ctx())
    record = [r for r in _qa_records(pcp_dir) if r["check"] == "lazy-marker"][0]
    assert record["result"] == "block"
    assert any("lazy-marker hit" in e for e in record["errors"])


def test_lazy_marker_check_flags_stub_body(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (tmp_path / "app.py").write_text("def compute_total():\n    pass\n")
    _run_lazy_marker_check(pcp_dir, tmp_path, ["app.py"], _ctx())
    record = [r for r in _qa_records(pcp_dir) if r["check"] == "lazy-marker"][0]
    assert record["result"] == "block"
    assert any("stub function body" in e for e in record["errors"])


def test_lazy_marker_check_skips_test_files(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (tmp_path / "test_app.py").write_text("def test_x():\n    # TODO fix this test\n    pass\n")
    _run_lazy_marker_check(pcp_dir, tmp_path, ["test_app.py"], _ctx())
    record = [r for r in _qa_records(pcp_dir) if r["check"] == "lazy-marker"][0]
    assert record["result"] == "pass"


def test_lazy_marker_check_skips_missing_files(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _run_lazy_marker_check(pcp_dir, tmp_path, ["nonexistent.py"], _ctx())
    record = [r for r in _qa_records(pcp_dir) if r["check"] == "lazy-marker"][0]
    assert record["result"] == "pass"
