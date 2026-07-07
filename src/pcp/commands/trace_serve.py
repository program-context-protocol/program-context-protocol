"""pcp trace-serve — local-only catalog+detail view for reviewing the
feature-to-module traceability map: which code implements which BRD item.

Catalog-first, not graph-first (Palantir Ontology Manager's actual pattern,
confirmed by research, not guessed): a searchable list of features is the
primary surface; clicking one opens a detail panel with suggested module
matches and, for approved links, that module's concrete acceptance
criteria. No force-directed canvas — every row here is a feature, module,
or criterion by construction, not a parsed entity that might be noise.

Binds to 127.0.0.1 only — same trust boundary as pcp ontology-serve.
"""

import json
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import click
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, NoPCPDir
from pcp.traceability import build_full_view, apply_review_action, TraceabilityError

console = Console()

TEMPLATE_PATH = Path(__file__).parent.parent / "templates" / "trace_map.html"


def _make_handler(pcp_dir: Path, project_name: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def _send_json(self, status: int, payload) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", ""):
                html = TEMPLATE_PATH.read_text().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
            elif self.path == "/api/trace":
                data = build_full_view(pcp_dir)
                data["project_name"] = project_name
                self._send_json(200, data)
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/api/trace-review":
                self._send_json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._send_json(400, {"error": "invalid JSON body"})
                return

            link_id = payload.get("id")
            action = payload.get("action")
            new_label = payload.get("new_label")
            if not link_id or action not in ("approve", "reject", "edit"):
                self._send_json(400, {"error": "id and action (approve/reject/edit) required"})
                return

            try:
                result = apply_review_action(pcp_dir, link_id, action, new_label)
            except TraceabilityError as e:
                self._send_json(400, {"error": str(e)})
                return

            self._send_json(200, result)

    return Handler


@click.command()
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root (default: cwd, walks up to find .pcp/).")
@click.option("--port", default=8421, help="Port to listen on (default: 8421).")
@click.option("--no-open", "no_open", is_flag=True, help="Don't auto-open the browser.")
def trace_serve(project_path: str | None, port: int, no_open: bool):
    """Local-only catalog view for reviewing the feature-to-module
    traceability map. Binds to 127.0.0.1 only."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    if not TEMPLATE_PATH.exists():
        console.print(f"[red]Error:[/red] template missing at {TEMPLATE_PATH}")
        sys.exit(2)

    project_name = pcp_dir.parent.name
    handler = _make_handler(pcp_dir, project_name)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{port}/"

    console.print(f"[green]pcp trace-serve[/green] listening on [bold]{url}[/bold] (127.0.0.1 only)")
    console.print("[dim]Ctrl+C to stop.[/dim]")

    if not no_open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")
        server.shutdown()
