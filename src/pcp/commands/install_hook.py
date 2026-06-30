"""pcp install-hook — install pcp check as a git pre-commit hook."""

import sys
from pathlib import Path

import click
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, NoPCPDir

console = Console()

PRE_COMMIT_HOOK = """\
#!/bin/sh
# PCP Layer 1 pre-commit gate
# Installed by: pcp install-hook
pcp check --commit-msg-file "$(git rev-parse --git-dir)/COMMIT_EDITMSG"
"""

PRE_COMMIT_FRAMEWORK_CONFIG = """\
repos:
  - repo: local
    hooks:
      - id: pcp-check
        name: PCP Layer 1 gate
        entry: pcp check
        language: system
        pass_filenames: false
        always_run: true
"""


@click.command()
@click.option("--path", "project_path", type=click.Path(), default=None)
@click.option("--pre-commit-framework", is_flag=True,
              help="Add to .pre-commit-config.yaml instead of .git/hooks/.")
@click.option("--force", is_flag=True, help="Overwrite existing hook.")
def install_hook(project_path: str | None, pre_commit_framework: bool, force: bool):
    """Install pcp check as git pre-commit hook."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    project_root = pcp_dir.parent

    if pre_commit_framework:
        config_path = project_root / ".pre-commit-config.yaml"
        if config_path.exists():
            existing = config_path.read_text()
            if "pcp-check" in existing:
                console.print("[dim]pcp-check already in .pre-commit-config.yaml[/dim]")
                sys.exit(0)
            with open(config_path, "a") as f:
                f.write("\n" + PRE_COMMIT_FRAMEWORK_CONFIG)
            console.print("[green]appended[/green] pcp-check to .pre-commit-config.yaml")
        else:
            config_path.write_text(PRE_COMMIT_FRAMEWORK_CONFIG)
            console.print("[green]created[/green] .pre-commit-config.yaml with pcp-check")
        return

    git_dir_result = None
    try:
        import subprocess
        r = subprocess.run(["git", "rev-parse", "--git-dir"],
                           capture_output=True, text=True, cwd=project_root)
        git_dir = Path(r.stdout.strip()) if r.returncode == 0 else None
    except FileNotFoundError:
        git_dir = None

    if not git_dir:
        console.print("[red]Error:[/red] not a git repository.")
        sys.exit(2)

    if not git_dir.is_absolute():
        git_dir = project_root / git_dir

    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    hook_path = hooks_dir / "pre-commit"

    if hook_path.exists() and not force:
        console.print(f"[yellow]Hook already exists:[/yellow] {hook_path}")
        console.print("Use --force to overwrite, or --pre-commit-framework to append.")
        sys.exit(1)

    hook_path.write_text(PRE_COMMIT_HOOK)
    hook_path.chmod(0o755)
    console.print(f"[green]installed[/green] {hook_path}")
    console.print("[dim]pcp check will run before every commit.[/dim]")

    _install_cron_scripts()


def _install_cron_scripts():
    """Install daily cron scripts for intervention aggregation and skill upgrade."""
    import subprocess
    scripts_dir = Path.home() / ".pcp" / "cron"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    aggregator = scripts_dir / "aggregate_interventions.sh"
    aggregator.write_text("""\
#!/bin/bash
# PCP daily intervention aggregation — installed by pcp install-hook
set -euo pipefail

LEARNING_DIR="$HOME/.pcp"
mkdir -p "$LEARNING_DIR"
OUTFILE="$LEARNING_DIR/global_learning.yaml"
TMPFILE="$(mktemp)"

echo "generated_at: $(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$TMPFILE"
echo "projects_scanned: 0" >> "$TMPFILE"
echo "interventions: []" >> "$TMPFILE"

