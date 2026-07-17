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
import uuid
from pathlib import Path

import click
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, NoPCPDir
from pcp.commands.doctor import load_integrations, check_environment
from pcp.commands.build import check_agent_depth_or_exit
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


def _watch_agent_max_budget_usd() -> str:
    """Per-attempt dollar cap passed to `claude -p --max-budget-usd` for
    watch's auto-fix agent -- same mechanism build.py already uses for its
    own coding-agent subprocess, applied here since this is the same shape of
    call and had no cap at all until now. Doesn't add a new retry pathway:
    an attempt that hits this cap without finishing is indistinguishable
    from any other incomplete attempt, and already falls under the existing
    PCP_WATCH_MAX_CONSECUTIVE_FIXES ceiling below -- it just bounds the cost
    of each individual attempt within that ceiling instead of leaving it open.
    Override with PCP_WATCH_AGENT_MAX_BUDGET_USD."""
    return os.environ.get("PCP_WATCH_AGENT_MAX_BUDGET_USD", "5")


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
    """Send via slack-notify if available; NEVER fail silently. A delivery
    failure downgrading to console-only without saying so is a lived incident
    class here (an SSL cert error once silently fell back to log-only and a
    security STOP sat unread for 8 days) — if delivery fails, say so loudly
    so the console record itself shows the human was probably NOT reached."""
    delivered = False
    if shutil.which("slack-notify"):
        try:
            result = subprocess.run(["slack-notify", message], capture_output=True, text=True, timeout=15)
            delivered = result.returncode == 0
            if not delivered:
                console.print(
                    f"[red bold]Notification delivery FAILED[/red bold] "
                    f"(slack-notify exit {result.returncode}: {(result.stderr or result.stdout).strip()[:200]}) "
                    "— message below reached the console ONLY, a human has likely not seen it."
                )
        except subprocess.TimeoutExpired:
            console.print(
                "[red bold]Notification delivery FAILED[/red bold] (slack-notify timed out) "
                "— message below reached the console ONLY, a human has likely not seen it."
            )
    console.print(f"[dim]Notify: {message}[/dim]")


def check_stale_escalations(pcp_dir: Path, already_reported: set) -> None:
    """Escalation-acknowledgment watchdog: an escalation recorded but never
    acted on (criterion still pending past PCP_ESCALATION_STALE_HOURS,
    default 24h) gets re-surfaced — recording an escalation is not the same
    fact as a human having seen it. Each stale entry is re-notified once per
    watch run, not once per poll (notification fatigue is its own failure
    mode: a team trained to ignore pings misses the real one)."""
    from pcp import escalations
    for e in escalations.find_stale(pcp_dir):
        key = (e.get("module"), e.get("criterion_id"), e.get("timestamp"))
        if key in already_reported:
            continue
        already_reported.add(key)
        msg = (
            f"pcp watch: STALE ESCALATION — {e.get('module')}/{e.get('criterion_id')} "
            f"escalated {e.get('age_hours')}h ago and its criterion is still pending. "
            "No human appears to have acted on it."
        )
        console.print(f"[red bold]{msg}[/red bold]")
        notify(msg)


