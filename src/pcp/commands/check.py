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


# Rule ID shape is the schema's own constraint (ci_rules.schema.json:
# "^[A-Z]+_?[0-9]+$") -- R001, SEC_002, MOD_A003, etc. Matching only "R\d+"
# would silently fail to scope a bypass for any project using the SEC_/MOD_
# convention, which real ci_rules.yaml files do.
_SCOPED_BYPASS_PREFIX = re.compile(
    r"^((?:[A-Z]+_?[0-9]+)(?:\s*,\s*[A-Z]+_?[0-9]+)*)\s*:\s*(.+)$", re.DOTALL,
)


def _read_bypass_reason(commit_msg_file: Path | None) -> tuple[str, list[str] | None] | None:
    """(reason, scoped_rule_ids) or None. scoped_rule_ids is None for a blanket
    bypass (skips every rule -- the original, still-default behaviour) or a list
    for `[pcp-bypass: R008: reason]` / `[pcp-bypass: R003,R008: reason]`, which
    skips ONLY the named rule(s) and still runs everything else.

    Scoping added 2026-07-30 after a real incident: an `ast_pattern` rule
    (R008) matched its own text inside PCP's generated telemetry.jsonl -- a
    false positive against a file that was never supposed to be scanned (see
    pcp/operational.py) -- and because bypass was all-or-nothing, the one
    genuine false positive silently disabled R001 through R010 together for
    that commit. A human writing `[pcp-bypass: R008: ...]` almost always means
    "this one rule is wrong here", not "skip Layer 1 entirely" -- the blanket
    form remains available for when that IS what's meant, but is no longer the
    only option.

    Only recognizes the marker when it occupies an ENTIRE line by itself (any
    line in the message, not just the last one). Confirmed bug, twice: a
    paragraph-scoped version of this still self-triggered on a commit message
    whose body was one unbroken multi-line block (no blank line inside it)
    that merely *mentioned* the marker mid-sentence while describing this
    exact fix. Requiring a full-line match is both simpler and tighter: prose
    like "...scope the [pcp-bypass: reason] match to..." shares its line with
    other text and can never match, while genuine usage -- the marker alone
    on its own line, anywhere in the message -- always does."""
    if not commit_msg_file or not commit_msg_file.exists():
        return None
    msg = commit_msg_file.read_text()
    for line in msg.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = BYPASS_MARKER.fullmatch(line.strip())
        if not m:
            continue
        body = m.group(1).strip()
        scoped = _SCOPED_BYPASS_PREFIX.match(body)
        if scoped:
            rule_ids = [r.strip().upper() for r in scoped.group(1).split(",")]
            return scoped.group(2).strip(), rule_ids
        return body, None
    return None


def _log_bypass(pcp_dir: Path, reason: str, rules_checked: list[str],
                 files: list[str] | None = None, modules: list[str] | None = None) -> None:
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
        "files": files or [],
        "modules": modules or [],
    }
    existing.append(chain_entry(prev_hash, fields))

    with open(log_path, "w") as f:
        yaml.dump({"bypasses": existing}, f, default_flow_style=False)


def _attributed_modules(project_root: Path, pcp_dir: Path, staged_files: list[str],
                         module_names: list[str]) -> list[str]:
    """Map staged files to the module(s) they belong to, so a bypass entry can
    be placed on that module's own docs/changelog.md timeline instead of
    sitting as an unattributed global entry (the gap CLAUDE.md's Per-Module
    Doc Kit section names explicitly -- bypass_log.yaml has no file/module
    field, so changelog.md excludes bypasses today).

    Two match strategies, both cheap/deterministic (no LLM):
    1. Direct spec-dir match -- a staged file under strategy/modules/<name>/
       belongs to <name>.
    2. Criterion target match -- a staged source file matches a criterion's
       declared `target` path in that module's acceptance.yaml.
    """
    modules_dir = get_modules_dir(pcp_dir)
    matched: set[str] = set()

    for rel_path in staged_files:
        for name in module_names:
            prefix = f"strategy/modules/{name}/"
            if rel_path.startswith(prefix) or rel_path.startswith(".pcp/" + prefix):
                matched.add(name)

    for name in module_names:
        acceptance_path = modules_dir / name / "acceptance.yaml"
        if not acceptance_path.exists():
            continue
        try:
            acc_data = yaml.safe_load(acceptance_path.read_text()) or {}
        except yaml.YAMLError:
            continue
        targets = {c.get("target") for c in acc_data.get("criteria", []) if c.get("target")}
        if not targets:
            continue
        for rel_path in staged_files:
            if rel_path in targets:
                matched.add(name)

    return sorted(matched)


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


