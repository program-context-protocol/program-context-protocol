"""pcp build-status — live view of an in-progress `pcp build` run.

Reads `.pcp/build_progress.yaml`, written by build.py's `_write_progress()`
at each criterion-attempt checkpoint (coding / evaluating gates / done /
failed). Real gap this closes (2026-07-24, ontology-foundry incident):
`pcp build` gives no way to tell what it's currently doing short of reading
raw agent output -- backgrounded (`nohup pcp build ... &`) it's fully
opaque, which is what triggered "i want to see whats happening" and a build
getting killed over what looked like a stall.
"""

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, NoPCPDir

console = Console()

STALE_AFTER_SEC = 600  # a step with no update in 10 min is worth flagging


def load_progress(pcp_dir: Path) -> dict | None:
    path = pcp_dir / "build_progress.yaml"
    if not path.exists():
        return None
    try:
        return yaml.safe_load(path.read_text()) or None
    except yaml.YAMLError:
        return None


def format_status(data: dict | None, now: datetime | None = None) -> str:
    """Pure formatting -- testable without a clock or filesystem."""
    if not data:
        return "No build in progress (no .pcp/build_progress.yaml yet)."

    now = now or datetime.now(timezone.utc)
    line = (
        f"{data.get('module', '?')}/{data.get('criterion_id', '?')} "
        f"attempt {data.get('attempt', '?')} — {data.get('step', '?')}"
    )
    updated_at = data.get("updated_at")
    if updated_at:
        try:
            ts = datetime.strptime(updated_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            age_sec = (now - ts).total_seconds()
            line += f"  (updated {int(age_sec)}s ago)"
            if age_sec > STALE_AFTER_SEC and data.get("step") not in ("done", "failed"):
                line += "  ⚠ no update in a while — may be stuck"
        except ValueError:
            pass
    return line


@click.command(name="build-status")
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root (default: cwd, walks up to find .pcp/).")
@click.option("--watch", is_flag=True, help="Poll and redraw every --interval seconds until Ctrl-C.")
@click.option("--interval", type=int, default=5, help="Poll interval in seconds for --watch (default 5).")
def build_status(project_path: str | None, watch: bool, interval: int):
    """Show what an in-progress `pcp build` run is currently doing."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    if not watch:
        console.print(format_status(load_progress(pcp_dir)))
        sys.exit(0)

    try:
        while True:
            console.print(format_status(load_progress(pcp_dir)))
            time.sleep(interval)
    except KeyboardInterrupt:
        sys.exit(0)
