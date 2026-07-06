"""pcp capture — classify a session transcript into business-logic drift
(-> .pcp/brd.md) and technical input (-> .pcp/decision_log.jsonl).

Advisory, never blocks. Designed to be wired to a Claude Code SessionEnd hook
(reads the hook's JSON payload from stdin) for human/PM/UAT sessions; `pcp
build` calls the same underlying pcp.capture.run_capture() directly for its
own per-criterion agent sessions.
"""

import json
import sys
from pathlib import Path

import click
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, NoPCPDir
from pcp.capture import run_capture

console = Console()


@click.command()
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root (default: cwd, walks up to find .pcp/).")
@click.option("--transcript-file", type=click.Path(exists=True), default=None,
              help="Transcript JSONL to classify (manual/testing use). "
                   "Default: read a Claude Code SessionEnd hook payload from stdin.")
def capture(project_path: str | None, transcript_file: str | None):
    """Classify a session transcript into business/technical drift. Advisory, never blocks."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir:
        # Silent — this may run via a hook in any project, not just PCP-managed ones.
        sys.exit(0)

    transcript_path = None
    session_id = None

    if transcript_file:
        transcript_path = Path(transcript_file)
        # Claude Code transcripts are always named <session-id>.jsonl (same
        # convention pcp.capture.find_transcript_for_session relies on) — derive
        # it from the filename so manual/testing runs still get real traceability
        # instead of session_id=None / source="session:unknown".
        session_id = transcript_path.stem
    else:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        if raw.strip():
            try:
                payload = json.loads(raw)
                session_id = payload.get("session_id")
                tp = payload.get("transcript_path")
                if tp:
                    transcript_path = Path(tp)
            except json.JSONDecodeError:
                pass

    if not transcript_path or not transcript_path.exists():
        console.print("[dim]pcp capture: no transcript available, skipping.[/dim]")
        sys.exit(0)

    result = run_capture(pcp_dir, transcript_path, source=f"session:{session_id or 'unknown'}", session_id=session_id)

    if result.get("skipped"):
        console.print(f"[dim]pcp capture: {result['skipped']}[/dim]")
    else:
        console.print(
            f"[green]pcp capture:[/green] {result['business_count']} business item(s) -> .pcp/brd.md, "
            f"{result['technical_count']} technical item(s) -> .pcp/decision_log.jsonl"
        )
    sys.exit(0)
