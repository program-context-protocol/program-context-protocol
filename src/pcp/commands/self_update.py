"""`pcp self-update` -- explicit, human-run, git-pull-based update.

Deliberately NOT the shape removed as a launch blocker on 2026-07-27: that
cron silently curled a mutable file off raw.githubusercontent.com and
overwrote an agent instruction file with no signature, no hash check, no
human trigger. This command is the opposite on every axis: only runs when
a human types it, only touches a real git checkout (refuses cleanly
otherwise), only ever fast-forwards (never clobbers local changes), and
reports exactly what changed -- same shape Project P landed on for its own
agent self-update (`git pull --ff-only`, see decision_git_pull_self_update).

`--check` is the announce-only half, added for the SessionStart hook
(.pcp/hooks/session_update_check.py, scaffolded by `pcp init`): a project
opening a session can tell you an update exists without ever pulling one
byte on its own. Actually updating always stays a separate, explicit
`pcp self-update` invocation.
"""
import subprocess
from pathlib import Path

import click
from rich.console import Console

from pcp import version_drift

console = Console()


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _resolve_checkout() -> tuple[Path | None, str | None]:
    """Returns (root, error_message). error_message is None on success."""
    root = version_drift.source_root()
    if root is None:
        return None, (
            "No git checkout found for this install. self-update only works on a "
            "git-cloned source tree (set PCP_SOURCE_ROOT if this one lives somewhere non-standard)."
        )
    if not (root / ".git").exists():
        return None, f"{root} is not a git repository. Cannot self-update."
    return root, None


def check_for_update() -> dict:
    """Read-only: fetches from origin and reports whether the local checkout
    is behind, without touching the working tree. Never pulls."""
    root, err = _resolve_checkout()
    if root is None:
        return {"status": "unavailable", "message": err}

    fetch = _git(["fetch", "--quiet"], root)
    if fetch.returncode != 0:
        return {"status": "unknown", "message": f"git fetch failed: {(fetch.stderr or fetch.stdout).strip()}"}

    behind = _git(["rev-list", "--count", "HEAD..@{u}"], root)
    if behind.returncode != 0:
        # No upstream tracking branch configured -- can't tell.
        return {"status": "unknown", "message": "No upstream tracking branch configured."}

    count = int(behind.stdout.strip() or "0")
    if count == 0:
        return {"status": "current", "message": "PCP is up to date."}

    current = version_drift.source_version(root) or "unknown"
    return {
        "status": "behind", "commits_behind": count, "current_version": current,
        "message": f"PCP update available ({count} commit(s) behind) -- run `pcp self-update`.",
    }


@click.command(name="self-update")
@click.option("--check", "check_only", is_flag=True,
              help="Report whether an update is available without pulling it.")
def self_update(check_only: bool):
    """Update this PCP install via `git pull --ff-only` on its own checkout."""
    if check_only:
        result = check_for_update()
        console.print(result["message"])
        raise SystemExit(0 if result["status"] != "unavailable" else 1)

    root, err = _resolve_checkout()
    if root is None:
        console.print(f"[red]{err}[/red]")
        raise SystemExit(1)

    status = _git(["status", "--porcelain"], root)
    if status.stdout.strip():
        console.print(
            f"[yellow]{root} has uncommitted changes.[/yellow] "
            "Commit or stash them first -- self-update refuses to pull over local edits."
        )
        raise SystemExit(1)

    before = version_drift.source_version(root) or "unknown"
    console.print(f"Current source version: {before}")
    console.print("Running `git pull --ff-only`...")

    pull = _git(["pull", "--ff-only"], root)
    if pull.returncode != 0:
        console.print("[red]git pull --ff-only failed:[/red]")
        console.print(pull.stdout + pull.stderr)
        console.print(
            "[dim]Common cause: local commits diverged from origin. "
            "Resolve manually (rebase/merge) -- self-update will not force it.[/dim]"
        )
        raise SystemExit(1)

    after = version_drift.source_version(root) or "unknown"
    if pull.stdout.strip():
        console.print(pull.stdout.strip())

    if before == after:
        console.print(f"[green]Already up to date[/green] ({after}).")
        return

    console.print(f"[green]Updated {before} -> {after}.[/green]")
    if version_drift.is_editable():
        console.print("Editable install -- code is live now, no reinstall needed.")
    else:
        console.print(
            f"[yellow]This is a wheel install[/yellow] -- source is updated at {root}, "
            f"but reinstall to pick it up: `pip install -e {root}`."
        )
