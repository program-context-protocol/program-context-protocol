"""pcp check — Layer 1 pre-commit gate (deterministic, no LLM, <1s)."""

import os
import re
import sys
from pathlib import Path

import click
import yaml
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, get_modules_dir, NoPCPDir
from pcp.schema.validator import validate_file, load_yaml

console = Console()

BYPASS_MARKER = re.compile(r"\[pcp-bypass:\s*(.+?)\]", re.IGNORECASE)
BYPASS_LOG = "bypass_log.yaml"


def _read_bypass_reason(commit_msg_file: Path | None) -> str | None:
    """Only recognizes the marker when it occupies an ENTIRE line by itself
    (any line in the message, not just the last one). Confirmed bug, twice:
    a paragraph-scoped version of this still self-triggered on a commit
    message whose body was one unbroken multi-line block (no blank line
    inside it) that merely *mentioned* the marker mid-sentence while
    describing this exact fix. Requiring a full-line match is both simpler
    and tighter: prose like "...scope the [pcp-bypass: reason] match to..."
    shares its line with other text and can never match, while genuine usage
    -- the marker alone on its own line, anywhere in the message -- always
    does, matching the documented convention."""
    if not commit_msg_file or not commit_msg_file.exists():
        return None
    msg = commit_msg_file.read_text()
    for line in msg.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = BYPASS_MARKER.fullmatch(line.strip())
        if m:
            return m.group(1).strip()
    return None


def _log_bypass(pcp_dir: Path, reason: str, rules_checked: list[str]) -> None:
    from datetime import datetime, timezone
    from pcp.evidence_chain import chain_entry

    log_path = pcp_dir / BYPASS_LOG
    existing = []
    if log_path.exists():
        data = yaml.safe_load(log_path.read_text()) or {}
        existing = data.get("bypasses", [])

    prev_hash = existing[-1].get("entry_hash") if existing else None
    fields = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reason": reason,
        "rules_bypassed": rules_checked,
    }
    existing.append(chain_entry(prev_hash, fields))

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


def run_protected_path_rule(rule: dict, staged_files: list[str]) -> list[str]:
    """Violations for a check:protected_path ci_rule. Only enforced inside a
    pcp-build agent session (PCP_AGENT_SESSION=1 in the environment, set by
    build.py before spawning the coding agent) — a human's own interactive
    commit (pcp pm, direct editing) never sets this and is never blocked."""
    if os.environ.get("PCP_AGENT_SESSION") != "1":
        return []
    scope = rule.get("scope", [])
    violations = []
    for rel_path in staged_files:
        if _match_scope(rel_path, scope):
            violations.append(
                f"{rel_path}: protected spec file modified by an agent session "
                "(human-owned, never agent-writable)"
            )
    return violations


def get_module_names(pcp_dir: Path) -> list[str]:
    modules_dir = get_modules_dir(pcp_dir)
    if not modules_dir.exists():
        return []
    return sorted(p.name for p in modules_dir.iterdir() if p.is_dir() and (p / "spec.yaml").exists())


def run_file_exists_rule(rule: dict, project_root: Path, module_names: list[str]) -> list[str]:
    """Violations for a check:file_exists ci_rule. Resolves {module}/{MODULE}
    placeholders per-module if present in the target; otherwise checks the
    literal target once. Project-wide structural check, not diff-scoped."""
    target_template = rule.get("target", "")
    violations = []
    if "{module}" in target_template or "{MODULE}" in target_template:
        for name in module_names:
            target = target_template.replace("{module}", name).replace("{MODULE}", name.upper())
            if not (project_root / target).exists():
                violations.append(f"{target}: required file missing (module '{name}')")
    else:
        if target_template and not (project_root / target_template).exists():
            violations.append(f"{target_template}: required file missing")
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
@click.option("--baseline", is_flag=True,
              help="Brownfield: scan all files, write baseline_violations.yaml. Does not block.")
@click.option("--staged-only", is_flag=True,
              help="Brownfield: check staged changes only, exclude baseline_violations.yaml violations.")
