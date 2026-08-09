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
from pcp.test_composition import analyze_test_composition
from pcp.mutation_confirm import (
    cosmic_ray_available, resolve_definition_file_all, run_targeted_mutation_test,
)
from pcp.flaky_detect import flaky_detect_available, run_flaky_detection

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


def _run_falsegreen(project_root: Path) -> dict | None:
    """Prior-art integration (2026-08-08): real, broader test smell coverage
    than this project's own hand-built test_composition.py -- falsegreen
    (MIT, zero-dependency AST scanner, github.com/vinicq/falsegreen) covers
    47 false-green codes: always-true assertions, self-comparison, assertions
    inside a conditional that may never execute, assertions swallowed by
    try/except, unreachable assertions in dead code, no assertions at all.
    Complementary, not a replacement -- test_composition.py's grep-shaped
    check (assert "Name" in file_contents) isn't one of falsegreen's named
    codes, so both stay wired in rather than one subsuming the other.

    Cheap enough to run unconditionally like ast-grep/jscpd above (a
    zero-dependency static AST pass, no test execution) -- unlike the
    coverage/mutation-confirm checks, which are real-cost and opt-in."""
    if not shutil.which("falsegreen"):
        return None
    result = subprocess.run(
        ["falsegreen", "--format", "json", "."],
        capture_output=True, text=True, cwd=project_root,
    )
    try:
        records = json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError:
        return {"tool": "falsegreen", "findings": []}
    findings = [
        f"{r.get('file', '?')}:{r.get('line', '?')}: [{r.get('code', '?')}] {r.get('title', '')}"
        for r in records
    ]
    return {"tool": "falsegreen", "findings": findings}


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


DEFAULT_GREP_SHAPED_ADVISORY_THRESHOLD = 50


def _run_test_composition_check(project_root: Path) -> dict:
    """Real finding (2026-08-08): a project's test suite -- 255 tests, 100%
    passing -- was 97% `assert "Name" in file_contents` (source-text grep),
    3% real execution against a compiled binary. All 255 were honest,
    passing tests; that number alone said nothing about which tier of
    verification they represented. Pure stdlib AST, no external tool, no
    LLM -- always runs, unlike vulture/jscpd/etc which need a tool detected.

    Advisory threshold mirrors _run_coverage_check's own pattern:
    PCP_GREP_SHAPED_ADVISORY_THRESHOLD (default 50%) -- flagged, never
    blocking, same report-first posture every new check in this repo earns
    hard-block status through only after a measured false-positive rate."""
    result = analyze_test_composition(project_root)
    threshold = int(os.environ.get("PCP_GREP_SHAPED_ADVISORY_THRESHOLD", str(DEFAULT_GREP_SHAPED_ADVISORY_THRESHOLD)))
    result["threshold"] = threshold
    result["above_threshold"] = (
        result["total_test_functions"] > 0 and result["grep_shaped_ratio"] * 100 > threshold
    )
    return result


DEFAULT_MUTATION_CONFIRM_MAX = 5


def _run_mutation_confirm_check(project_root: Path, test_composition_result: dict) -> dict | None:
    """Opt-in, real-cost empirical follow-up to the free static grep-shaped
    flag above -- see mutation_confirm.py's module docstring for why this
    is scoped to individual flagged functions, not a whole-module sweep.

    Returns None when cosmic-ray isn't installed, same "tool not detected,
    skip gracefully" posture as vulture/knip/ast-grep/jscpd -- distinct from
    _run_test_composition_check, which has no external-tool dependency and
    always runs.

    Capped at PCP_MUTATION_CONFIRM_MAX (default 5) candidates per run --
    each one is a real subprocess chain (init/baseline/exec/dump), genuine
    wall-clock cost, same posture as the coverage check's own real cost.
    Candidates are every (test, target-name) pair test_composition.py
    flagged, in file order -- no attempt to rank by "importance" here,
    since that would need judgment this deterministic pass doesn't have."""
    if not cosmic_ray_available():
        return None

    max_confirm = int(os.environ.get("PCP_MUTATION_CONFIRM_MAX", str(DEFAULT_MUTATION_CONFIRM_MAX)))
    candidates = []
    for file_result in test_composition_result.get("files", []):
        test_file = Path(file_result["path"])
        for g in file_result.get("grep_shaped_functions", []):
            for target in g.get("targets", []):
                candidates.append({"test_name": g["test_name"], "target_name": target, "test_file": test_file})

    confirmations = []
    skipped_no_definition = 0
    for c in candidates[:max_confirm]:
        matches = resolve_definition_file_all(project_root, c["target_name"])
        if len(matches) != 1:
            skipped_no_definition += 1
            continue
        outcome = run_targeted_mutation_test(project_root, c["target_name"], matches[0], c["test_file"])
        confirmations.append({**c, **outcome, "definition_file": str(matches[0].relative_to(project_root))})

    return {
        "candidates_found": len(candidates),
        "confirmed_this_run": len(confirmations),
        "skipped_ambiguous_or_missing_definition": skipped_no_definition,
        "skipped_over_cap": max(0, len(candidates) - max_confirm),
        "results": confirmations,
    }


