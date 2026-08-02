"""pcp audit — advisory dead-code / bloat scan (unused exports, unreferenced funcs).

Never hard-blocks. Wraps whatever dead-code tool is already installed for the
target language (vulture for Python, knip for JS/TS). Writes .pcp/audit.md so
drift in code bloat is visible the same way drift in spec coverage is.
"""

import json
import os
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
DEFAULT_COVERAGE_ADVISORY_THRESHOLD = 50

# ast-grep pattern for a swallowed exception: any except clause (bare, typed,
# or `as e`) whose entire body is just `pass` -- real error-handling that was
# never designed in, not paperwork (same class as the lazy-marker scan's stub
# bodies, deliberately distinct from it -- CTRL-029 catches TODO/placeholder
# text and `def f(): pass` stubs; this catches a *handled-looking* exception
# that actually discards the error). `except Exception: logger.exception(...)`
# and similar do NOT match -- only a body that is exactly `pass`.
_AST_GREP_SWALLOWED_EXCEPTION_PATTERN = "try:\n    $$$BODY\nexcept $$$EXC:\n    pass"


def _run_ast_grep_swallowed_exceptions(project_root: Path) -> dict | None:
    """Advisory-only, deliberately kept OUT of `pcp build`'s per-criterion
    hot loop -- this project's own build.py already documents removing four
    checks from that loop after cost/signal-quality problems (see the
    gate_calls comment there). `pcp audit` runs once per module, not once per
    attempt, which is the same tradeoff CTRL-029's lazy-marker scan and the
    dead-code scan already make."""
    if not shutil.which("ast-grep"):
        return None
    result = subprocess.run(
        ["ast-grep", "run", "--pattern", _AST_GREP_SWALLOWED_EXCEPTION_PATTERN,
         "--lang", "python", "--json"],
        capture_output=True, text=True, cwd=project_root,
    )
    if result.returncode not in (0, 1):  # grep-style: 0 = matches found, 1 = ran clean with no matches
        return {"tool": "ast-grep", "findings": []}
    try:
        matches = json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError:
        return {"tool": "ast-grep", "findings": []}
    findings = []
    for m in matches:
        line = m.get("range", {}).get("start", {}).get("line", 0) + 1  # 0-indexed
        findings.append(f"{m.get('file', '?')}:{line}: except clause swallows the error (body is just `pass`)")
    return {"tool": "ast-grep", "findings": findings}


def _run_jscpd(project_root: Path) -> dict | None:
    """Duplication %, advisory-only, same posture as the dead-code scan.
    jscpd writes a JSON report to a temp dir rather than stdout, so this
    scopes its own output location and cleans up after itself."""
    if not shutil.which("jscpd"):
        return None
    import tempfile
    with tempfile.TemporaryDirectory() as report_dir:
        subprocess.run(
            ["jscpd", ".", "--reporters", "json", "--output", report_dir],
            capture_output=True, text=True, cwd=project_root,
        )
        report_path = Path(report_dir) / "jscpd-report.json"
        if not report_path.exists():
            return {"tool": "jscpd", "duplication_pct": None, "findings": []}
        try:
            data = json.loads(report_path.read_text())
        except (json.JSONDecodeError, OSError):
            return {"tool": "jscpd", "duplication_pct": None, "findings": []}
    stats = data.get("statistics", {}).get("total", {})
    pct = stats.get("percentage")
    findings = []
    for dup in data.get("duplicates", [])[:MAX_FINDINGS_SHOWN]:
        first = dup.get("firstFile", {})
        second = dup.get("secondFile", {})
        findings.append(
            f"{first.get('name', '?')}:{first.get('startLoc', {}).get('line', '?')} "
            f"~ {second.get('name', '?')}:{second.get('startLoc', {}).get('line', '?')} "
            f"({dup.get('lines', '?')} duplicated lines)"
        )
    return {"tool": "jscpd", "duplication_pct": pct, "findings": findings}


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


def _run_coverage_check(project_root: Path) -> dict:
    """Reuses `qa.run_coverage` (already exists, powers the opt-in
    `pcp scan --coverage`) -- coverage % was tracked but never gated
    anywhere (2026-08-02 gap analysis). This is the advisory-first flip:
    run it once per module here (same cadence as the rest of `pcp audit`,
    NOT the per-attempt hot loop -- a full coverage run means running the
    whole test suite instrumented, and the test-suite gate is already 76%
    of this project's own measured build cost), and warn when it's below
    PCP_COVERAGE_ADVISORY_THRESHOLD (default 50). Advisory only -- this is
    the track-record-building step before anything here could ever become
    a hard gate, per PCP's own new-checks-are-advisory-first doctrine."""
    from pcp import qa
    result = qa.run_coverage(project_root)
    threshold = int(os.environ.get("PCP_COVERAGE_ADVISORY_THRESHOLD", str(DEFAULT_COVERAGE_ADVISORY_THRESHOLD)))
    result["threshold"] = threshold
    result["below_threshold"] = (
        result.get("percent") is not None and result["percent"] < threshold
    )
    return result


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


