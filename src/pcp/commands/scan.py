"""pcp scan — auto-generate current_state.md from acceptance criteria."""

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, get_modules_dir, NoPCPDir
from pcp.schema.validator import validate_file, load_yaml
from pcp.pcp_status import write_pcp_md
from pcp.discovery.scanner import detect_stack, collect_source_files
from pcp import qa
from pcp import uat

console = Console()

# Per-process caches: one `pcp scan` invocation is one process, so these are
# safe to keep for the lifetime of the run and avoid re-globbing/re-reading
# the whole repo once per acceptance criterion.
_SOURCE_FILES_CACHE: dict[Path, list[Path]] = {}
_FILE_CONTENT_CACHE: dict[Path, str] = {}


def _project_source_files(project_root: Path) -> list[Path]:
    cached = _SOURCE_FILES_CACHE.get(project_root)
    if cached is None:
        stack = detect_stack(project_root)
        cached = collect_source_files(project_root, stack)
        _SOURCE_FILES_CACHE[project_root] = cached
    return cached


def _read_cached(path: Path) -> str:
    content = _FILE_CONTENT_CACHE.get(path)
    if content is None:
        content = path.read_text(errors="replace")
        _FILE_CONTENT_CACHE[path] = content
    return content


def _check_file_exists(target: str, project_root: Path) -> tuple[bool, str]:
    path = project_root / target
    if path.exists():
        return True, str(path.relative_to(project_root))

    # Fallback: file may have moved/been renamed during a refactor.
    # Search for a same-basename file elsewhere in the tree before failing.
    basename = Path(target).name
    for f in _project_source_files(project_root):
        if f.name == basename:
            rel = f.relative_to(project_root)
            return True, f"{target}: not at declared path, found moved to {rel}"

    return False, f"{target}: not found (declared path or elsewhere)"


def _check_ast_pattern(target: str, pattern: str, project_root: Path) -> tuple[bool, str]:
    path = project_root / target
    # Real bug, found 2026-07-08: path.exists() is True for a directory too,
    # and directories don't have text content -- _read_cached(path).read_text()
    # raised an unhandled IsADirectoryError that crashed the whole `pcp scan`
    # run outright, whenever any acceptance criterion's ast_pattern target
    # happened to be a directory rather than a file. is_file() correctly
    # falls through to the same repo-wide fallback search used for a target
    # that's simply missing.
    if path.is_file():
        if re.search(pattern, _read_cached(path), re.MULTILINE):
            return True, f"pattern found in {target}"

    # Fallback: feature may have been absorbed into a differently-named file
    # during a refactor. Search other source files for the same pattern
    # before declaring the criterion incomplete.
    for f in _project_source_files(project_root):
        if f == path:
            continue
        if re.search(pattern, _read_cached(f), re.MULTILINE):
            rel = f.relative_to(project_root)
            return True, f"pattern not found in {target}, found instead in {rel} — spec target may be stale"

    return False, f"pattern not found in {target} or elsewhere in repo"


def _load_prior_manual_status(current_state_path: Path) -> dict[str, str]:
    """Parse prior current_state.md to preserve manual criterion statuses."""
    if not current_state_path.exists():
        return {}
    statuses = {}
    content = current_state_path.read_text()
    for line in content.splitlines():
        # Format: `- [x] MODULE/ID: description` or `- [ ] MODULE/ID: description`
        m = re.match(r"- \[([ x])\] ([A-Z]+/[A-Z][0-9]+):", line.strip())
        if m:
            done = m.group(1) == "x"
            key = m.group(2)
            statuses[key] = "complete" if done else "pending"
    return statuses


def _evaluate_criterion(
    criterion: dict,
    module_name: str,
    project_root: Path,
    prior_manual: dict[str, str],
    spec: dict,
    pcp_dir: Path | None = None,
) -> tuple[str, str]:
    """Returns (status, detail)."""
    cid = criterion["id"]
    check = criterion.get("check", "manual")
    target = criterion.get("target", "")
    pattern = criterion.get("pattern", "")

    if check == "file_exists":
        ok, detail = _check_file_exists(target, project_root)
        return ("complete" if ok else "pending"), detail

    elif check == "ast_pattern":
        if not pattern:
            return criterion.get("status", "pending"), "no pattern defined"
        ok, detail = _check_ast_pattern(target, pattern, project_root)
        return ("complete" if ok else "pending"), detail

    elif check == "test_passes":
        return criterion.get("status", "pending"), "test_passes: preserved (run tests to update)"

    elif check == "url_responds":
        ok, detail = uat.check_url_responds(criterion.get("url", ""))
        return ("complete" if ok else "pending"), detail

    elif check == "dom_contains":
        ok, detail = uat.check_dom_contains(criterion.get("url", ""), criterion.get("selector", ""))
        return ("complete" if ok else "pending"), detail

    elif check == "visual":
        screenshot_path = None
        if pcp_dir:
            screenshot_path = pcp_dir / "evidence" / "_visual" / module_name / f"{cid}.png"
        ok, detail = uat.check_visual(criterion.get("url", ""), screenshot_path)
        if ok is None:
            # Optional dependency not installed -- "could not check", not a
            # verdict. Preserve prior status rather than downgrading it, same
            # posture the manual fallback below already uses.
            return criterion.get("status", "pending"), detail
        return ("complete" if ok else "pending"), detail

    else:  # manual
        key = f"{module_name.upper()}/{cid}"
        prior = prior_manual.get(key)
        if prior:
            return prior, "manual (preserved from prior scan)"
        return criterion.get("status", "pending"), "manual"