def _run_ast_required_rule(rule: dict, project_root: Path) -> list[str]:
    """Violations for a check:ast_pattern rule with require_present: true --
    inverted from ast_pattern's default 'block if pattern found anywhere in a
    changed file' semantics. This is 'block if pattern is found NOWHERE in
    scope' instead -- e.g. 'agent.py must call policy.strip() somewhere', not
    'agent.py must never contain X'. Project-wide like file_exists, not
    diff-scoped like the default ast_pattern rules: this represents a standing
    invariant on the file's current content, checked every time, not only
    when that file happens to be part of the current commit."""
    pattern = re.compile(rule["pattern"], re.MULTILINE)
    scope = rule.get("scope", [])
    if not scope:
        return [f"[{rule['id']}] require_present rule has no scope — cannot check"]

    matched_any_file = False
    for pat in scope:
        for full_path in project_root.glob(pat):
            if not full_path.is_file():
                continue
            matched_any_file = True
            try:
                content = full_path.read_text(errors="replace")
            except OSError:
                continue
            if pattern.search(content):
                return []  # found somewhere in scope -- satisfied

    if not matched_any_file:
        return [f"scope {scope} matched no files — required pattern /{rule['pattern']}/ cannot be verified"]
    return [f"required pattern /{rule['pattern']}/ not found in any file matching {scope}"]


def _git_show_head(project_root: Path, rel_path: str) -> str | None:
    import subprocess
    result = subprocess.run(
        ["git", "show", f"HEAD:{rel_path}"], cwd=project_root, capture_output=True, text=True,
    )
    return result.stdout if result.returncode == 0 else None


def _dequote(text: str) -> str:
    return re.sub(r"""['"\\]""", "", text)


def is_syntax_only_yaml_fix(old_text: str | None, new_text: str) -> bool:
    """True only if new_text is valid YAML AND differs from old_text purely
    by quote/escape characters -- e.g. wrapping an unquoted bullet containing
    a stray colon in quotes so it parses. Deterministic, not an agent's own
    say-so: a real parse-error fix is verifiable by a YAML parser plus a
    de-quoted character comparison, the same way an ast_pattern rule is
    verifiable by a regex rather than trusted on request.

    Returns False (never a "safe" fix) for: new_text still doesn't parse,
    old_text doesn't exist yet (a brand new protected file -- that's real
    content creation, not a fix), or the de-quoted text differs at all
    (a real, non-syntax change hiding inside what's claimed to be a
    formatting fix). Known limitation: doesn't handle a fix that also needed
    to escape a pre-existing internal quote character -- de-quoting both
    sides can't distinguish "added a quote" from "added an escaped quote" in
    that case. Good enough for the reported case (wrapping a colon-containing
    bullet in quotes); flagged here rather than silently over-trusted."""
    try:
        yaml.safe_load(new_text)
    except yaml.YAMLError:
        return False
    if old_text is None:
        return False
    return _dequote(old_text) == _dequote(new_text)


