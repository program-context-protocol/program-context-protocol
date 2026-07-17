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


def _source_metrics(project_root: Path) -> dict:
    """Cheap erosion proxies: total source lines + file count for the primary
    source tree. Deterministic, tool-free."""
    target = _detect_python_src(project_root) or project_root
    total_lines = files = 0
    for p in target.rglob("*.py"):
        if any(seg in p.parts for seg in ("__pycache__", ".venv", "venv", "node_modules", ".pcp")):
            continue
        try:
            total_lines += sum(1 for _ in open(p, errors="replace"))
            files += 1
        except OSError:
            continue
    return {"source_lines": total_lines, "source_files": files}


def _append_trend(pcp_dir: Path, timestamp: str, result: dict, metrics: dict) -> list[dict]:
    """Erosion TREND, not snapshot (SlopCodeBench, arXiv:2603.24755: structural
    erosion is the default trajectory of iterative agentic coding — 77% of
    trajectories — so a single audit run can look fine while the slope is
    bad). Plain JSONL, operational record."""
    import json
    path = pcp_dir / "audit_trend.jsonl"
    entry = {
        "timestamp": timestamp, "tool": result["tool"],
        "findings": len(result["findings"]), **metrics,
    }
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")
    rows = []
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _write_audit_md(pcp_dir: Path, result: dict, timestamp: str, trend: list[dict] | None = None) -> Path:
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

    if trend and len(trend) >= 2:
        lines += ["", "## Erosion Trend (last runs)", "",
                  "| When | Findings | Source lines | Files |", "|---|---|---|---|"]
        for row in trend[-8:]:
            lines.append(f"| {row.get('timestamp', '')} | {row.get('findings', '')} | "
                         f"{row.get('source_lines', '')} | {row.get('source_files', '')} |")
        first, last = trend[0], trend[-1]
        if last.get("source_lines") and first.get("source_lines"):
            growth = last["source_lines"] / max(first["source_lines"], 1)
            lines += ["", f"Source growth since first audit: {growth:.2f}x lines; "
                          f"dead-code findings {first.get('findings', 0)} → {last.get('findings', 0)}. "
                          "Rising findings alongside faster-than-feature line growth is the erosion signature "
                          "(SlopCodeBench: default trajectory of iterative agentic coding, not an edge case)."]
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
    metrics = _source_metrics(project_root)
    trend = _append_trend(pcp_dir, timestamp, result, metrics)
    out_path = _write_audit_md(pcp_dir, result, timestamp, trend)

    if quiet:
        sys.exit(0)

    if result["tool"] is None:
        console.print("[dim]No dead-code tool detected (vulture or knip) — skipped.[/dim]")
    else:
        count = len(result["findings"])
        color = "green" if count == 0 else "yellow"
        console.print(f"[{color}]{count} dead-code finding(s)[/{color}] ([dim]{result['tool']}[/dim])  →  {out_path.relative_to(project_root)}")

    sys.exit(0)