DEFAULT_FLAKY_DETECT_RUNS = 3


def _run_flaky_detect_check(project_root: Path) -> dict | None:
    """Opt-in, real cost: reruns the FULL suite PCP_FLAKY_DETECT_RUNS times
    (default 3) and reports tests that flipped pass/fail across identical
    code -- see flaky_detect.py's module docstring for the prior-art check
    and why this is a separate facet from test_composition.py/falsegreen
    (those are static -- this is the only one that actually re-executes).

    Returns None when this project has no detectable pytest -- same
    "tool not applicable here" posture as the coverage/mutation-confirm
    checks, distinct from a real zero-flaky-tests result."""
    if not flaky_detect_available(project_root):
        return None
    runs = int(os.environ.get("PCP_FLAKY_DETECT_RUNS", str(DEFAULT_FLAKY_DETECT_RUNS)))
    return run_flaky_detection(project_root, runs=runs)


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
    coverage_result: dict | None = None, test_composition_result: dict | None = None,
    falsegreen_result: dict | None = None, flaky_detect_result: dict | None = None,
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
    if test_composition_result is not None:
        entry["grep_shaped_ratio"] = test_composition_result.get("grep_shaped_ratio")
    if falsegreen_result is not None:
        entry["falsegreen_findings"] = len(falsegreen_result["findings"])
    if flaky_detect_result is not None and flaky_detect_result.get("available"):
        entry["flaky_tests_found"] = len(flaky_detect_result["flaky_tests"])
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
    coverage_result: dict | None = None, test_composition_result: dict | None = None,
    mutation_confirm_result: dict | None = None, falsegreen_result: dict | None = None,
    flaky_detect_result: dict | None = None,
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

    if falsegreen_result is None:
        lines += ["", "## False-Green Tests (falsegreen)", "",
                   "_falsegreen not detected — `pip install falsegreen` to enable. "
                   "Broader test-smell coverage than this project's own Test Composition "
                   "check below (always-true/self-comparison/swallowed/unreachable assertions, "
                   "47 codes) -- complementary, not overlapping._"]
    else:
        fg_findings = falsegreen_result["findings"]
        lines += ["", "## False-Green Tests (falsegreen)", "",
                   f"Findings: {len(fg_findings)}", ""]
        if fg_findings:
            for f in fg_findings[:MAX_FINDINGS_SHOWN]:
                lines.append(f"- {f}")
            if len(fg_findings) > MAX_FINDINGS_SHOWN:
                lines.append(f"- _...and {len(fg_findings) - MAX_FINDINGS_SHOWN} more (see full tool output)_")
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

    if test_composition_result is not None:
        tc = test_composition_result
        lines += ["", "## Test Composition (source-grep vs. real-execution)", "",
                   f"_{tc['scope_note']}_", ""]
        if tc["total_test_functions"] == 0:
            lines.append("_No Python test_*.py / *_test.py files found._")
        else:
            pct = tc["grep_shaped_ratio"] * 100
            lines += [
                f"{tc['total_test_functions']} test function(s): "
                f"**{tc['grep_shaped']} source-grep** ({pct:.0f}%), "
                f"{tc['real_execution']} real-execution, {tc['other']} unclassified.",
                "",
                "Source-grep = `assert \"Name\" in file_contents` — confirms a name exists in a "
                "file, not that calling it produces correct behavior. A passing test suite can be "
                "100% honest and still tell you almost nothing about runtime correctness if this "
                "ratio is high.",
            ]
            if tc.get("above_threshold"):
                lines.append(f"\n⚠ Above advisory threshold ({tc['threshold']}%). Tracked, not blocking.")
            grep_files = sorted(
                (f for f in tc["files"] if f["grep_shaped"] > 0),
                key=lambda f: f["grep_shaped"], reverse=True,
            )
            if grep_files:
                lines += ["", "### Highest source-grep concentration"]
                for f in grep_files[:MAX_FINDINGS_SHOWN]:
                    lines.append(f"- `{f['path']}`: {f['grep_shaped']} source-grep test(s)")
                if len(grep_files) > MAX_FINDINGS_SHOWN:
                    lines.append(f"- _...and {len(grep_files) - MAX_FINDINGS_SHOWN} more file(s)_")

    if mutation_confirm_result is None:
        lines += ["", "## Mutation Confirmation (empirical follow-up)", "",
                   "_cosmic-ray not detected — `pip install cosmic-ray` to enable "
                   "(real cost, opt-in via `--mutation-confirm`)._"]
    else:
        mc = mutation_confirm_result
        lines += ["", "## Mutation Confirmation (empirical follow-up)", "",
                   "Targeted, not a whole-module sweep: mutates only the specific function(s) "
                   "a grep-shaped test claims to check, confirms empirically whether it really "
                   "catches nothing.", ""]
        lines.append(
            f"{mc['candidates_found']} candidate(s) found, {mc['confirmed_this_run']} confirmed this run, "
            f"{mc['skipped_over_cap']} left for a future run (cap), "
            f"{mc['skipped_ambiguous_or_missing_definition']} skipped (ambiguous or no resolvable definition)."
        )
        if mc["results"]:
            lines += ["", "| Test | Target | Mutation score | Verdict |", "|---|---|---|---|"]
            for r in mc["results"]:
                if not r.get("available") or "error" in r:
                    verdict = f"error: {r.get('error', 'cosmic-ray unavailable')[:80]}"
                    score = "—"
                elif r["mutation_score"] is None:
                    verdict = "no mutants — unjudgeable"
                    score = "—"
                elif r["confirms_grep_shaped"]:
                    verdict = "⚠ CONFIRMED grep-shaped (0% caught)"
                    score = "0%"
                else:
                    verdict = "real assertions — caught real mutations"
                    score = f"{r['mutation_score'] * 100:.0f}%"
                lines.append(f"| `{r['test_name']}` | `{r['target_name']}` | {score} | {verdict} |")

    if flaky_detect_result is None or not flaky_detect_result.get("available"):
        lines += ["", "## Flaky Tests (proactive)", "",
                   "_No pytest detected — flaky-test detection skipped._"]
    else:
        fd = flaky_detect_result
        order_note = ("order randomized each run (pytest-randomly detected)" if fd["order_randomized"]
                      else "same order each run (pytest-randomly not installed — order-dependent "
                           "flakiness may go undetected; `pip install pytest-randomly` to widen coverage)")
        lines += ["", "## Flaky Tests (proactive)", "",
                   f"Reran the full suite {fd['runs']} time(s) against identical code, {order_note}. "
                   f"{fd['total_unique_tests']} unique test(s) observed.", ""]
        if fd.get("any_run_timed_out"):
            lines.append("⚠ At least one run timed out or crashed — results are a lower bound.\n")
        if fd["flaky_tests"]:
            lines += ["| Test | Outcomes across runs |", "|---|---|"]
            for ft in fd["flaky_tests"][:MAX_FINDINGS_SHOWN]:
                lines.append(f"| `{ft['test_id']}` | {' → '.join(o or 'missing' for o in ft['outcomes'])} |")
            if len(fd["flaky_tests"]) > MAX_FINDINGS_SHOWN:
                lines.append(f"| _...and {len(fd['flaky_tests']) - MAX_FINDINGS_SHOWN} more_ | |")
        else:
            lines.append("_No test flipped pass/fail across runs._")

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
@click.option("--mutation-confirm", "with_mutation_confirm", is_flag=True,
              help="Empirically confirm the highest-priority grep-shaped test findings by mutating "
                   "ONLY the specific flagged function(s) and checking whether the test really catches "
                   "nothing (cosmic-ray, real cost, capped at PCP_MUTATION_CONFIRM_MAX per run, default 5). "
                   "Requires cosmic-ray installed (`pip install cosmic-ray`); skipped gracefully if absent.")
