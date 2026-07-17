import http.server
import threading
from unittest.mock import patch

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
