"""pcp ontology-serve — local-only live dashboard for reviewing the ontology
draft with click-to-approve/reject/edit, writing directly to
ontology_state.yaml via the same apply_review_action() the CLI uses (see
ontology.py) — one implementation, two callers, so the CLI and the live
dashboard can never drift apart on what an action does.

Binds to 127.0.0.1 only — never exposed to the network. No auth needed
since it's loopback-only, the same trust boundary as any other local dev
tool (a Jupyter notebook, a local dev server) left running on your own
machine — not meant to be run on a shared/multi-user host.
"""

import json
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import click
import yaml
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, get_ontology_state, NoPCPDir
from pcp.ontology import to_display_items, apply_review_action, ReviewError

console = Console()

GRAPH_TEMPLATE_PATH = Path(__file__).parent.parent / "templates" / "ontology_graph.html"
TABLE_TEMPLATE_PATH = Path(__file__).parent.parent / "templates" / "ontology_dashboard.html"


def _make_handler(pcp_dir: Path, project_name: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # quiet — rich console gives its own status line

        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, path: Path) -> None:
            html = path.read_text().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def do_GET(self):
            if self.path in ("/", ""):
                self._send_html(GRAPH_TEMPLATE_PATH)
            elif self.path == "/table":
                self._send_html(TABLE_TEMPLATE_PATH)
            elif self.path == "/api/state":
                state_path = get_ontology_state(pcp_dir)
                if not state_path.exists():
                    self._send_json(200, {"generated_at": None, "project_name": project_name, "items": []})
                    return
                state = yaml.safe_load(state_path.read_text()) or {}
                self._send_json(200, {
                    "generated_at": state.get("generated_at"),
                    "project_name": project_name,
                    "items": to_display_items(state),
                })
            elif self.path == "/api/graph":
                state_path = get_ontology_state(pcp_dir)
                if not state_path.exists():
                    self._send_json(200, {"project_name": project_name, "nodes": [], "edges": []})
                    return
                state = yaml.safe_load(state_path.read_text()) or {}
                items = to_display_items(state)
                self._send_json(200, {
                    "project_name": project_name,
                    "nodes": [i for i in items if i["kind"] == "node"],
                    "edges": [i for i in items if i["kind"] == "edge"],
                })
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/api/review":
                self._send_json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._send_json(400, {"error": "invalid JSON body"})
                return

            item_id = payload.get("id")
            action = payload.get("action")
            new_label = payload.get("new_label")
            if not item_id or action not in ("approve", "reject", "edit"):
                self._send_json(400, {"error": "id and action (approve/reject/edit) required"})
                return

            try:
                result = apply_review_action(pcp_dir, item_id, action, new_label)
            except ReviewError as e:
                self._send_json(400, {"error": str(e)})
                return

            self._send_json(200, result)

    return Handler


@click.command()
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root (default: cwd, walks up to find .pcp/).")
@click.option("--port", default=8420, help="Port to listen on (default: 8420).")
@click.option("--no-open", "no_open", is_flag=True, help="Don't auto-open the browser.")
def ontology_serve(project_path: str | None, port: int, no_open: bool):
    """Local-only live dashboard for reviewing the ontology draft — click
    Approve/Reject/Edit, writes directly to ontology_state.yaml. Binds to
    127.0.0.1 only, never exposed to the network."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    if not GRAPH_TEMPLATE_PATH.exists() or not TABLE_TEMPLATE_PATH.exists():
        console.print(f"[red]Error:[/red] template missing under {GRAPH_TEMPLATE_PATH.parent}")
        sys.exit(2)

    project_name = pcp_dir.parent.name
    handler = _make_handler(pcp_dir, project_name)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{port}/"

    console.print(f"[green]pcp ontology-serve[/green] listening on [bold]{url}[/bold] (127.0.0.1 only)")
    console.print(f"[dim]Graph view (default): {url}  ·  Searchable table: {url}table[/dim]")
    console.print("[dim]Ctrl+C to stop.[/dim]")

    if not no_open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")
        server.shutdown()