@click.option("--flaky-detect", "with_flaky_detect", is_flag=True,
              help="Proactively find flaky tests by rerunning the FULL suite PCP_FLAKY_DETECT_RUNS times "
                   "(default 3) against identical code and diffing per-test outcomes. Real cost -- N full "
                   "suite runs. Order-randomized if pytest-randomly is installed (recommended: "
                   "`pip install pytest-randomly`, catches the most common flaky-test root cause).")
def audit(project_path: str | None, quiet: bool, with_coverage: bool, with_mutation_confirm: bool,
          with_flaky_detect: bool):
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
    falsegreen_result = _run_falsegreen(project_root)
    coverage_result = _run_coverage_check(project_root) if with_coverage else None
    test_composition_result = _run_test_composition_check(project_root)
    mutation_confirm_result = (
        _run_mutation_confirm_check(project_root, test_composition_result) if with_mutation_confirm else None
    )
    flaky_detect_result = _run_flaky_detect_check(project_root) if with_flaky_detect else None
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    metrics = _source_metrics(project_root)
    trend = _append_trend(
        pcp_dir, timestamp, result, metrics, ast_grep_result, jscpd_result, coverage_result,
        test_composition_result, falsegreen_result, flaky_detect_result,
    )
    out_path = _write_audit_md(
        pcp_dir, result, timestamp, trend, ast_grep_result, jscpd_result, coverage_result,
        test_composition_result, mutation_confirm_result, falsegreen_result, flaky_detect_result,
    )

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

    if falsegreen_result is None:
        console.print("[dim]falsegreen not detected — false-green test scan skipped (`pip install falsegreen`).[/dim]")
    else:
        count = len(falsegreen_result["findings"])
        color = "green" if count == 0 else "yellow"
        console.print(f"[{color}]{count} false-green test finding(s)[/{color}] ([dim]falsegreen[/dim])")

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

    if test_composition_result["total_test_functions"] == 0:
        console.print("[dim]No Python test_*.py / *_test.py files found — test-composition check skipped.[/dim]")
    else:
        tc = test_composition_result
        pct = tc["grep_shaped_ratio"] * 100
        color = "red" if tc.get("above_threshold") else ("yellow" if pct > 0 else "green")
        suffix = f" — above {tc['threshold']}% advisory threshold" if tc.get("above_threshold") else ""
        console.print(
            f"[{color}]{pct:.0f}% source-grep tests[/{color}] "
            f"({tc['grep_shaped']}/{tc['total_test_functions']}, {tc['real_execution']} real-execution){suffix}"
        )

    if with_mutation_confirm:
        if mutation_confirm_result is None:
            console.print("[dim]cosmic-ray not detected — mutation-confirm skipped (`pip install cosmic-ray`).[/dim]")
        else:
            mc = mutation_confirm_result
            confirmed = sum(1 for r in mc["results"] if r.get("confirms_grep_shaped"))
            color = "red" if confirmed else "green"
            console.print(
                f"[{color}]{confirmed}/{mc['confirmed_this_run']} confirmed grep-shaped[/{color}] "
                f"(mutation-tested, {mc['candidates_found']} candidate(s) found)"
            )

    if with_flaky_detect:
        if flaky_detect_result is None or not flaky_detect_result.get("available"):
            console.print("[dim]No pytest detected — flaky-detect skipped.[/dim]")
        else:
            fd = flaky_detect_result
            count = len(fd["flaky_tests"])
            color = "red" if count else "green"
            order_tag = "randomized" if fd["order_randomized"] else "same order"
            console.print(
                f"[{color}]{count} flaky test(s) found[/{color}] "
                f"({fd['runs']} run(s), {order_tag}, {fd['total_unique_tests']} test(s) observed)"
            )

    sys.exit(0)
