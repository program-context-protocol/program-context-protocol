"""pcp domain-serve — PCP's own 5-object domain model (Objective, Module,
Criterion, Requirement, Gate). Mostly a read-only view -- most facts here
are already asserted by a structured file, nothing to approve/reject.

Two things ARE editable through this UI, deliberately scoped narrow:
Module/Requirement descriptions (writes straight to spec.yaml/
brd_items.yaml -- a human editing through this interface is exactly as
legitimate as editing the file directly), and proposing a Requirement-to-
Module link by connecting two nodes (creates an unreviewed "blue" link
through the same traceability review path as the LLM classifier -- never
writes a confirmed edge directly, a human still approves it). Adding a
brand-new Module isn't supported here -- that needs the same scaffolding
`pcp init --module` does, a separate, bigger piece of work.

Binds to 127.0.0.1 only.
"""

import json
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import click
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, NoPCPDir
from pcp.domain_model import build_domain_model
from pcp.traceability import (
    propose_link, update_module_description, update_requirement_description,
    create_requirement, apply_review_action, TraceabilityError,
)

console = Console()

TEMPLATE_PATH = Path(__file__).parent.parent / "templates" / "domain_model.html"


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
            elif self.path == "/api/model":
                data = build_domain_model(pcp_dir)
                data["project_name"] = project_name
                self._send_json(200, data)
            else:
                self._send_json(404, {"error": "not found"})

        def _read_json_body(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            return json.loads(self.rfile.read(length) or b"{}")

        def do_POST(self):
            try:
                payload = self._read_json_body()
            except json.JSONDecodeError:
                self._send_json(400, {"error": "invalid JSON body"})
                return

            try:
                if self.path == "/api/model/edit-text":
                    kind = payload.get("kind")
                    entity_id = payload.get("id")
                    text = payload.get("text", "")
                    if kind == "module":
                        update_module_description(pcp_dir, entity_id, text)
                    elif kind == "requirement":
                        update_requirement_description(pcp_dir, entity_id, text)
                    else:
                        self._send_json(400, {"error": "kind must be 'module' or 'requirement'"})
                        return
                    self._send_json(200, {"ok": True})

                elif self.path == "/api/model/add-requirement":
                    description = payload.get("description", "").strip()
                    if not description:
                        self._send_json(400, {"error": "description required"})
                        return
                    result = create_requirement(pcp_dir, description)
                    self._send_json(200, result)

                elif self.path == "/api/model/propose-link":
                    feature_id = payload.get("feature_id")
                    module = payload.get("module")
                    if not feature_id or not module:
                        self._send_json(400, {"error": "feature_id and module required"})
                        return
                    result = propose_link(pcp_dir, feature_id, module)
                    self._send_json(200, result)

                elif self.path == "/api/model/review-link":
                    link_id = payload.get("id")
                    action = payload.get("action")
                    if not link_id or action not in ("approve", "reject"):
                        self._send_json(400, {"error": "id and action (approve/reject) required"})
                        return
                    result = apply_review_action(pcp_dir, link_id, action)
                    self._send_json(200, result)

                else:
                    self._send_json(404, {"error": "not found"})
            except TraceabilityError as e:
                self._send_json(400, {"error": str(e)})

    return Handler


@click.command()
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root (default: cwd, walks up to find .pcp/).")
@click.option("--port", default=8422, help="Port to listen on (default: 8422).")
@click.option("--no-open", "no_open", is_flag=True, help="Don't auto-open the browser.")
def domain_serve(project_path: str | None, port: int, no_open: bool):
    """Read-only view of PCP's 5-object domain model. Binds to 127.0.0.1 only."""
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

    console.print(f"[green]pcp domain-serve[/green] listening on [bold]{url}[/bold] (127.0.0.1 only)")
    console.print("[dim]Ctrl+C to stop.[/dim]")

    if not no_open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")
        server.shutdown()