def _scan_module(
    module_name: str,
    acceptance_path: Path,
    project_root: Path,
    prior_manual: dict[str, str],
    pcp_dir: Path | None = None,
) -> dict:
    errors = validate_file(acceptance_path, "module_acceptance")
    if errors:
        console.print(f"[yellow]⚠  {module_name}/acceptance.yaml: schema errors[/yellow]")
        for e in errors:
            console.print(f"   {e}")

    data = load_yaml(acceptance_path)
    criteria = data.get("criteria", [])
    results = []

    for c in criteria:
        status, detail = _evaluate_criterion(c, module_name, project_root, prior_manual, data, pcp_dir)
        results.append({
            "id": c["id"],
            "description": c["description"],
            "check": c.get("check", "manual"),
            "status": status,
            "detail": detail,
        })

    return {"module": module_name, "criteria": results}


def _write_current_state(pcp_dir: Path, modules_results: list[dict], timestamp: str, coverage: dict | None = None) -> None:
    total = sum(len(m["criteria"]) for m in modules_results)
    complete = sum(
        1 for m in modules_results for c in m["criteria"] if c["status"] == "complete"
    )
    score = complete / total if total else 0.0

    lines = [
        "# Current State",
        f"Generated: {timestamp}",
        "",
        "## Summary",
        f"- Total criteria: {total}",
        f"- Complete: {complete} ({score:.0%})",
        f"- Pending: {total - complete}",
        "",
        "## Module Status",
    ]

    for m in modules_results:
        lines.append(f"\n### {m['module']}")
        for c in m["criteria"]:
            mark = "x" if c["status"] == "complete" else " "
            key = f"{m['module'].upper()}/{c['id']}"
            lines.append(f"- [{mark}] {key}: {c['description']}")
            if c["detail"] and c["detail"] not in ("manual", ""):
                lines.append(f"  > {c['detail']}")

    lines += [
        "",
        "## Drift Score",
        f"acceptance coverage: {score:.2f}",
        "",
    ]

    if coverage and coverage.get("tool") and coverage.get("percent") is not None:
        lines += [
            "## Test Coverage",
            f"{coverage['percent']:.0f}% ({coverage['tool']})",
            "",
        ]

    out = pcp_dir / "current_state.md"
    out.write_text("\n".join(lines))
    return out


@click.command()
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root (default: cwd, walks up to find .pcp/).")
@click.option("--quiet", is_flag=True, help="Suppress output.")
@click.option("--coverage", "with_coverage", is_flag=True,
              help="Also run the test suite under coverage and record %% covered. "
                   "Off by default — runs the full suite, slower than a plain scan.")
def scan(project_path: str | None, quiet: bool, with_coverage: bool):
    """Auto-generate .pcp/current_state.md from acceptance criteria."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    project_root = pcp_dir.parent
    modules_dir = get_modules_dir(pcp_dir)

    if not modules_dir.exists():
        console.print("[yellow]No modules found in .pcp/strategy/modules/.[/yellow]")
        sys.exit(0)

    current_state_path = pcp_dir / "current_state.md"
    prior_manual = _load_prior_manual_status(current_state_path)

    acceptance_files = sorted(modules_dir.glob("*/acceptance.yaml"))
    if not acceptance_files:
        console.print("[yellow]No acceptance.yaml files found.[/yellow]")
        sys.exit(0)

    modules_results = []
    for af in acceptance_files:
        module_name = af.parent.name
        result = _scan_module(module_name, af, project_root, prior_manual, pcp_dir)
        modules_results.append(result)

    coverage = None
    if with_coverage:
        if not quiet:
            console.print("[dim]Running test suite under coverage...[/dim]")
        coverage = qa.run_coverage(project_root)
        if not quiet and not coverage.get("tool"):
            console.print("[dim]No coverage tool detected (coverage/pytest or npm coverage script) — skipped.[/dim]")

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out_path = _write_current_state(pcp_dir, modules_results, timestamp, coverage)

    total = sum(len(m["criteria"]) for m in modules_results)
    complete = sum(1 for m in modules_results for c in m["criteria"] if c["status"] == "complete")

    pcp_md_path = write_pcp_md(pcp_dir, modules_results, timestamp, total, complete)

    if not quiet:
        score = complete / total if total else 0.0
        color = "green" if score >= 0.8 else "yellow" if score >= 0.5 else "red"
        console.print(f"[{color}]{complete}/{total} criteria complete ({score:.0%})[/{color}]  →  {out_path.relative_to(project_root)}  +  {pcp_md_path.name}")