COUNT=0
TOTAL=0
while IFS= read -r log; do
  COUNT=$((COUNT + 1))
  ENTRIES=$(python3 -c "
import yaml, sys
data = yaml.safe_load(open('$log')) or {}
items = data.get('interventions', [])
print(len(items))
" 2>/dev/null || echo 0)
  TOTAL=$((TOTAL + ENTRIES))
done < <(find ~/Claude-code -name "intervention_log.yaml" -path "*/.pcp/*" 2>/dev/null)

python3 - "$TMPFILE" "$OUTFILE" "$COUNT" "$TOTAL" << 'PYEOF'
import yaml, sys
from pathlib import Path
from collections import defaultdict

tmpfile, outfile, projects, total = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
from datetime import datetime, timezone

all_entries = []
import subprocess, os
result = subprocess.run(
    ["find", os.path.expanduser("~/Claude-code"), "-name", "intervention_log.yaml", "-path", "*/.pcp/*"],
    capture_output=True, text=True
)
for log_path in result.stdout.strip().splitlines():
    try:
        data = yaml.safe_load(open(log_path)) or {}
        all_entries.extend(data.get("interventions", []))
    except Exception:
        pass

by_type = defaultdict(lambda: {"count": 0, "times": []})
for e in all_entries:
    t = e.get("type", "unknown")
    by_type[t]["count"] += 1
    mins = e.get("time_to_resolve_minutes")
    if mins:
        by_type[t]["times"].append(mins)

summary = {
    "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "projects_scanned": projects,
    "total_interventions": len(all_entries),
    "by_type": {
        k: {
            "count": v["count"],
            "avg_minutes": round(sum(v["times"]) / len(v["times"]), 1) if v["times"] else None
        }
        for k, v in sorted(by_type.items(), key=lambda x: -x[1]["count"])
    },
}
Path(outfile).write_text(yaml.dump(summary, default_flow_style=False))
print(f"Written: {outfile}")
PYEOF

# Slack notification
if command -v slack-notify &>/dev/null; then
  TOTAL_INT=$(python3 -c "import yaml; d=yaml.safe_load(open('$OUTFILE')); print(d.get('total_interventions',0))" 2>/dev/null || echo "?")
  slack-notify -c "#pcp-learning" "PCP Daily Learning — $(date +%Y-%m-%d)
Projects scanned: $COUNT | Total interventions: $TOTAL_INT
See: ~/.pcp/global_learning.yaml"
fi
""")
    aggregator.chmod(0o755)

    upgrader = scripts_dir / "upgrade_skill.sh"
    upgrader.write_text("""\
#!/bin/bash
# PCP skill upgrade check — installed by pcp install-hook
set -euo pipefail

SKILL_PATH="$HOME/.claude/skills/pcp/SKILL.md"
[ -f "$SKILL_PATH" ] || exit 0

LOCAL_VERSION=$(grep "^version:" "$SKILL_PATH" | head -1 | awk '{print $2}' | tr -d '"')

# Try GitHub first (once repo is live)
REMOTE_URL="https://raw.githubusercontent.com/ganeshnallasivam-cell/program-context-protocol/main/SKILL.md"
REMOTE_SKILL=$(curl -sf "$REMOTE_URL" 2>/dev/null || echo "")

if [ -n "$REMOTE_SKILL" ]; then
  REMOTE_VERSION=$(echo "$REMOTE_SKILL" | grep "^version:" | head -1 | awk '{print $2}' | tr -d '"')
  if [ "$REMOTE_VERSION" != "$LOCAL_VERSION" ] && [ -n "$REMOTE_VERSION" ]; then
    cp "$SKILL_PATH" "${SKILL_PATH}.bak"
    echo "$REMOTE_SKILL" > "$SKILL_PATH"
    slack-notify "PCP skill upgraded: v${LOCAL_VERSION} → v${REMOTE_VERSION}. Changes active on next /pcp." 2>/dev/null || true
    echo "Upgraded: $LOCAL_VERSION → $REMOTE_VERSION"
  fi
else
  echo "No remote version available — skipping upgrade check."
fi
""")
    upgrader.chmod(0o755)

    # Register in crontab
    try:
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        crontab = existing.stdout if existing.returncode == 0 else ""
        changed = False

        agg_line = f"47 8 * * * {aggregator} >> $HOME/.pcp/cron_aggregator.log 2>&1"
        upg_line = f"13 9 * * * {upgrader} >> $HOME/.pcp/cron_upgrader.log 2>&1"

        if str(aggregator) not in crontab:
            crontab += f"\n{agg_line}\n"
            changed = True
        if str(upgrader) not in crontab:
            crontab += f"\n{upg_line}\n"
            changed = True

        if changed:
            proc = subprocess.run(["crontab", "-"], input=crontab, text=True)
            if proc.returncode == 0:
                console.print("[green]installed[/green] daily cron jobs:")
                console.print(f"  [dim]8:47am — intervention aggregation → ~/.pcp/global_learning.yaml[/dim]")
                console.print(f"  [dim]9:13am — skill upgrade check[/dim]")
            else:
                console.print("[yellow]crontab write failed — scripts written but not scheduled:[/yellow]")
                console.print(f"  {aggregator}")
                console.print(f"  {upgrader}")
        else:
            console.print("[dim]cron jobs already installed[/dim]")
    except FileNotFoundError:
        console.print("[yellow]crontab not available — scripts written to:[/yellow]")
        console.print(f"  {aggregator}")
        console.print(f"  {upgrader}")
        console.print("  Add them to your scheduler manually.")
