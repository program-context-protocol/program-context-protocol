"""pcp watch — continuous CI/deploy monitoring.

Polls every N seconds (default 270, per docs/greenfield.md) for a new CI run
result and deploy health. On failure: fetches logs, spawns a fresh coding
agent to diagnose+fix+commit+push, notifies via slack-notify if available.
The fix isn't verified here — the next poll cycle re-checks CI for it, same
as a human would push a fix and watch CI pick it up.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import click
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, NoPCPDir
from pcp.commands.doctor import load_integrations, check_environment
from pcp.llm.client import _claude_bin

console = Console()

DEFAULT_INTERVAL = 270


def _default_max_iterations() -> int:
    """Overall poll-loop ceiling — a daemon left running for days should stop
    on its own, not rely on someone remembering it's up. Default ~15h at the
    default 270s interval. Override with PCP_WATCH_MAX_ITERATIONS."""
    return int(os.environ.get("PCP_WATCH_MAX_ITERATIONS", "200"))


def _max_consecutive_auto_fix_attempts() -> int:
    """Regression-loop breaker: if auto-fix keeps getting attempted without a
    CI success in between, a fresh agent is likely re-diagnosing from scratch
    each time and could be fixing X by breaking Y, then fixing Y by breaking X.
    After this many consecutive attempts with no success, stop auto-fixing
    and just report — a human needs to look. Override with
    PCP_WATCH_MAX_CONSECUTIVE_FIXES."""
    return int(os.environ.get("PCP_WATCH_MAX_CONSECUTIVE_FIXES", "3"))


def get_latest_ci_run(project_root: Path) -> dict | None:
    if not shutil.which("gh"):
        return None
    try:
        result = subprocess.run(
            ["gh", "run", "list", "--limit", "1", "--json", "status,conclusion,databaseId,headBranch,name,url"],
            capture_output=True, text=True, cwd=project_root, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    try:
        runs = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return runs[0] if runs else None


def get_failed_logs(project_root: Path, run_id) -> str:
    try:
        result = subprocess.run(
            ["gh", "run", "view", str(run_id), "--log-failed"],
            capture_output=True, text=True, cwd=project_root, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return "(timed out fetching logs)"
    return (result.stdout + result.stderr)[-8000:]


def check_deploy_health(health_url: str | None) -> bool | None:
    """True/False if checked, None if no URL configured."""
    if not health_url:
        return None
    try:
        import urllib.request
        req = urllib.request.Request(health_url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def notify(message: str) -> None:
    if shutil.which("slack-notify"):
        try:
            subprocess.run(["slack-notify", message], capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired:
            pass
    console.print(f"[dim]Notify: {message}[/dim]")


def attempt_auto_fix(pcp_dir: Path, failure_context: str) -> bool:
    """Spawn a one-off claude -p session instructed to diagnose+fix+commit+push.
    Returns True if the agent ran without a process-level error — not a guarantee
    the fix worked, the next poll cycle re-checks CI for that."""
    prompt = (
        "A CI run failed on this repository. Diagnose the failure from the log below, "
        "fix the underlying issue, commit, and push to the current branch. "
        "Follow this project's CLAUDE.md and ci_rules.yaml.\n\n"
        f"## CI Failure Log\n```\n{failure_context}\n```\n"
    )
    cmd = [_claude_bin(), "-p", "--permission-mode", "acceptEdits", "--output-format", "json"]
    build_model = os.environ.get("PCP_BUILD_MODEL")
    if build_model:
        cmd += ["--model", build_model]
    try:
        result = subprocess.run(
            cmd, input=prompt, text=True, capture_output=True, cwd=pcp_dir.parent, timeout=600,
        )
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


@click.command()
@click.option("--path", "project_path", type=click.Path(), default=None)
@click.option("--interval", default=DEFAULT_INTERVAL, show_default=True, help="Poll interval in seconds.")
@click.option("--once", is_flag=True, help="Single pass, no loop/sleep — for testing or one-shot checks.")
@click.option("--max-iterations", default=None, type=int, help="Stop after N polls (default: PCP_WATCH_MAX_ITERATIONS env or 200).")
def watch(project_path: str | None, interval: int, once: bool, max_iterations: int | None):
    """Continuously monitor CI + deploy health, auto-diagnose and fix failures."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    project_root = pcp_dir.parent
    check_environment(pcp_dir, fatal_on_missing_required=False)
    integrations = load_integrations(pcp_dir)
    health_url = (integrations.get("deploy") or {}).get("health_check_url")

    if not shutil.which("gh") and not health_url:
        console.print("[yellow]Neither `gh` CLI nor a configured health-check URL found — nothing to watch.[/yellow]")
        console.print("[dim]Run `pcp doctor` to configure a deploy health-check URL.[/dim]")
        sys.exit(0)

    max_iterations = max_iterations or _default_max_iterations()
    max_consecutive_fixes = _max_consecutive_auto_fix_attempts()
    last_seen_run_id = None
    iteration = 0
    consecutive_fix_attempts = 0
    auto_fix_disabled = False

    while True:
        iteration += 1
        console.print(f"[dim]Watch poll #{iteration}/{max_iterations} ({time.strftime('%H:%M:%S')})...[/dim]")

        run = get_latest_ci_run(project_root)
        if run and run.get("databaseId") != last_seen_run_id:
            last_seen_run_id = run.get("databaseId")
            if run.get("status") == "completed" and run.get("conclusion") not in ("success", None):
                console.print(f"[red]CI run failed:[/red] {run.get('name')} — {run.get('url')}")
                if auto_fix_disabled:
                    notify(f"pcp watch: CI still failing after {max_consecutive_fixes} auto-fix attempts — auto-fix paused, needs human attention. {run.get('url')}")
                else:
                    logs = get_failed_logs(project_root, run["databaseId"])
                    console.print("[dim]Attempting auto-fix...[/dim]")
                    fixed = attempt_auto_fix(pcp_dir, logs)
                    consecutive_fix_attempts += 1
                    if fixed:
                        notify(f"pcp watch: auto-fix attempted for failed CI run {run.get('url')} — pushed, awaiting next CI result.")
                    else:
                        notify(f"pcp watch: CI run failed and auto-fix attempt errored — needs human attention. {run.get('url')}")
                    if consecutive_fix_attempts >= max_consecutive_fixes:
                        auto_fix_disabled = True
                        notify(
                            f"pcp watch: {consecutive_fix_attempts} consecutive auto-fix attempts without a CI success — "
                            "pausing auto-fix (possible regression loop). Still watching and reporting status."
                        )
            elif run.get("status") == "completed" and run.get("conclusion") == "success":
                console.print(f"[green]CI run succeeded:[/green] {run.get('name')}")
                consecutive_fix_attempts = 0
                auto_fix_disabled = False

        if health_url:
            healthy = check_deploy_health(health_url)
            if healthy is False:
                console.print(f"[red]Deploy health check FAILED:[/red] {health_url}")
                notify(f"pcp watch: deploy health check failing at {health_url}")
            elif healthy is True:
                console.print(f"[green]Deploy healthy:[/green] {health_url}")

        if once:
            break
        if iteration >= max_iterations:
            console.print(f"[yellow]Reached max iterations ({max_iterations}) — stopping. Override with --max-iterations or PCP_WATCH_MAX_ITERATIONS.[/yellow]")
            break
        time.sleep(interval)
