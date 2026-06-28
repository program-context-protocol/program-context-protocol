"""pcp check — Layer 1 pre-commit gate (deterministic, no LLM, <1s)."""

import re
import sys
from pathlib import Path

import click
import yaml
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, NoPCPDir
from pcp.schema.validator import validate_file, load_yaml

console = Console()

BYPASS_MARKER = re.compile(r"\[pcp-bypass:\s*(.+?)\]", re.IGNORECASE)
BYPASS_LOG = "bypass_log.yaml"


def _read_bypass_reason(commit_msg_file: Path | None) -> str | None:
    if not commit_msg_file or not commit_msg_file.exists():
        return None
    msg = commit_msg_file.read_text()
    m = BYPASS_MARKER.search(msg)
    return m.group(1).strip() if m else None


def _log_bypass(pcp_dir: Path, reason: str, rules_checked: list[str]) -> None:
    log_path = pcp_dir / BYPASS_LOG
    existing = []
    if log_path.exists():
        data = yaml.safe_load(log_path.read_text()) or {}
        existing = data.get("bypasses", [])

    from datetime import datetime, timezone
    entry = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reason": reason,
        "rules_bypassed": rules_checked,
    }
    existing.append(entry)

    with open(log_path, "w") as f:
        yaml.dump({"bypasses": existing}, f, default_flow_style=False)


def _match_scope(file_path: str, scope_patterns: list[str]) -> bool:
    """Return True if file_path matches any scope glob."""
    if not scope_patterns:
        return True
    from fnmatch import fnmatch
    return any(fnmatch(file_path, pat) for pat in scope_patterns)


def _run_ast_rule(rule: dict, staged_files: list[str], project_root: Path) -> list[str]:
    """Return list of violation messages for this rule."""
    pattern = re.compile(rule["pattern"], re.MULTILINE)
    scope = rule.get("scope", [])
    violations = []

    for rel_path in staged_files:
        if not _match_scope(rel_path, scope) and scope:
            continue
        full_path = project_root / rel_path
        if not full_path.exists() or not full_path.is_file():
            continue
        try:
            content = full_path.read_text(errors="replace")
        except OSError:
            continue
        for m in pattern.finditer(content):
            line_no = content[: m.start()].count("\n") + 1
            violations.append(f"{rel_path}:{line_no}: matched pattern /{rule['pattern']}/")

    return violations


def _get_staged_files() -> list[str]:
    import subprocess
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    return [f.strip() for f in result.stdout.splitlines() if f.strip()]


@click.command()
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root (default: cwd, walks up to find .pcp/).")
@click.option("--commit-msg-file", type=click.Path(), default=None,
              help="Path to commit message file (set by git hook).")
@click.option("--files", "file_list", default=None,
              help="Comma-separated file list to check (default: git staged files).")
def check(project_path: str | None, commit_msg_file: str | None, file_list: str | None):
    """Layer 1 pre-commit gate — YAML schema + AST pattern rules. No LLM."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    project_root = pcp_dir.parent
    ci_rules_path = pcp_dir / "ci_rules.yaml"

    if not ci_rules_path.exists():
        console.print("[dim]No ci_rules.yaml found — skipping check.[/dim]")
        sys.exit(0)

    # Validate ci_rules.yaml schema first
    schema_errors = validate_file(ci_rules_path, "ci_rules")
    if schema_errors:
        console.print("[red]ci_rules.yaml schema errors:[/red]")
        for e in schema_errors:
            console.print(f"  {e}")
        sys.exit(1)

    data = load_yaml(ci_rules_path)
    rules = [r for r in data.get("rules", []) if r.get("check") == "ast_pattern"]

    if not rules:
        console.print("[dim]No ast_pattern rules in ci_rules.yaml.[/dim]")
        sys.exit(0)

    # Check for bypass
    msg_file = Path(commit_msg_file) if commit_msg_file else None
    bypass_reason = _read_bypass_reason(msg_file)
    if bypass_reason:
        rule_ids = [r["id"] for r in rules]
        _log_bypass(pcp_dir, bypass_reason, rule_ids)
        console.print(f"[yellow]pcp-bypass:[/yellow] {bypass_reason} (logged to bypass_log.yaml)")
        sys.exit(0)

    # Get files to check
    if file_list:
        staged = [f.strip() for f in file_list.split(",") if f.strip()]
    else:
        staged = _get_staged_files()

    if not staged:
        sys.exit(0)

    hard_violations = []
    advisory_violations = []

    for rule in rules:
        violations = _run_ast_rule(rule, staged, project_root)
        if not violations:
            continue
        severity = rule.get("severity", "advisory")
        entry = {"rule": rule, "violations": violations}
        if severity == "hard_block":
            hard_violations.append(entry)
        else:
            advisory_violations.append(entry)

    if advisory_violations:
        console.print("[yellow]Advisory violations:[/yellow]")
        for entry in advisory_violations:
            r = entry["rule"]
            console.print(f"  [{r['id']}] {r['name']}")
            for v in entry["violations"][:3]:
                console.print(f"    {v}")

    if hard_violations:
        console.print("[red bold]BLOCKED — hard rule violations:[/red bold]")
        for entry in hard_violations:
            r = entry["rule"]
            console.print(f"  [{r['id']}] {r['name']}")
            for v in entry["violations"][:3]:
                console.print(f"    {v}")
        console.print(
            "\n[dim]To bypass: add [pcp-bypass: reason] to your commit message.[/dim]"
        )
        sys.exit(1)

    if not advisory_violations:
        console.print("[green]✓  All rules passed.[/green]")

    sys.exit(0)
