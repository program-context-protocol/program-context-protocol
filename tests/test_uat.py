import http.server
import subprocess
import threading
from unittest.mock import patch, MagicMock

import pytest

from pcp import uat

try:
    import playwright  # noqa: F401
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


@pytest.fixture(scope="module")
def local_server():
    """A real local HTTP server -- these tests exercise real network I/O,
    not mocks, since url_responds/dom_contains are themselves thin I/O wrappers."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/ok":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"<html><body><h1>Welcome to the app</h1></body></html>")
            elif self.path == "/notfound":
                self.send_response(404)
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()

        def log_message(self, format, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


# ── url_responds ──

def test_url_responds_true_on_2xx(local_server):
    ok, detail = uat.check_url_responds(f"{local_server}/ok")
    assert ok is True
    assert "200" in detail


def test_url_responds_false_on_404(local_server):
    ok, detail = uat.check_url_responds(f"{local_server}/notfound")
    assert ok is False
    assert "404" in detail


def test_url_responds_false_on_unreachable_host():
    ok, detail = uat.check_url_responds("http://127.0.0.1:1/nope")
    assert ok is False
    assert "did not respond" in detail


def test_url_responds_false_when_no_url():
    ok, detail = uat.check_url_responds("")
    assert ok is False


# ── dom_contains ──

def test_dom_contains_true_on_literal_text_match(local_server):
    ok, detail = uat.check_dom_contains(f"{local_server}/ok", "Welcome to the app")
    assert ok is True
    assert "found in" in detail


def test_dom_contains_true_on_regex_match(local_server):
    ok, detail = uat.check_dom_contains(f"{local_server}/ok", r"<h1>.*app</h1>")
    assert ok is True
    assert "matched" in detail


def test_dom_contains_false_when_text_absent(local_server):
    ok, detail = uat.check_dom_contains(f"{local_server}/ok", "Goodbye")
    assert ok is False
    assert "not found" in detail


def test_dom_contains_false_when_no_selector(local_server):
    ok, detail = uat.check_dom_contains(f"{local_server}/ok", "")
    assert ok is False


def test_dom_contains_false_when_unreachable():
    ok, detail = uat.check_dom_contains("http://127.0.0.1:1/nope", "text")
    assert ok is False
    assert "did not respond" in detail


# ── check_visual ──

def test_check_visual_false_when_no_url():
    ok, detail = uat.check_visual("")
    assert ok is False


def test_check_visual_returns_none_when_playwright_not_installed(local_server):
    """None, not False -- 'could not check' must be distinguishable from
    'checked and it failed' so callers preserve prior status instead of
    downgrading a criterion just because an optional dependency is missing."""
    with patch.dict("sys.modules", {"playwright.sync_api": None}):
        ok, detail = uat.check_visual(f"{local_server}/ok")
    assert ok is None
    assert "not installed" in detail
    assert "[visual]" in detail


@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
def test_check_visual_true_on_real_render(local_server, tmp_path):
    screenshot_path = tmp_path / "shot.png"
    ok, detail = uat.check_visual(f"{local_server}/ok", screenshot_path)
    assert ok is True
    assert "rendered successfully" in detail
    assert screenshot_path.exists()
    assert screenshot_path.stat().st_size > 0


@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
def test_check_visual_false_on_unreachable_host():
    ok, detail = uat.check_visual("http://127.0.0.1:1/nope")
    assert ok is False
    assert "failed to render" in detail


@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
def test_check_visual_works_without_screenshot_path(local_server):
    ok, detail = uat.check_visual(f"{local_server}/ok", screenshot_path=None)
    assert ok is True
    assert "screenshot" not in detail


# ── check_axe ──

def test_check_axe_false_when_no_url():
    ok, detail = uat.check_axe("")
    assert ok is False


def test_check_axe_none_when_npx_not_installed():
    with patch("pcp.uat.shutil.which", return_value=None):
        ok, detail = uat.check_axe("http://example.com")
    assert ok is None
    assert "npx not found" in detail


def test_check_axe_true_on_clean_scan():
    with patch("pcp.uat.shutil.which", return_value="/usr/bin/npx"), \
         patch("pcp.uat.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="0 violations found!", stderr="")
        ok, detail = uat.check_axe("http://example.com")
    assert ok is True
    assert "no violations" in detail


def test_check_axe_false_on_violations():
    with patch("pcp.uat.shutil.which", return_value="/usr/bin/npx"), \
         patch("pcp.uat.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="3 violations found", stderr="")
        ok, detail = uat.check_axe("http://example.com")
    assert ok is False
    assert "violation" in detail


def test_check_axe_false_on_timeout():
    with patch("pcp.uat.shutil.which", return_value="/usr/bin/npx"), \
         patch("pcp.uat.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="npx", timeout=120)):
        ok, detail = uat.check_axe("http://example.com")
    assert ok is False
    assert "timed out" in detail


def test_check_axe_invokes_expected_cli_flags():
    with patch("pcp.uat.shutil.which", return_value="/usr/bin/npx"), \
         patch("pcp.uat.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        uat.check_axe("http://example.com/page")
    cmd = mock_run.call_args.args[0]
    assert cmd[:3] == ["npx", "--yes", "@axe-core/cli"]
    assert "http://example.com/page" in cmd
    assert "--exit" in cmd
    assert "--stdout" in cmd


# ── check_visual_quality ──

def test_check_visual_quality_none_when_no_screenshot(tmp_path):
    ok, detail, items = uat.check_visual_quality(tmp_path / "missing.png")
    assert ok is None
    assert items == []


def test_check_visual_quality_passes_screenshot_to_judge(tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"fake-png-bytes")
    verdict = {"items": [{"item": "layout ok", "passed": True, "reason": "looks fine"}], "overall_passed": True}
    with patch("pcp.llm.client.call_json_with_images", return_value=verdict) as mock_call:
        ok, detail, items = uat.check_visual_quality(shot)
    assert ok is True
    assert detail == "all checklist items passed"
    assert items == verdict["items"]
    image_paths = mock_call.call_args.args[2]
    assert image_paths == [shot]


def test_check_visual_quality_includes_reference_image_when_present(tmp_path):
    shot = tmp_path / "shot.png"
    ref = tmp_path / "ref.png"
    shot.write_bytes(b"fake-shot")
    ref.write_bytes(b"fake-ref")
    verdict = {"items": [], "overall_passed": True}
    with patch("pcp.llm.client.call_json_with_images", return_value=verdict) as mock_call:
        uat.check_visual_quality(shot, reference_image_path=ref)
    image_paths = mock_call.call_args.args[2]
    assert image_paths == [shot, ref]


def test_check_visual_quality_omits_missing_reference_image(tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"fake-shot")
    missing_ref = tmp_path / "does_not_exist.png"
    verdict = {"items": [], "overall_passed": True}
    with patch("pcp.llm.client.call_json_with_images", return_value=verdict) as mock_call:
        uat.check_visual_quality(shot, reference_image_path=missing_ref)
    image_paths = mock_call.call_args.args[2]
    assert image_paths == [shot]


def test_check_visual_quality_false_on_failed_item(tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"fake-shot")
    verdict = {
        "items": [{"item": "layout ok", "passed": False, "reason": "overlapping buttons"}],
        "overall_passed": False,
    }
    with patch("pcp.llm.client.call_json_with_images", return_value=verdict):
        ok, detail, items = uat.check_visual_quality(shot)
    assert ok is False
    assert "overlapping buttons" in detail


def test_check_visual_quality_none_when_judge_call_errors(tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"fake-shot")
    with patch("pcp.llm.client.call_json_with_images", side_effect=RuntimeError("boom")):
        ok, detail, items = uat.check_visual_quality(shot)
    assert ok is None
    assert "boom" in detail
    assert items == []
