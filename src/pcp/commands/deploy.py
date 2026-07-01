"""pcp deploy — production deploy automation.

Per docs/greenfield.md Phase 6: checklist, trigger, smoke test, auto-rollback.
Deploy is the one place in the PCP lifecycle where an irreversible production
action happens — human approval is mandatory by default (`--yes` opts out for
CI use, deliberately not the default). Extra scrutiny if migration/PII/payment
-flavoured criteria are detected in this release.
"""

import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, get_modules_dir, NoPCPDir
from pcp.schema.validator import load_yaml
from pcp.commands.doctor import load_integrations, check_environment
from pcp.commands.watch import check_deploy_health, notify

console = Console()

RISK_KEYWORDS = ["migration", "migrate", "pii", "personal data", "payment", "credit card", "compliance"]


def run_deploy_check(project_root: Path) -> bool:
    """Shells out to `pcp deploy-check` (Layer 3) rather than importing it —
    deploy_check.py's logic is a single click command body, not factored into
    a reusable function, and shelling out exercises the exact same path a
    human running it manually would."""
    result = subprocess.run(
        [sys.executable, "-m", "pcp.cli", "deploy-check", "--path", str(project_root)],
        cwd=project_root,
    )
    return result.returncode == 0


def collect_risk_flags(modules_dir: Path) -> list[str]:
    flags = []
    if not modules_dir.exists():
        return flags
    for acc_path in sorted(modules_dir.glob("*/acceptance.yaml")):
        data = load_yaml(acc_path)
        for c in data.get("criteria", []):
            desc = (c.get("description") or "").lower()
            for kw in RISK_KEYWORDS:
                if kw in desc:
                    flags.append(f"{acc_path.parent.name}/{c['id']}: \"{c['description']}\" (matched '{kw}')")
                    break
    return flags


def log_deploy(pcp_dir: Path, entry: dict) -> None:
    log_path = pcp_dir / "deploy_log.yaml"
    existing = []
    if log_path.exists():
        data = yaml.safe_load(log_path.read_text()) or {}
        existing = data.get("deploys", [])
    existing.append(entry)
    log_path.write_text(yaml.dump({"deploys": existing}, default_flow_style=False))


@click.command()
@click.option("--path", "project_path", type=click.Path(), default=None)
@click.option("--yes", is_flag=True, help="Skip the interactive approval prompt (CI/non-interactive use — opt-in, not default).")
@click.option("--rollout", default="100", help="Rollout percentage, logged for audit (e.g. 5, 25, 100).")
def deploy(project_path: str | None, yes: bool, rollout: str):
    """Production deploy — checklist, trigger, smoke test, auto-rollback."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    project_root = pcp_dir.parent
    check_environment(pcp_dir, fatal_on_missing_required=False)
    integrations = load_integrations(pcp_dir)
    deploy_cfg = integrations.get("deploy") or {}
    deploy_command = deploy_cfg.get("command")
    health_url = deploy_cfg.get("health_check_url")
    rollback_command = deploy_cfg.get("rollback_command")

    console.print("[bold]Deploy checklist[/bold]")
    console.print("[dim]Checking Layer 3 SDLC phase exit criteria (pcp deploy-check)...[/dim]")
    if not run_deploy_check(project_root):
        console.print("[red bold]BLOCKED — deploy-check failed. Resolve exit criteria before deploying.[/red bold]")
        sys.exit(1)
    console.print("[green]✓[/green] deploy-check passed")

    try:
        from pcp.commands.provenance import write_provenance
        write_provenance(pcp_dir)
        console.print("[dim]Audit evidence refreshed -> .pcp/provenance.md (review before approving)[/dim]")
    except Exception as e:
        console.print(f"[dim]Provenance refresh skipped: {e}[/dim]")

    modules_dir = get_modules_dir(pcp_dir)
    risk_flags = collect_risk_flags(modules_dir)
    if risk_flags:
        console.print("\n[yellow bold]Risk flags detected — review before approving:[/yellow bold]")
        for f in risk_flags:
            console.print(f"  ⚠ {f}")
    else:
        console.print("[dim]No migration/PII/payment-flavoured criteria detected in this release.[/dim]")

    console.print(f"\nRollout: {rollout}%")
    console.print(f"Deploy command: {deploy_command or '(none configured — run `pcp doctor`)'}")
    console.print(f"Health check: {health_url or '(none configured)'}")
    console.print(f"Rollback command: {rollback_command or '(none configured)'}")

    if not deploy_command:
        console.print("[red]No deploy command configured. Run `pcp doctor` to set one.[/red]")
        sys.exit(2)

    if not yes:
        prompt = "Approve this deploy?"
        if risk_flags:
            prompt = f"⚠ {len(risk_flags)} risk flag(s) above — approve this deploy anyway?"
        if not click.confirm(prompt, default=False):
            console.print("[yellow]Deploy aborted by user.[/yellow]")
            sys.exit(0)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    console.print(f"\n[bold]Triggering deploy:[/bold] {deploy_command}")
    trigger_result = subprocess.run(deploy_command, shell=True, cwd=project_root)
    deploy_ok = trigger_result.returncode == 0

    smoke_result = None
    rolled_back = False
    if deploy_ok and health_url:
        console.print("[dim]Waiting 10s before smoke test...[/dim]")
        time.sleep(10)
        smoke_result = check_deploy_health(health_url)
        if smoke_result:
            console.print("[green]✓ Smoke test passed.[/green]")
        else:
            console.print("[red]✗ Smoke test FAILED.[/red]")
            if rollback_command:
                console.print(f"[bold]Auto-rollback:[/bold] {rollback_command}")
                rb = subprocess.run(rollback_command, shell=True, cwd=project_root)
                rolled_back = rb.returncode == 0
                notify(f"pcp deploy: smoke test failed, auto-rollback {'succeeded' if rolled_back else 'FAILED — needs human attention'}")
            else:
                notify("pcp deploy: smoke test failed, no rollback command configured — needs human attention")

    log_deploy(pcp_dir, {
        "timestamp": timestamp, "rollout_pct": rollout, "risk_flags": risk_flags,
        "deploy_command": deploy_command, "trigger_succeeded": deploy_ok,
        "smoke_test_passed": smoke_result, "rolled_back": rolled_back,
    })

    if not deploy_ok:
        console.print("[red bold]Deploy trigger command failed.[/red bold]")
        sys.exit(1)

    console.print("\n[bold]Post-deploy:[/bold] watch P95 latency, error rate, memory for the next 15 minutes.")
    if smoke_result is False and not rolled_back:
        sys.exit(1)
