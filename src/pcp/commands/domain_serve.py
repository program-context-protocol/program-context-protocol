"""pcp domain-serve — read-only view of PCP's own 5-object domain model
(Objective, Module, Criterion, Requirement, Gate). No review workflow --
every fact here is already asserted by a structured file, nothing is an
uncertain claim needing approve/reject. Binds to 127.0.0.1 only.
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

console = Console()

TEMPLATE_PATH = Path(__file__).parent.parent / "templates" / "domain_model.html"


def _make_handler(pcp_dir: Path, project_name: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

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
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

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