def attempt_auto_fix(pcp_dir: Path, failure_context: str, session_id: str, is_first_attempt: bool) -> bool:
    """Spawn a claude -p session instructed to diagnose+fix+commit+push.

    session_id ties consecutive auto-fix attempts within the same failure
    streak together: is_first_attempt=True opens a fresh session with
    --session-id, subsequent consecutive attempts --resume it instead of
    cold-restarting -- the same fix build.py already applies to its own
    per-criterion retries (a cold restart re-explores the whole repo and
    re-pastes context for every attempt). The caller resets to a new
    session_id once CI succeeds, so a genuinely new failure never resumes
    stale context from an unrelated one.

    Returns True if the agent ran without a process-level error — not a
    guarantee the fix worked, the next poll cycle re-checks CI for that."""
    prompt = (
        "A CI run failed on this repository. FIRST classify the failure from the log below "
        "as exactly one of: CODE (a real defect in application/test logic), FLAKY (test "
        "passes/fails non-deterministically — timing, ordering, network, shared state), or "
        "INFRA (runner/tooling/dependency-resolution problem outside the code). State the "
        "classification and your evidence for it before doing anything else.\n"
        "- CODE: fix the underlying defect, commit, and push to the current branch.\n"
        "- FLAKY: do NOT patch application code to make the symptom go away — masking an "
        "unreliable test with application changes creates debt and hides the real problem. "
        "Quarantine the test instead (mark it skipped/xfail with a comment naming the "
        "flakiness evidence), commit that, and say clearly a human needs to fix the test's "
        "underlying non-determinism.\n"
        "- INFRA: do NOT change application code. Fix the workflow/tooling config only if "
        "the cause is unambiguous from the log; otherwise change nothing and report what "
        "you found.\n"
        "Follow this project's CLAUDE.md and ci_rules.yaml.\n\n"
        f"## CI Failure Log\n```\n{failure_context}\n```\n"
    )
    session_flag = ["--session-id", session_id] if is_first_attempt else ["--resume", session_id]
    cmd = [
        _claude_bin(), "-p",
        "--permission-mode", "acceptEdits",
        "--output-format", "json",
        "--max-budget-usd", _watch_agent_max_budget_usd(),
        *session_flag,
    ]
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
@click.option("--report-only", is_flag=True,
              help="Report + notify on failures but never spawn a fix agent. Recommended first "
                   "phase on a new project — measure diagnosis signal quality before granting "
                   "auto-fix. Also enabled via PCP_WATCH_REPORT_ONLY=1.")
def watch(project_path: str | None, interval: int, once: bool, max_iterations: int | None, report_only: bool):
    """Continuously monitor CI + deploy health, auto-diagnose and fix failures."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    project_root = pcp_dir.parent
    check_environment(pcp_dir, fatal_on_missing_required=False)
    check_agent_depth_or_exit()
    integrations = load_integrations(pcp_dir)
    health_url = (integrations.get("deploy") or {}).get("health_check_url")

    if not shutil.which("gh") and not health_url:
        console.print("[yellow]Neither `gh` CLI nor a configured health-check URL found — nothing to watch.[/yellow]")
        console.print("[dim]Run `pcp doctor` to configure a deploy health-check URL.[/dim]")
        sys.exit(0)

    report_only = report_only or os.environ.get("PCP_WATCH_REPORT_ONLY", "") in ("1", "true", "yes")
    if report_only:
        console.print("[cyan]Report-only mode: failures will be reported/notified, no fix agent will be spawned.[/cyan]")

    max_iterations = max_iterations or _default_max_iterations()
    max_consecutive_fixes = _max_consecutive_auto_fix_attempts()
    last_seen_run_id = None
    iteration = 0
    consecutive_fix_attempts = 0
    auto_fix_disabled = False
    fix_session_id = None
    stale_escalations_reported: set = set()

    while True:
        iteration += 1
        console.print(f"[dim]Watch poll #{iteration}/{max_iterations} ({time.strftime('%H:%M:%S')})...[/dim]")

        run = get_latest_ci_run(project_root)
        if run and run.get("databaseId") != last_seen_run_id:
            last_seen_run_id = run.get("databaseId")
            if run.get("status") == "completed" and run.get("conclusion") not in ("success", None):
                console.print(f"[red]CI run failed:[/red] {run.get('name')} — {run.get('url')}")
                if report_only:
                    notify(f"pcp watch (report-only): CI run failed — {run.get('name')} {run.get('url')}")
                elif auto_fix_disabled:
                    notify(f"pcp watch: CI still failing after {max_consecutive_fixes} auto-fix attempts — auto-fix paused, needs human attention. {run.get('url')}")
                else:
                    logs = get_failed_logs(project_root, run["databaseId"])
                    console.print("[dim]Attempting auto-fix...[/dim]")
                    is_first_attempt = fix_session_id is None
                    if is_first_attempt:
                        fix_session_id = str(uuid.uuid4())
                    fixed = attempt_auto_fix(pcp_dir, logs, fix_session_id, is_first_attempt)
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
                fix_session_id = None

        check_stale_escalations(pcp_dir, stale_escalations_reported)

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
