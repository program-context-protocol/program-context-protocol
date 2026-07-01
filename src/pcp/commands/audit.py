"""pcp audit — advisory dead-code / bloat scan (unused exports, unreferenced funcs).

Never hard-blocks. Wraps whatever dead-code tool is already installed for the
target language (vulture for Python, knip for JS/TS). Writes .pcp/audit.md so
drift in code bloat is visible the same way drift in spec coverage is.
"""

import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, NoPCPDir

console = Console()

MAX_FINDINGS_SHOWN = 30


def _detect_python_src(project_root: Path) -> Path | None:
    for candidate in ("src", "."):
        p = project_root / candidate
        if candidate == "src" and p.is_dir():
            return p
    if (project_root / "pyproject.toml").exists() or (project_root / "setup.py").exists():
        return project_root
    return None


def _run_vulture(project_root: Path) -> dict | None:
    if not shutil.which("vulture"):
        return None
    target = _detect_python_src(project_root)
    if target is None:
        return None
    result = subprocess.run(
        ["vulture", str(target), "--min-confidence", "80"],
        capture_output=True, text=True, cwd=project_root,
    )
    findings = [l.strip() for l in result.stdout.splitlines() if l.strip()]
    return {"tool": "vulture", "findings": findings}


def _run_knip(project_root: Path) -> dict | None:
    knip_bin = project_root / "node_modules" / ".bin" / "knip"
    if not (project_root / "package.json").exists() or not knip_bin.exists():
        return None
    result = subprocess.run(
        [str(knip_bin), "--reporter", "compact"],
        capture_output=True, text=True, cwd=project_root,
    )
    findings = [l.strip() for l in result.stdout.splitlines() if l.strip()]
    return {"tool": "knip", "findings": findings}


def _run_audit(project_root: Path) -> dict:
    for runner in (_run_vulture, _run_knip):
        out = runner(project_root)
        if out is not None:
            return out
    return {"tool": None, "findings": []}


def _write_audit_md(pcp_dir: Path, result: dict, timestamp: str) -> Path:
    tool = result["tool"]
    findings = result["findings"]
    lines = [
        "# Dead Code / Bloat Audit",
        f"Generated: {timestamp}",
        "",
    ]
    if tool is None:
        lines += [
            "_No audit tool detected._",
            "",
            "Install one to enable this check:",
            "- Python: `pip install vulture`",
            "- JS/TS: `npm install -D knip`",
        ]
    else:
        lines += [
            f"Tool: `{tool}`",
            f"Findings: {len(findings)}",
            "",
        ]
        if findings:
            lines.append("## Findings")
            lines.append("")
            for f in findings[:MAX_FINDINGS_SHOWN]:
                lines.append(f"- {f}")
            if len(findings) > MAX_FINDINGS_SHOWN:
                lines.append(f"- _...and {len(findings) - MAX_FINDINGS_SHOWN} more (see full tool output)_")
        else:
            lines.append("_No findings._")
    lines.append("")
    out = pcp_dir / "audit.md"
    out.write_text("\n".join(lines))
    return out


@click.command()
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root (default: cwd, walks up to find .pcp/).")
@click.option("--quiet", is_flag=True, help="Suppress output.")
def audit(project_path: str | None, quiet: bool):
    """Advisory dead-code / bloat scan. Writes .pcp/audit.md. Never blocks."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    project_root = pcp_dir.parent
    result = _run_audit(project_root)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out_path = _write_audit_md(pcp_dir, result, timestamp)

    if quiet:
        sys.exit(0)

    if result["tool"] is None:
        console.print("[dim]No dead-code tool detected (vulture or knip) — skipped.[/dim]")
    else:
        count = len(result["findings"])
        color = "green" if count == 0 else "yellow"
        console.print(f"[{color}]{count} dead-code finding(s)[/{color}] ([dim]{result['tool']}[/dim])  →  {out_path.relative_to(project_root)}")

    sys.exit(0)
