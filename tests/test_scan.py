import http.server
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from pcp.commands.scan import (
    _check_ast_pattern, _check_file_exists, _evaluate_criterion, _SOURCE_FILES_CACHE, _FILE_CONTENT_CACHE,
    _scan_module, _write_current_state,
)


def _reset_caches():
    _SOURCE_FILES_CACHE.clear()
    _FILE_CONTENT_CACHE.clear()


def test_ast_pattern_found_at_declared_target(tmp_path):
    _reset_caches()
    (tmp_path / "auth.py").write_text("def login(): pass\n")

    ok, detail = _check_ast_pattern("auth.py", r"def login", tmp_path)

    assert ok is True
    assert "auth.py" in detail


def test_ast_pattern_falls_back_when_feature_moved_to_another_file(tmp_path):
    """Refactor absorbed the spec'd feature into a differently-named file.

    The exact `target` no longer contains the pattern, but the pattern
    exists elsewhere in the tree — scan should not false-negative this.
    """
    _reset_caches()
    (tmp_path / "postcar_check.py").write_text("def validate_registration(): pass\n")

    ok, detail = _check_ast_pattern("registration_check.py", r"def validate_registration", tmp_path)

    assert ok is True
    assert "postcar_check.py" in detail
    assert "registration_check.py" in detail


def test_ast_pattern_not_found_anywhere_stays_pending(tmp_path):
    _reset_caches()
    (tmp_path / "other.py").write_text("def unrelated(): pass\n")

    ok, detail = _check_ast_pattern("missing.py", r"def never_written", tmp_path)

    assert ok is False


def test_ast_pattern_target_pointing_at_a_directory_does_not_crash(tmp_path):
    """Real bug, found 2026-07-08 dogfooding against ontology-foundry: a
    criterion's target field pointed at a real directory, not a file.
    path.exists() is True for a directory too, so the old code tried to
    read_text() it and crashed the whole `pcp scan` run with an unhandled
    IsADirectoryError. Should fall through to the repo-wide fallback search
    instead, same as any other "not found at declared path" case."""
    _reset_caches()
    (tmp_path / "extractors").mkdir()
    (tmp_path / "other.py").write_text("def unrelated(): pass\n")

    ok, detail = _check_ast_pattern("extractors", r"def unrelated", tmp_path)

    assert ok is True  # found via the repo-wide fallback, not a crash
    assert "other.py" in detail


def test_file_exists_at_declared_path(tmp_path):
    _reset_caches()
    (tmp_path / "module.py").touch()

    ok, detail = _check_file_exists("module.py", tmp_path)

    assert ok is True


def test_file_exists_falls_back_to_moved_file(tmp_path):
    _reset_caches()
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "module.py").touch()

    ok, detail = _check_file_exists("module.py", tmp_path)

    assert ok is True
    assert "moved to" in detail


def test_file_exists_false_when_truly_absent(tmp_path):
    _reset_caches()

    ok, detail = _check_file_exists("nowhere.py", tmp_path)

    assert ok is False


@pytest.fixture(scope="module")
def local_server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<html><body>Welcome to the app</body></html>")

        def log_message(self, format, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_evaluate_criterion_url_responds_complete(local_server):
    criterion = {"id": "A001", "check": "url_responds", "url": f"{local_server}/health"}
    status, detail = _evaluate_criterion(criterion, "mod", Path("."), {}, {})
    assert status == "complete"


def test_evaluate_criterion_url_responds_pending_when_unreachable():
    criterion = {"id": "A001", "check": "url_responds", "url": "http://127.0.0.1:1/nope"}
    status, detail = _evaluate_criterion(criterion, "mod", Path("."), {}, {})
    assert status == "pending"


def test_evaluate_criterion_dom_contains_complete(local_server):
    criterion = {"id": "A001", "check": "dom_contains", "url": local_server, "selector": "Welcome to the app"}
    status, detail = _evaluate_criterion(criterion, "mod", Path("."), {}, {})
    assert status == "complete"


def test_evaluate_criterion_dom_contains_pending_when_text_absent(local_server):
    criterion = {"id": "A001", "check": "dom_contains", "url": local_server, "selector": "Goodbye"}
    status, detail = _evaluate_criterion(criterion, "mod", Path("."), {}, {})
    assert status == "pending"


def test_evaluate_criterion_visual_complete_on_real_render(local_server, tmp_path):
    pytest.importorskip("playwright")
    criterion = {"id": "A001", "check": "visual", "url": local_server}
    pcp_dir = tmp_path / ".pcp"
    status, detail = _evaluate_criterion(criterion, "mod", Path("."), {}, {}, pcp_dir)
    assert status == "complete"
    assert (pcp_dir / "evidence" / "_visual" / "mod" / "A001.png").exists()


def test_evaluate_criterion_visual_preserves_prior_status_when_playwright_missing(tmp_path):
    """Missing optional dependency must never downgrade a criterion that was
    already marked complete from a prior scan."""
    with patch.dict("sys.modules", {"playwright.sync_api": None}):
        criterion = {"id": "A001", "check": "visual", "url": "http://example.invalid", "status": "complete"}
        status, detail = _evaluate_criterion(criterion, "mod", Path("."), {}, {}, tmp_path / ".pcp")
    assert status == "complete"
    assert "not installed" in detail


# ── verified_by provenance (2026-07-24) ──

def test_scan_module_propagates_verified_by(tmp_path):
    import yaml
    acc_path = tmp_path / "acceptance.yaml"
    acc_path.write_text(yaml.dump({"criteria": [
        {"id": "A001", "description": "x", "check": "manual", "status": "complete", "verified_by": "pcp_build"},
    ]}))
    result = _scan_module("mod", acc_path, tmp_path, {})
    assert result["criteria"][0]["verified_by"] == "pcp_build"


def test_scan_module_verified_by_absent_when_never_stamped(tmp_path):
    import yaml
    acc_path = tmp_path / "acceptance.yaml"
    acc_path.write_text(yaml.dump({"criteria": [
        {"id": "A001", "description": "x", "check": "manual", "status": "complete"},
    ]}))
    result = _scan_module("mod", acc_path, tmp_path, {})
    assert result["criteria"][0]["verified_by"] is None


def test_current_state_md_marks_verified_criterion(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    modules_results = [{"module": "mod", "criteria": [
        {"id": "A001", "description": "x", "check": "manual", "status": "complete",
         "detail": "manual", "verified_by": "pcp_build"},
    ]}]
    out_path = _write_current_state(pcp_dir, modules_results, "2026-07-24T00:00:00Z")
    text = out_path.read_text()
    assert "[verified: pcp_build]" in text


def test_current_state_md_flags_unverified_completion(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    modules_results = [{"module": "mod", "criteria": [
        {"id": "A001", "description": "x", "check": "manual", "status": "complete", "detail": "manual", "verified_by": None},
    ]}]
    out_path = _write_current_state(pcp_dir, modules_results, "2026-07-24T00:00:00Z")
    text = out_path.read_text()
    assert "unverified — not marked complete by pcp build" in text


def test_current_state_md_pending_criterion_gets_no_verified_annotation(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    modules_results = [{"module": "mod", "criteria": [
        {"id": "A001", "description": "x", "check": "manual", "status": "pending", "detail": "manual", "verified_by": None},
    ]}]
    out_path = _write_current_state(pcp_dir, modules_results, "2026-07-24T00:00:00Z")
    text = out_path.read_text()
    assert "verified" not in text
