"""pcp status — generate or refresh pcp.md governance snapshot."""

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, get_modules_dir, NoPCPDir
from pcp.schema.validator import validate_file, load_yaml
from pcp.pcp_status import write_pcp_md

console = Console()


def _load_modules_results(modules_dir: Path, project_root: Path) -> list[dict]:
    """Reconstruct module results from existing current_state.md or live scan."""
    results = []
    for af in sorted(modules_dir.glob("*/acceptance.yaml")):
        module_name = af.parent.name
        data = load_yaml(af)
        criteria_results = []
        for c in data.get("criteria", []):
            criteria_results.append({
                "id": c["id"],
                "description": c["description"],
                "check": c.get("check", "manual"),
                "status": c.get("status", "pending"),
                "detail": "",
            })
        results.append({"module": module_name, "criteria": criteria_results})
    return results


def _parse_from_current_state(current_state_path: Path) -> dict[str, str]:
    """Parse status from current_state.md — avoids re-running all checks."""
    if not current_state_path.exists():
        return {}
    statuses = {}
    for line in current_state_path.read_text().splitlines():
        m = re.match(r"- \[([ x])\] ([A-Z0-9_-]+/[A-Z][0-9]+):", line.strip())
        if m:
            statuses[m.group(2)] = "complete" if m.group(1) == "x" else "pending"
    return statuses


PM_STATUS_SYSTEM_PROMPT = """\
You are an expert AI product manager.
Your task is to generate a plain-English project status report for a non-technical PM based on the provided project context and git history.

The status report must be user-friendly, high-level, and avoid code snippets, technical jargon, or raw config file content.
It must include:
1. Current SDLC Phase and Completion % (calculated from criteria complete/total).
2. This Week's Progress (derived from recent git commits and completed criteria).
3. Current Blockers / PM Actions Needed (e.g. pending manual criteria, inputs/decisions needed).
4. What's Next (what features/modules will be developed next).

Keep it concise, clear, and action-oriented. Use formatting like bold text and bullet points.
"""


def _get_recent_commits(repo_path: Path) -> str:
    import subprocess
    result = subprocess.run(
        ["git", "log", "--since=7.days.ago", "--oneline"],
        capture_output=True,
        text=True,
        cwd=repo_path,
    )
    if result.returncode != 0:
        return "No recent commits."
    return result.stdout


@click.command()
@click.option("--path", "project_path", type=click.Path(), default=None)
@click.option("--rescan", is_flag=True,
              help="Re-run full scan before writing pcp.md (slower but accurate).")
@click.option("--print", "print_only", is_flag=True,
              help="Print pcp.md to stdout instead of writing file.")
@click.option("--pm", "pm_mode", is_flag=True,
              help="Generate a plain-English status report for the PM.")
def status(project_path: str | None, rescan: bool, print_only: bool, pm_mode: bool):
    """Generate or refresh pcp.md governance snapshot at project root.

    By default reads from existing current_state.md (fast).
    Use --rescan to re-evaluate all acceptance criteria first.
    """
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    project_root = pcp_dir.parent
    modules_dir = get_modules_dir(pcp_dir)

    if rescan:
        # Invoke scan command logic inline
        from pcp.commands.scan import _scan_module, _write_current_state, _load_prior_manual_status
        current_state_path = pcp_dir / "current_state.md"
        prior_manual = _load_prior_manual_status(current_state_path)
        modules_results = []
        for af in sorted(modules_dir.glob("*/acceptance.yaml")):
            module_name = af.parent.name
            result = _scan_module(module_name, af, project_root, prior_manual)
            modules_results.append(result)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_current_state(pcp_dir, modules_results, timestamp)
    else:
        # Fast path: read from current_state.md
        statuses = _parse_from_current_state(pcp_dir / "current_state.md")
        modules_results = _load_modules_results(modules_dir, project_root)
        # Apply parsed statuses
        for m in modules_results:
            for c in m["criteria"]:
                key = f"{m['module'].upper()}/{c['id']}"
                if key in statuses:
                    c["status"] = statuses[key]
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    total = sum(len(m["criteria"]) for m in modules_results)
    complete = sum(1 for m in modules_results for c in m["criteria"] if c["status"] == "complete")

    pcp_md_path = write_pcp_md(pcp_dir, modules_results, timestamp, total, complete)

    if pm_mode:
        from pcp.llm import client as llm
        obj_text = (pcp_dir / "objective.md").read_text() if (pcp_dir / "objective.md").exists() else ""
        current_state_text = (pcp_dir / "current_state.md").read_text() if (pcp_dir / "current_state.md").exists() else ""
        sdlc_text = (pcp_dir / "SDLC_phase.yaml").read_text() if (pcp_dir / "SDLC_phase.yaml").exists() else ""
        commits = _get_recent_commits(project_root)

        score_pct = f"{complete}/{total} ({complete/total:.0%})" if total else "0/0 (0%)"

        user_prompt = (
            f"Objective:\n{obj_text}\n\n"
            f"Current State & Completion:\nCompletion Score: {score_pct}\n{current_state_text}\n\n"
            f"SDLC Phase Config:\n{sdlc_text}\n\n"
            f"Recent Commits (last 7 days):\n{commits}\n"
        )

        console.print("[dim]Generating plain-English PM status report...[/dim]\n")
        try:
            report_text = llm.call(
                PM_STATUS_SYSTEM_PROMPT, user_prompt,
                model=llm.JUDGE_MODEL, pcp_dir=pcp_dir, command="status-pm",
            )
            click.echo(report_text)
        except Exception as e:
            console.print(f"[red]Error generating PM report:[/red] {e}")
            sys.exit(2)
        return

    if print_only:
        click.echo(pcp_md_path.read_text())
        return

    score = complete / total if total else 0.0
    color = "green" if score >= 0.8 else "yellow" if score >= 0.5 else "red"
    console.print(f"[{color}]{complete}/{total} ({score:.0%})[/{color}]  →  pcp.md")

    # Escalation-acknowledgment watchdog — an escalation recorded but never
    # acted on must stay loudly visible, not buried in escalations.yaml.
    from pcp import escalations
    for e in escalations.find_stale(pcp_dir):
        console.print(
            f"[red bold]STALE ESCALATION [{e.get('state', 'unacked')}]:[/red bold] "
            f"{e.get('module')}/{e.get('criterion_id')} ({e.get('category', 'uncategorized')}) "
            f"waiting {e.get('age_hours')}h — ack: pcp escalations --ack {e.get('module')}/{e.get('criterion_id')}"
        )

    # Notification dead-man's-switch — attempts without successes means the
    # pipeline is broken and nobody is being reached.
    from pcp.commands.watch import check_notify_heartbeat
    hb = check_notify_heartbeat(pcp_dir)
    if hb:
        console.print(f"[red bold]{hb}[/red bold]")