def check(project_path: str | None, commit_msg_file: str | None, file_list: str | None,
          baseline: bool, staged_only: bool):
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
    file_rules = [r for r in data.get("rules", []) if r.get("check") == "file_exists"]
    protected_rules = [r for r in data.get("rules", []) if r.get("check") == "protected_path"]
    module_names = get_module_names(pcp_dir)

    if not rules and not file_rules and not protected_rules:
        console.print("[dim]No ast_pattern, file_exists, or protected_path rules in ci_rules.yaml.[/dim]")
        sys.exit(0)

    # Check for bypass
    msg_file = Path(commit_msg_file) if commit_msg_file else None
    bypass_reason = _read_bypass_reason(msg_file)
    if bypass_reason:
        from pcp import policy
        decision = policy.evaluate(pcp_dir, "data.pcp.bypass.approved", {"reason": bypass_reason})
        if decision.get("available") and not decision.get("undefined") and decision.get("value") is False:
            console.print(
                f"[red]pcp-bypass rejected:[/red] '{bypass_reason}' reads as a placeholder, "
                "not a real reason (policy: .pcp/policies/bypass_approval.rego)."
            )
            console.print("[dim]Give a specific, verifiable reason — not \"reason\"/\"todo\"/\"test\"/\"fixme\".[/dim]")
            sys.exit(1)

        rule_ids = [r["id"] for r in rules + file_rules + protected_rules]
        _log_bypass(pcp_dir, bypass_reason, rule_ids)
        from pcp import telemetry
        telemetry.record(
            pcp_dir, cycle="qa", cycle_number=None, check="layer1-bypass",
            control_id="CTRL-004", module=None, submodule=None, criterion_id=None,
            files=(file_list.split(",") if file_list else _get_staged_files()),
            result="bypassed", errors=[f"reason: {bypass_reason}"] + [f"rule bypassed: {r}" for r in rule_ids],
            error_count=len(rule_ids),
        )
        console.print(f"[yellow]pcp-bypass:[/yellow] {bypass_reason} (logged to bypass_log.yaml)")
        sys.exit(0)

    # Get files to check
    if file_list:
        staged = [f.strip() for f in file_list.split(",") if f.strip()]
    else:
        staged = _get_staged_files()

    # ── Baseline mode: scan everything, write baseline_violations.yaml ────────
    if baseline:
        import subprocess
        all_files_result = subprocess.run(
            ["git", "ls-files"],
            capture_output=True, text=True, cwd=project_root
        )
        all_files = [f.strip() for f in all_files_result.stdout.splitlines() if f.strip()]
        all_violations = []
        for rule in rules:
            violations = _run_ast_rule(rule, all_files, project_root)
            for v in violations:
                all_violations.append({"rule_id": rule["id"], "file": v.split(":")[0], "detail": v})
        for rule in file_rules:
            violations = run_file_exists_rule(rule, project_root, module_names)
            for v in violations:
                all_violations.append({"rule_id": rule["id"], "file": v.split(":")[0], "detail": v})

        from datetime import datetime, timezone
        baseline_path = pcp_dir / "baseline_violations.yaml"
        baseline_data = {
            "violations": all_violations,
            "total": len(all_violations),
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        baseline_path.write_text(yaml.dump(baseline_data, default_flow_style=False))
        console.print(f"[dim]Baseline scan: {len(all_violations)} pre-existing violations → "
                      f"baseline_violations.yaml[/dim]")
        console.print("[dim]These violations are excluded from hard gates (brownfield grace mode).[/dim]")
        sys.exit(0)

    # ── Staged-only mode: exclude violations already in baseline ─────────────
    baseline_keys: set[str] = set()
    if staged_only:
        baseline_path = pcp_dir / "baseline_violations.yaml"
        if baseline_path.exists():
            bd = yaml.safe_load(baseline_path.read_text()) or {}
            for v in bd.get("violations", []):
                baseline_keys.add(v.get("detail", ""))

    if not staged and not file_rules:
        sys.exit(0)

    hard_violations = []
    advisory_violations = []

    for rule in rules:
        violations = _run_ast_rule(rule, staged, project_root)
        if staged_only and baseline_keys:
            violations = [v for v in violations if v not in baseline_keys]
        if not violations:
            continue
        severity = rule.get("severity", "advisory")
        entry = {"rule": rule, "violations": violations}
        if severity == "hard_block":
            hard_violations.append(entry)
        else:
            advisory_violations.append(entry)

    # protected_path rules are diff-scoped, like ast_pattern — only fire inside
    # a pcp-build agent session (see run_protected_path_rule's env-var check).
    for rule in protected_rules:
        violations = run_protected_path_rule(rule, staged)
        if staged_only and baseline_keys:
            violations = [v for v in violations if v not in baseline_keys]
        if not violations:
            continue
        severity = rule.get("severity", "advisory")
        entry = {"rule": rule, "violations": violations}
        if severity == "hard_block":
            hard_violations.append(entry)
        else:
            advisory_violations.append(entry)

    # file_exists rules are structural (project-wide), not diff-scoped — always evaluated.
    for rule in file_rules:
        violations = run_file_exists_rule(rule, project_root, module_names)
        if staged_only and baseline_keys:
            violations = [v for v in violations if v not in baseline_keys]
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
            if r.get("message"):
                console.print(f"    [dim]Fix: {r['message']}[/dim]")

    if hard_violations:
        console.print("[red bold]BLOCKED — hard rule violations:[/red bold]")
        for entry in hard_violations:
            r = entry["rule"]
            console.print(f"  [{r['id']}] {r['name']}")
            for v in entry["violations"][:3]:
                console.print(f"    {v}")
            if r.get("message"):
                console.print(f"    [dim]Fix: {r['message']}[/dim]")
        console.print(
            "\n[dim]To bypass: add [pcp-bypass: reason] to your commit message.[/dim]"
        )
        sys.exit(1)

    if not advisory_violations:
        console.print("[green]✓  All rules passed.[/green]")

    sys.exit(0)