def _append_trend(
    pcp_dir: Path, timestamp: str, result: dict, metrics: dict,
    ast_grep_result: dict | None = None, jscpd_result: dict | None = None,
    coverage_result: dict | None = None,
) -> list[dict]:
    """Erosion TREND, not snapshot (SlopCodeBench, arXiv:2603.24755: structural
    erosion is the default trajectory of iterative agentic coding — 77% of
    trajectories — so a single audit run can look fine while the slope is
    bad). Plain JSONL, operational record.

    ast_grep_result/jscpd_result are new fields (2026-08-02) — old rows simply
    lack them, which every consumer already handles via .get()."""
    import json
    path = pcp_dir / "audit_trend.jsonl"
    entry = {
        "timestamp": timestamp, "tool": result["tool"],
        "findings": len(result["findings"]), **metrics,
    }
    if ast_grep_result is not None:
        entry["swallowed_exceptions"] = len(ast_grep_result["findings"])
    if jscpd_result is not None:
        entry["duplication_pct"] = jscpd_result.get("duplication_pct")
    if coverage_result is not None:
        entry["coverage_percent"] = coverage_result.get("percent")
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")
    rows = []
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _write_audit_md(
    pcp_dir: Path, result: dict, timestamp: str, trend: list[dict] | None = None,
    ast_grep_result: dict | None = None, jscpd_result: dict | None = None,
    coverage_result: dict | None = None,
) -> Path:
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

    if ast_grep_result is None:
        lines += ["", "## Swallowed Exceptions (ast-grep)", "",
                   "_ast-grep not detected — `npm install -g @ast-grep/cli` to enable._"]
    else:
        ag_findings = ast_grep_result["findings"]
        lines += ["", "## Swallowed Exceptions (ast-grep)", "",
                   f"Findings: {len(ag_findings)}", ""]
        if ag_findings:
            for f in ag_findings[:MAX_FINDINGS_SHOWN]:
                lines.append(f"- {f}")
            if len(ag_findings) > MAX_FINDINGS_SHOWN:
                lines.append(f"- _...and {len(ag_findings) - MAX_FINDINGS_SHOWN} more_")
        else:
            lines.append("_No findings._")

    if jscpd_result is None:
        lines += ["", "## Duplication (jscpd)", "",
                   "_jscpd not detected — `npm install -g jscpd` to enable._"]
    else:
        pct = jscpd_result.get("duplication_pct")
        jc_findings = jscpd_result["findings"]
        lines += ["", "## Duplication (jscpd)", "",
                   f"Duplicated: {pct:.2f}%" if pct is not None else "Duplicated: unknown", ""]
        if jc_findings:
            for f in jc_findings[:MAX_FINDINGS_SHOWN]:
                lines.append(f"- {f}")
            if len(jc_findings) > MAX_FINDINGS_SHOWN:
                lines.append(f"- _...and {len(jc_findings) - MAX_FINDINGS_SHOWN} more_")
        else:
            lines.append("_No duplicated blocks found._")

    if coverage_result is None:
        pass  # not requested this run -- distinct from "tool not found"
    elif coverage_result.get("tool") is None:
        lines += ["", "## Test Coverage", "",
                   "_No coverage tool detected (coverage.py / npm coverage script) — skipped._"]
    else:
        pct = coverage_result.get("percent")
        threshold = coverage_result.get("threshold")
        lines += ["", "## Test Coverage", "",
                   f"{pct:.0f}% ({coverage_result['tool']})" if pct is not None else "unknown"]
        if coverage_result.get("below_threshold"):
            lines.append(f"\n⚠ Below advisory threshold ({threshold}%). Tracked, not blocking.")

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
@click.option("--coverage", "with_coverage", is_flag=True,
              help="Also run the test suite under coverage and warn if below "
                   "PCP_COVERAGE_ADVISORY_THRESHOLD (default 50). Opt-in: runs the "
                   "full suite instrumented, real cost -- same posture as `pcp scan --coverage`.")
def audit(project_path: str | None, quiet: bool, with_coverage: bool):
    """Advisory dead-code / bloat scan. Writes .pcp/audit.md. Never blocks."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    project_root = pcp_dir.parent
    result = _run_audit(project_root)
    ast_grep_result = _run_ast_grep_swallowed_exceptions(project_root)
    jscpd_result = _run_jscpd(project_root)
    coverage_result = _run_coverage_check(project_root) if with_coverage else None
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    metrics = _source_metrics(project_root)
    trend = _append_trend(pcp_dir, timestamp, result, metrics, ast_grep_result, jscpd_result, coverage_result)
    out_path = _write_audit_md(pcp_dir, result, timestamp, trend, ast_grep_result, jscpd_result, coverage_result)

    if quiet:
        sys.exit(0)

    if result["tool"] is None:
        console.print("[dim]No dead-code tool detected (vulture or knip) — skipped.[/dim]")
    else:
        count = len(result["findings"])
        color = "green" if count == 0 else "yellow"
        console.print(f"[{color}]{count} dead-code finding(s)[/{color}] ([dim]{result['tool']}[/dim])  →  {out_path.relative_to(project_root)}")

    if ast_grep_result is None:
        console.print("[dim]ast-grep not detected — swallowed-exception scan skipped.[/dim]")
    else:
        count = len(ast_grep_result["findings"])
        color = "green" if count == 0 else "yellow"
        console.print(f"[{color}]{count} swallowed-exception finding(s)[/{color}] ([dim]ast-grep[/dim])")

    if jscpd_result is None:
        console.print("[dim]jscpd not detected — duplication scan skipped.[/dim]")
    else:
        pct = jscpd_result.get("duplication_pct")
        color = "green" if not pct else ("yellow" if pct < 10 else "red")
        console.print(f"[{color}]{pct:.2f}% duplicated[/{color}] ([dim]jscpd[/dim])" if pct is not None
                      else "[dim]jscpd ran but reported no percentage.[/dim]")

    if coverage_result is not None:
        if coverage_result.get("tool") is None:
            console.print("[dim]No coverage tool detected — coverage check skipped.[/dim]")
        else:
            pct = coverage_result.get("percent")
            color = "red" if coverage_result.get("below_threshold") else "green"
            suffix = f" — below {coverage_result['threshold']}% advisory threshold" if coverage_result.get("below_threshold") else ""
            console.print(f"[{color}]{pct:.0f}% coverage[/{color}] ([dim]{coverage_result['tool']}[/dim]){suffix}")

    sys.exit(0)
