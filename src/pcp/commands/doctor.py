"""pcp doctor — environment preflight. Checks which CLI integrations are
available, writes .pcp/integrations.yaml. Interactive mode asks for the ones
that need human-provided config (deploy command, health-check URL).

pcp build/watch/deploy call check_environment() at the start of their run in
non-interactive report-only mode — never blocks on missing optional tooling,
only on git/claude (required for the lifecycle to function at all).
"""

import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.table import Table

from pcp.pcp_dir import find_pcp_dir, NoPCPDir

console = Console()

REQUIRED_TOOLS = ["git", "claude"]

DEPLOY_HINT_FILES = {
    "railway.toml": "railway up",
    "vercel.json": "vercel --prod",
    "Procfile": "git push origin main",
    "Dockerfile": None,
}


def _which(name: str) -> str | None:
    return shutil.which(name)


def detect_tools() -> dict:
    """Pure detection, no prompts. Used by both `pcp doctor` and the
    non-interactive preflight other commands run automatically."""
    return {
        "git": {"available": _which("git") is not None, "path": _which("git")},
        "claude": {"available": _which("claude") is not None, "path": _which("claude")},
        "gh": {"available": _which("gh") is not None, "path": _which("gh")},
        "test_runner": _detect_one(["pytest", "npm", "go"]),
        "lint": _detect_one(["ruff", "eslint"]),
        "sast": _detect_one(["semgrep"]),
        "coverage": _detect_one(["coverage"]),
        "audit": _detect_one(["vulture", "knip"]),
        "slack_notify": {"available": _which("slack-notify") is not None, "path": _which("slack-notify")},
    }


def _detect_one(candidates: list[str]) -> dict:
    for c in candidates:
        path = _which(c)
        if path:
            return {"tool": c, "available": True, "path": path}
    return {"tool": None, "available": False, "path": None}


def _guess_deploy_command(project_root: Path) -> str | None:
    for fname, cmd in DEPLOY_HINT_FILES.items():
        if (project_root / fname).exists() and cmd:
            return cmd
    return None


def load_integrations(pcp_dir: Path) -> dict:
    path = pcp_dir / "integrations.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def check_environment(pcp_dir: Path, fatal_on_missing_required: bool = True) -> dict:
    """Non-interactive preflight — called automatically at the start of
    pcp build/watch/deploy. Reports, never prompts. Fatal only on git/claude."""
    tools = detect_tools()
    missing_required = [t for t in REQUIRED_TOOLS if not tools[t]["available"]]
    if missing_required:
        console.print(f"[red bold]Missing required tool(s): {', '.join(missing_required)}[/red bold]")
        console.print("[dim]Run `pcp doctor` for full environment setup.[/dim]")
        if fatal_on_missing_required:
            sys.exit(2)

    optional_missing = [
        k for k in ("test_runner", "lint", "sast", "coverage", "audit")
        if not tools[k]["available"]
    ]
    if optional_missing:
        console.print(
            f"[dim]Preflight: no {', '.join(optional_missing)} tool detected — those QA steps will skip. "
            f"Run `pcp doctor` to review.[/dim]"
        )
    return tools


@click.command()
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root (default: cwd, walks up to find .pcp/).")
@click.option("--check", "check_only", is_flag=True,
              help="Non-interactive: detect and report only, don't prompt or write.")
def doctor(project_path: str | None, check_only: bool):
    """Check/configure CLI integrations for this project's PCP lifecycle."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    project_root = pcp_dir.parent
    tools = detect_tools()

    table = Table(title="PCP Environment Check")
    table.add_column("Integration")
    table.add_column("Status")
    table.add_column("Detail")

    def _row(name: str, info: dict, key_label: str = "tool"):
        if info.get("available"):
            detail = info.get("path") or info.get(key_label) or ""
            table.add_row(name, "[green]available[/green]", str(detail))
        else:
            table.add_row(name, "[yellow]not found[/yellow]", "—")

    _row("git (required)", tools["git"])
    _row("claude CLI (required)", tools["claude"])
    _row("gh (CI status, for `pcp watch`)", tools["gh"])
    _row("Test runner", tools["test_runner"])
    _row("Lint", tools["lint"])
    _row("SAST/secret-scan", tools["sast"])
    _row("Coverage", tools["coverage"])
    _row("Dead-code audit", tools["audit"])
    _row("Slack notifications", tools["slack_notify"])
    console.print(table)
    console.print("[dim]Browser automation (for `pcp uat`): assumed available via this environment's MCP tools — not directly verified.[/dim]")

    existing = load_integrations(pcp_dir)
    deploy = existing.get("deploy", {})

    if check_only:
        return

    console.print("\n[bold]Deploy configuration[/bold] (used by `pcp deploy` and `pcp watch`)")
    guessed = _guess_deploy_command(project_root)
    default_cmd = deploy.get("command") or guessed or ""
    deploy_command = click.prompt(
        "Deploy command (blank to skip)", default=default_cmd, show_default=bool(default_cmd),
    )
    health_url = click.prompt(
        "Post-deploy health-check URL (blank to skip)", default=deploy.get("health_check_url", ""), show_default=False,
    )
    rollback_command = click.prompt(
        "Rollback command (blank to skip)", default=deploy.get("rollback_command", ""), show_default=False,
    )

    data = {
        "version": "1.0",
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tools": tools,
        "deploy": {
            "command": deploy_command or None,
            "health_check_url": health_url or None,
            "rollback_command": rollback_command or None,
        },
        "browser_automation": {"assumed_available": True},
    }
    out = pcp_dir / "integrations.yaml"
    out.write_text(yaml.dump(data, default_flow_style=False))
    console.print(f"\n[green]✓[/green] Saved → {out.relative_to(project_root)}")
