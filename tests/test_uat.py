import http.server
import threading

import pytest

from pcp import uat


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
