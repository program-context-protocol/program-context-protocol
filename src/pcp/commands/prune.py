"""pcp prune — retention/pruning for .pcp/evidence/ and .pcp/transcripts/.

Both grow unboundedly (CLAUDE.md names this explicitly: full raw QA proof
on every check, gzip-archived session transcripts on every pcp capture
call, no retention policy). Deliberately scoped to these two directories
only, NOT telemetry.jsonl/decision_log.jsonl/bypass_log.yaml -- those are
hash-chained (evidence_chain.py) and pruning entries out of the front of a
hash chain breaks verify_chain() at the new first record unless the
pruning routine re-anchors the chain with an explicit checkpoint, a bigger
change than this pass takes on. evidence/transcripts are plain files with
no chain to preserve, so they're the safe, high-value target: the actual
disk-bloat culprits (full test-suite output, full judge responses, full
session transcripts) rather than telemetry.jsonl's small per-record JSON
lines.

Deletion, not archival -- an irreversible action, so it gets the same
posture pcp deploy already established as this project's one other
genuinely irreversible action: mandatory interactive confirmation by
default, --yes opts out.

A deleted evidence file leaves a dangling evidence_path reference in
telemetry.jsonl -- dashboard.py already just renders that as a link
without checking existence, a known pre-existing gap this doesn't
introduce or fix, just doesn't make worse.
"""

import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, NoPCPDir
from pcp.schema.validator import load_yaml

console = Console()

PRUNE_LOG = "prune_log.yaml"


def _retention_config(pcp_dir: Path) -> dict:
    ci_rules_path = pcp_dir / "ci_rules.yaml"
    if not ci_rules_path.exists():
        return {}
    return (load_yaml(ci_rules_path) or {}).get("retention") or {}


def _age_days(path: Path, now: datetime) -> float:
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return (now - mtime).total_seconds() / 86400


def _find_stale_evidence(pcp_dir: Path, evidence_days: int, now: datetime) -> list[Path]:
    evidence_dir = pcp_dir / "evidence"
    if not evidence_dir.exists():
        return []
    return [
        p for p in evidence_dir.rglob("*.txt")
        if p.is_file() and _age_days(p, now) > evidence_days
    ]


def _find_stale_transcripts(pcp_dir: Path, transcript_days: int, now: datetime) -> list[Path]:
    transcripts_dir = pcp_dir / "transcripts"
    if not transcripts_dir.exists():
        return []
    return [
        p for p in transcripts_dir.glob("*.jsonl.gz")
        if p.is_file() and _age_days(p, now) > transcript_days
    ]


def _log_prune_run(pcp_dir: Path, evidence_days: int | None, transcript_days: int | None,
                    files_removed: int, bytes_reclaimed: int) -> None:
    """Plain append list, not hash-chained -- this is an operational
    maintenance record (what a config-driven cleanup pass did), not a
    security/audit control checkpoint the way telemetry.jsonl/bypass_log.yaml
    are. Scope kept narrow deliberately, same "phase 1, not the finished
    thing" posture as pcp docs's drift score."""
    log_path = pcp_dir / PRUNE_LOG
    existing = []
    if log_path.exists():
        existing = (yaml.safe_load(log_path.read_text()) or {}).get("runs", [])
    existing.append({
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "evidence_days": evidence_days,
        "transcript_days": transcript_days,
        "files_removed": files_removed,
        "bytes_reclaimed": bytes_reclaimed,
    })
    log_path.write_text(yaml.dump({"runs": existing}, default_flow_style=False))


def run_prune(pcp_dir: Path, evidence_days: int | None, transcript_days: int | None,
              now: datetime | None = None) -> dict:
    """Pure discovery + deletion, no prompting -- the CLI command owns the
    confirmation step. Returns a summary dict; always safe to call with
    both days=None (finds nothing, deletes nothing)."""
    now = now or datetime.now(timezone.utc)
    stale_evidence = _find_stale_evidence(pcp_dir, evidence_days, now) if evidence_days else []
    stale_transcripts = _find_stale_transcripts(pcp_dir, transcript_days, now) if transcript_days else []
    all_stale = stale_evidence + stale_transcripts
    total_bytes = sum(p.stat().st_size for p in all_stale)

    return {
        "evidence_files": stale_evidence,
        "transcript_files": stale_transcripts,
        "total_files": len(all_stale),
        "total_bytes": total_bytes,
    }


def _delete_files(paths: list[Path]) -> None:
    for p in paths:
        p.unlink(missing_ok=True)


@click.command()
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root (default: cwd, walks up to find .pcp/).")
@click.option("--evidence-days", type=int, default=None,
              help="Delete .pcp/evidence/*.txt older than N days (overrides ci_rules.yaml's retention.evidence_days).")
@click.option("--transcript-days", type=int, default=None,
              help="Delete .pcp/transcripts/*.jsonl.gz older than N days (overrides ci_rules.yaml's retention.transcript_days).")
@click.option("--dry-run", is_flag=True, help="Show what would be deleted, delete nothing.")
@click.option("--yes", is_flag=True, help="Skip the interactive confirmation prompt (CI/non-interactive use).")
def prune(project_path: str | None, evidence_days: int | None, transcript_days: int | None,
          dry_run: bool, yes: bool):
    """Delete stale raw QA evidence / session transcripts past a retention window.

    No retention happens unless configured -- via --evidence-days/
    --transcript-days here, or a `retention:` block in ci_rules.yaml. A bare
    `pcp prune` with nothing configured does nothing and says so."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    config = _retention_config(pcp_dir)
    evidence_days = evidence_days if evidence_days is not None else config.get("evidence_days")
    transcript_days = transcript_days if transcript_days is not None else config.get("transcript_days")

    if not evidence_days and not transcript_days:
        console.print(
            "[dim]No retention configured -- nothing to do. Pass --evidence-days/--transcript-days, "
            "or add a `retention:` block to .pcp/ci_rules.yaml.[/dim]"
        )
        sys.exit(0)

    result = run_prune(pcp_dir, evidence_days, transcript_days)
    if result["total_files"] == 0:
        console.print("[green]Nothing past the retention window.[/green]")
        sys.exit(0)

    mb = result["total_bytes"] / (1024 * 1024)
    console.print(
        f"[bold]{result['total_files']} file(s)[/bold] past retention "
        f"({len(result['evidence_files'])} evidence, {len(result['transcript_files'])} transcripts), "
        f"~{mb:.1f} MB."
    )

    if dry_run:
        console.print("[dim]--dry-run: nothing deleted.[/dim]")
        sys.exit(0)

    if not yes:
        prompt = f"Permanently delete these {result['total_files']} file(s)?"
        if not click.confirm(prompt, default=False):
            console.print("[yellow]Prune aborted.[/yellow]")
            sys.exit(0)

    _delete_files(result["evidence_files"])
    _delete_files(result["transcript_files"])
    _log_prune_run(pcp_dir, evidence_days, transcript_days, result["total_files"], result["total_bytes"])
    console.print(f"[green]✓[/green] Deleted {result['total_files']} file(s), reclaimed ~{mb:.1f} MB.")