def run_protected_path_rule(rule: dict, staged_files: list[str], project_root: Path | None = None) -> list[str]:
    """Violations for a check:protected_path ci_rule. Only enforced inside a
    pcp-build agent session (PCP_AGENT_SESSION=1 in the environment, set by
    build.py before spawning the coding agent) — a human's own interactive
    commit (pcp pm, direct editing) never sets this and is never blocked.

    Carve-out, added 2026-07-08 after a real recurrence in ontology-foundry:
    a protected file that's currently invalid YAML (blocking validate-strategy/
    architect-review project-wide) and an agent's fix touches ONLY quoting/
    escaping is allowed through -- verified by is_syntax_only_yaml_fix(),
    not by trusting the agent's own claim that "it's just a syntax fix"."""
    if os.environ.get("PCP_AGENT_SESSION") != "1":
        return []
    scope = rule.get("scope", [])
    violations = []
    for rel_path in staged_files:
        if not _match_scope(rel_path, scope):
            continue
        if project_root is not None:
            new_path = project_root / rel_path
            if new_path.exists():
                old_text = _git_show_head(project_root, rel_path)
                new_text = new_path.read_text(errors="replace")
                if is_syntax_only_yaml_fix(old_text, new_text):
                    continue
        violations.append(
            f"{rel_path}: protected spec file modified by an agent session "
            "(human-approved: use `pcp correct-objective` / `pcp pm` / "
            "`pcp amend` — diff shown, human approves, then written)"
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
    """Staged files a gate should actually evaluate.

    PCP's own generated output is excluded. It is machine-written record *about*
    the code — telemetry, ledgers, scans, evidence — never authored content, and
    evaluating a rule against it is not just noise, it is circular: telemetry
    records the findings of the rules, so a rule's own pattern text ends up
    written into the file the next commit stages and scans.

    That is not hypothetical. ontology-foundry, 2026-07-30, from bypass_log.yaml:

        reason: R008 matched its own rule text quoted inside generated
                telemetry.jsonl, not a real property_hints persistence

    And the blast radius was total, because a `[pcp-bypass]` is all-or-nothing
    across rules: that single self-match bypassed **R001 through R010 together**,
    on an unattended run, with nobody reading the output. One false positive from
    a file PCP wrote itself voided the entire Layer 1 gate for that commit.

    See pcp/operational.py for the path list and why it lives in its own module.
    """
    import subprocess

    from pcp.operational import filter_operational

    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    staged = [f.strip() for f in result.stdout.splitlines() if f.strip()]
    keep, skipped = filter_operational(staged)
    if skipped:
        # Say what was skipped. A gate that silently narrows its own scope is
        # indistinguishable from one that found nothing.
        console.print(
            f"[dim]Layer 1: skipping {len(skipped)} PCP-generated file(s) — "
            f"machine-written records, not authored content "
            f"({', '.join(skipped[:4])}{'…' if len(skipped) > 4 else ''}).[/dim]"
        )
    return keep


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
    rules = [r for r in data.get("rules", []) if r.get("check") == "ast_pattern" and not r.get("require_present")]
    required_rules = [r for r in data.get("rules", []) if r.get("check") == "ast_pattern" and r.get("require_present")]
    file_rules = [r for r in data.get("rules", []) if r.get("check") == "file_exists"]
    protected_rules = [r for r in data.get("rules", []) if r.get("check") == "protected_path"]
    module_names = get_module_names(pcp_dir)

    if not rules and not required_rules and not file_rules and not protected_rules:
        console.print("[dim]No ast_pattern, file_exists, or protected_path rules in ci_rules.yaml.[/dim]")
        sys.exit(0)

    # Check for bypass
    msg_file = Path(commit_msg_file) if commit_msg_file else None
    bypass_parsed = _read_bypass_reason(msg_file)
    if bypass_parsed:
        bypass_reason, scoped_rule_ids = bypass_parsed
        from pcp import policy
        decision = policy.evaluate(pcp_dir, "data.pcp.bypass.approved", {"reason": bypass_reason})
        if decision.get("available") and not decision.get("undefined") and decision.get("value") is False:
            console.print(
                f"[red]pcp-bypass rejected:[/red] '{bypass_reason}' reads as a placeholder, "
                "not a real reason (policy: .pcp/policies/bypass_approval.rego)."
            )
            console.print("[dim]Give a specific, verifiable reason — not \"reason\"/\"todo\"/\"test\"/\"fixme\".[/dim]")
            sys.exit(1)

        all_rules = rules + required_rules + file_rules + protected_rules
        all_ids = {r["id"] for r in all_rules}
        if scoped_rule_ids is None:
            bypassed_ids = [r["id"] for r in all_rules]
        else:
            unknown = [r for r in scoped_rule_ids if r not in all_ids]
            if unknown:
                console.print(
                    f"[yellow]pcp-bypass warning:[/yellow] {', '.join(unknown)} not found in "
                    f"ci_rules.yaml — nothing to bypass for {'that id' if len(unknown) == 1 else 'those ids'}."
                )
            bypassed_ids = [r for r in scoped_rule_ids if r in all_ids]

        bypass_files = file_list.split(",") if file_list else _get_staged_files()
        bypass_modules = _attributed_modules(project_root, pcp_dir, bypass_files, module_names)
        _log_bypass(pcp_dir, bypass_reason, bypassed_ids, files=bypass_files, modules=bypass_modules)
        from pcp import telemetry
        telemetry.record(
            pcp_dir, cycle="qa", cycle_number=None, check="layer1-bypass",
            control_id="CTRL-004", module=(bypass_modules[0] if bypass_modules else None),
            submodule=None, criterion_id=None,
            files=bypass_files,
            result="bypassed", errors=[f"reason: {bypass_reason}"] + [f"rule bypassed: {r}" for r in bypassed_ids],
            error_count=len(bypassed_ids),
        )

        if scoped_rule_ids is None:
            console.print(f"[yellow]pcp-bypass:[/yellow] {bypass_reason} (logged to bypass_log.yaml)")
            sys.exit(0)

        # Scoped: drop only the named rule(s) and keep going -- everything else
        # in this commit is still checked normally. This is the whole point of
        # scoping; exiting here would silently re-create the all-or-nothing bug.
        console.print(
            f"[yellow]pcp-bypass ({', '.join(bypassed_ids)}):[/yellow] {bypass_reason} "
            "(logged to bypass_log.yaml — other rules still run)"
        )
        rules = [r for r in rules if r["id"] not in bypassed_ids]
        required_rules = [r for r in required_rules if r["id"] not in bypassed_ids]
        file_rules = [r for r in file_rules if r["id"] not in bypassed_ids]
        protected_rules = [r for r in protected_rules if r["id"] not in bypassed_ids]

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
        for rule in required_rules:
            violations = _run_ast_required_rule(rule, project_root)
            for v in violations:
                all_violations.append({"rule_id": rule["id"], "file": None, "detail": v})

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

    if not staged and not file_rules and not required_rules:
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
        violations = run_protected_path_rule(rule, staged, project_root)
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

    # require_present ast_pattern rules are structural (project-wide), like file_exists.
    for rule in required_rules:
        violations = _run_ast_required_rule(rule, project_root)
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
