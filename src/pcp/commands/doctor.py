"""pcp doctor — environment preflight. Checks which CLI integrations are
available, writes .pcp/integrations.yaml. Interactive mode asks for the ones
that need human-provided config (deploy command, health-check URL).

pcp build/watch/deploy call check_environment() at the start of their run in
non-interactive report-only mode — never blocks on missing optional tooling,
only on git/claude (required for the lifecycle to function at all).
"""

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.table import Table

from pcp.pcp_dir import find_pcp_dir, NoPCPDir

console = Console()

REQUIRED_TOOLS = ["git", "claude"]

DEFAULT_SCHEMA_BLOAT_THRESHOLD = 50
DEFAULT_TEST_SCHEMA_PATTERN = "test_%"
_SAFE_SCHEMA_PATTERN_RE = re.compile(r"^[A-Za-z0-9_%-]+$")

DEPLOY_HINT_FILES = {
    "railway.toml": "railway up",
    "vercel.json": "vercel --prod",
    "Procfile": "git push origin main",
    "Dockerfile": None,
}

# Context7 (upstash/context7) injects live, version-specific library docs
# into a coding agent's context instead of relying on stale training data --
# a real mitigation for hallucinated/deprecated API usage, verified real via
# WebSearch 2026-07-17 (github.com/upstash/context7, official, free). Wired
# via the project's own .mcp.json, not a PCP-specific mechanism -- any
# `claude -p` session run with this project as cwd (including pcp build's
# coding-agent subprocess) picks it up automatically once configured.
CONTEXT7_MCP_ENTRY = {"command": "npx", "args": ["-y", "@upstash/context7-mcp@latest"]}


def _which(name: str) -> str | None:
    return shutil.which(name)


def _claude_bin_for_detection() -> str:
    """Real bug, found 2026-07-18 (CI failure, Project O-adjacent):
    llm/client.py's _claude_bin() respects PCP_CLAUDE_BIN so callers can
    substitute a stub agent (real substitution in tests, or a genuinely
    different install path), but this module's own REQUIRED_TOOLS check for
    "claude" only ever looked for a literal `claude` on PATH -- two
    different sources of truth for "is claude available" that silently
    agreed on every dev machine (both true) and silently disagreed the
    moment an environment has a working PCP_CLAUDE_BIN stub but no real
    `claude` binary on PATH (exactly GitHub Actions' ubuntu-latest runners,
    which is why build.py's fatal preflight check blocked CI even though
    several tests deliberately point PCP_CLAUDE_BIN at a fake, working
    agent script). shutil.which() already handles an absolute path
    correctly (bypasses PATH, just checks that exact file is executable),
    so this only needs to feed it the right name."""
    return os.environ.get("PCP_CLAUDE_BIN") or "claude"


def detect_tools() -> dict:
    """Pure detection, no prompts. Used by both `pcp doctor` and the
    non-interactive preflight other commands run automatically."""
    claude_bin = _claude_bin_for_detection()
    return {
        "git": {"available": _which("git") is not None, "path": _which("git")},
        "claude": {"available": _which(claude_bin) is not None, "path": _which(claude_bin)},
        "gh": {"available": _which("gh") is not None, "path": _which("gh")},
        "test_runner": _detect_one(["pytest", "npm", "go"]),
        "lint": _detect_one(["ruff", "eslint"]),
        "sast": _detect_one(["semgrep"]),
        "coverage": _detect_one(["coverage"]),
        "audit": _detect_one(["vulture", "knip"]),
        "slack_notify": {"available": _which("slack-notify") is not None, "path": _which("slack-notify")},
        "npx": {"available": _which("npx") is not None, "path": _which("npx")},
        "opa": _detect_one(["opa"]),
        "temporal": _detect_one(["temporal"]),
        # Only needed if PCP_VERIFIER_CROSS_VENDOR=1 is set (Loop 3's
        # cross-vendor architect-review verifier leg, build.py's
        # _verify_block_findings) -- optional, same posture as gh/opa above.
        "agy": {"available": _which("agy") is not None, "path": _which("agy")},
    }


def _detect_one(candidates: list[str]) -> dict:
    for c in candidates:
        path = _which(c)
        if path:
            return {"tool": c, "available": True, "path": path}
    return {"tool": None, "available": False, "path": None}


def _guess_deploy_command(project_root: Path) -> str | None:
    for fname, cmd in DEPLOY_HINT_FILES.items():
        if (project_root / fname).exists() and cmd:
            return cmd
    return None


def _mcp_config_path(project_root: Path) -> Path:
    return project_root / ".mcp.json"


def detect_context7(project_root: Path) -> dict:
    """Pure detection -- npx availability (needed to run the MCP server) and
    whether .mcp.json already declares it. Project-scoped (needs project_root
    for .mcp.json), so kept separate from detect_tools()'s pure PATH-lookup shape."""
    config_path = _mcp_config_path(project_root)
    configured = False
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text())
            configured = "context7" in (data.get("mcpServers") or {})
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "npx_available": _which("npx") is not None,
        "configured": configured,
        "config_path": str(config_path),
    }


def configure_context7(project_root: Path) -> bool:
    """Adds a context7 entry to .mcp.json, creating the file if absent and
    preserving any other MCP servers already declared there. Returns False
    (and touches nothing) if the file exists but isn't valid JSON -- never
    silently clobber a config a human hand-wrote."""
    config_path = _mcp_config_path(project_root)
    data = {}
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text())
        except json.JSONDecodeError:
            console.print(
                f"[yellow]⚠  {config_path.name} exists but isn't valid JSON -- "
                "skipping Context7 setup, add it manually.[/yellow]"
            )
            return False
    data.setdefault("mcpServers", {})
    data["mcpServers"]["context7"] = dict(CONTEXT7_MCP_ENTRY)
    config_path.write_text(json.dumps(data, indent=2) + "\n")
    return True


def _declared_tiers(pcp_dir: Path) -> set[int]:
    tiers: set[int] = set()
    modules_dir = pcp_dir / "strategy" / "modules"
    if not modules_dir.exists():
        return tiers
    for acc in modules_dir.glob("*/acceptance.yaml"):
        try:
            data = yaml.safe_load(acc.read_text()) or {}
        except yaml.YAMLError:
            continue
        for c in data.get("criteria", []):
            tier = c.get("logic_tier")
            if isinstance(tier, int):
                tiers.add(tier)
    return tiers


def _project_mentions(project_root: Path, names: tuple[str, ...]) -> bool:
    """Cheap check: any of these package names in requirements/pyproject/package.json."""
    for fname in ("requirements.txt", "pyproject.toml", "package.json", "requirements-dev.txt"):
        p = project_root / fname
        if p.exists():
            text = p.read_text(errors="replace").lower()
            if any(n in text for n in names):
                return True
    return False


def _rung_tooling_recommendations(pcp_dir: Path, project_root: Path) -> list[str]:
    """Advisory lines for logic-tier declarations missing their standard
    tooling. Deterministic, zero LLM."""
    tiers = _declared_tiers(pcp_dir)
    recs = []
    if 6 in tiers and not _project_mentions(project_root, ("outlines", "instructor", "guardrails", "baml")):
        recs.append(
            "rung-6 (LLM) criteria declared but no structured-output library found "
            "(outlines/instructor/guardrails/baml) — rung-6 outputs must be schema-validated; "
            "Outlines (constrained decoding) or Instructor (typed retry) are the standard picks"
        )
    if 5 in tiers and not _project_mentions(project_root, ("gptcache", "redis", "litellm", "bifrost")):
        recs.append(
            "rung-5 (cached-reuse) criteria declared but no semantic-cache dependency found — "
            "GPTCache successors: Bifrost (gateway-native), LiteLLM, RedisSemanticCache"
        )
    if 4 in tiers and not _project_mentions(project_root, ("semantic-router", "semantic_router", "chromadb", "faiss", "pinecone", "qdrant", "weaviate")):
        recs.append(
            "rung-4 (RAG) criteria declared but no retrieval dependency found — "
            "semantic-router (pre-LLM intent routing) or a vector store (chroma/faiss/qdrant) expected"
        )
    return recs


def _postgres_url() -> str | None:
    for var in ("DATABASE_URL", "POSTGRES_URL", "PG_DATABASE_URL", "TEST_DATABASE_URL"):
        val = os.environ.get(var)
        if val and "postgres" in val:
            return val
    return None


def check_schema_bloat(threshold: int | None = None, pattern: str | None = None) -> dict | None:
    """Postgres test-schema bloat preflight (2026-07-24, Project O
    incident root cause): a schema-per-test pattern that never tears down
    left 2000+ stray schemas, which correlated directly with pytest timeouts
    under worktree-parallel builds -- looked like a `pcp build` stall from
    the outside, was actually Postgres. Returns None (inert, never blocks)
    if no postgres connection is configured or `psql` isn't on PATH -- pure
    advisory detection, same warn-first posture every other doctor check
    uses. Pattern is restricted to LIKE-safe characters (no quote-breakout)
    since it's interpolated into a query string."""
    url = _postgres_url()
    if not url or not _which("psql"):
        return None
    threshold = threshold if threshold is not None else int(
        os.environ.get("PCP_SCHEMA_BLOAT_THRESHOLD", DEFAULT_SCHEMA_BLOAT_THRESHOLD)
    )
    pattern = pattern or os.environ.get("PCP_TEST_SCHEMA_PATTERN", DEFAULT_TEST_SCHEMA_PATTERN)
    if not _SAFE_SCHEMA_PATTERN_RE.match(pattern):
        return None
    query = f"SELECT count(*) FROM information_schema.schemata WHERE schema_name LIKE '{pattern}'"
    try:
        result = subprocess.run(["psql", url, "-tAc", query], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        count = int(result.stdout.strip())
    except ValueError:
        return None
    return {"count": count, "pattern": pattern, "threshold": threshold, "bloated": count > threshold}


def fix_schema_bloat(pattern: str) -> dict:
    """Drops every schema matching `pattern` -- destructive, the CLI command
    owns confirmation (never called on a bare `pcp doctor`). Returns
    {"dropped": [...], "errors": [...]}."""
    if not _SAFE_SCHEMA_PATTERN_RE.match(pattern):
        return {"dropped": [], "errors": [f"unsafe pattern: {pattern!r}"]}
    url = _postgres_url()
    if not url or not _which("psql"):
        return {"dropped": [], "errors": ["no postgres connection or psql binary detected"]}
    list_query = f"SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE '{pattern}'"
    result = subprocess.run(["psql", url, "-tAc", list_query], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return {"dropped": [], "errors": [result.stderr.strip()]}
    schemas = [s.strip() for s in result.stdout.splitlines() if s.strip()]
    dropped, errors = [], []
    for s in schemas:
        r = subprocess.run(
            ["psql", url, "-c", f'DROP SCHEMA IF EXISTS "{s}" CASCADE'],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            dropped.append(s)
        else:
            errors.append(f"{s}: {r.stderr.strip()}")
    return {"dropped": dropped, "errors": errors}


def load_integrations(pcp_dir: Path) -> dict:
    path = pcp_dir / "integrations.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def check_git_hooks_reachable(project_root: Path) -> dict | None:
    """Are the hooks `pcp init` installed actually reachable by git?

    `pcp init` writes `commit-msg` and `post-commit` into `.git/hooks/`. If
    `core.hooksPath` points somewhere else, git never looks in `.git/hooks/` at
    all -- the files are present, executable, and dead. Nothing reported this, so
    the failure is completely silent: `pcp scan` never re-runs after a commit and
    `current_state.md` quietly ages while the project moves.

    Measured across the local fleet 2026-07-30: **6 of 8 PCP-managed projects had
    `core.hooksPath = ~/.git-hooks`**, a directory containing a single `commit-msg`
    with no PCP reference. So PCP's Layer 1 commit-msg gate and its post-commit
    scan had never fired in any of them. Project A is the clearest case --
    `current_state.md` generated 2026-07-24, then 26 more commits landed and it
    was never regenerated.

    This is a recurrence: the same `core.hooksPath` shadowing was found and fixed
    once before (see the control-catalog work), on one project, by hand. Nothing
    was added to detect it, so it came back everywhere.

    Note `core.hooksPath = .git/hooks` is FINE -- it resolves to the directory git
    would use anyway. Only a path pointing elsewhere is a problem, which is why
    this compares resolved paths rather than testing whether the config is set.

    Returns None when there is nothing to say (not a git repo, or hooks are
    reachable). Advisory -- never blocks.
    """
    git_dir = project_root / ".git"
    if not git_dir.exists():
        return None
    installed = [h for h in ("commit-msg", "post-commit") if (git_dir / "hooks" / h).is_file()]
    if not installed:
        return None
    try:
        proc = subprocess.run(
            ["git", "config", "--get", "core.hooksPath"],
            cwd=project_root, capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    configured = proc.stdout.strip()
    if not configured:
        return None  # git default — .git/hooks is used
    effective = (project_root / configured).resolve() if not Path(configured).is_absolute() \
        else Path(configured).resolve()
    if effective == (git_dir / "hooks").resolve():
        return None  # explicitly set to the default — still fires
    # Every PCP hook is unreachable once hooksPath points elsewhere -- git reads
    # only that directory. A same-NAMED file there does not rescue the hook, it
    # REPLACES it: PCP's commit-msg carries the `[pcp-bypass: reason]` capture and
    # the co-author-trailer strip, so an unrelated commit-msg running in its place
    # is worse than none at all, and silent either way. That distinction is called
    # out separately rather than being scored as "fine" -- an earlier version of
    # this check reported such hooks as reachable, which is exactly the
    # absence-vs-clean conflation this codebase keeps finding.
    return {
        "hooks_path": configured,
        "installed": installed,
        "unreachable": installed,
        "replaced_by_other_file": [h for h in installed if (effective / h).is_file()],
    }


def report_dead_git_hooks(project_root: Path) -> bool:
    """Print the hooks-unreachable warning if there is one. Returns whether it fired.

    Shared by `pcp doctor` and the automatic preflight so the two cannot drift --
    the whole point of this check is catching a silent gap, and having it live in
    only one of the two entry points would recreate one.
    """
    hooks = check_git_hooks_reachable(project_root)
    if not hooks:
        return False
    console.print(
        f"[yellow bold]⚠ PCP git hooks are installed but unreachable:[/yellow bold] "
        f"core.hooksPath = '{hooks['hooks_path']}', so git never reads .git/hooks/ — "
        f"{', '.join(hooks['unreachable'])} never fire. Layer 1's commit-msg gate and the "
        f"post-commit `pcp scan` are both silently inactive, so current_state.md ages "
        f"without warning."
    )
    if hooks["replaced_by_other_file"]:
        console.print(
            f"[yellow]   {', '.join(hooks['replaced_by_other_file'])}: a DIFFERENT file of "
            f"the same name runs from that directory instead. PCP's commit-msg carries the "
            f"\\[pcp-bypass: reason] capture and the co-author-trailer strip, so a substitute "
            f"is worse than none — it looks installed and enforces nothing.[/yellow]"
        )
    console.print(
        "[dim]   Fix: copy PCP's hooks into that directory (chaining any existing ones), "
        "or `git config --unset core.hooksPath` if the override is not deliberate. Note a "
        "GLOBAL core.hooksPath is inherited by every new repo, so `pcp init` in a fresh "
        "project starts with its hooks already shadowed.[/dim]"
    )
    return True


def check_environment(pcp_dir: Path, fatal_on_missing_required: bool = True) -> dict:
    """Non-interactive preflight — called automatically at the start of
    pcp build/watch/deploy. Reports, never prompts. Fatal only on git/claude."""
    tools = detect_tools()
    missing_required = [t for t in REQUIRED_TOOLS if not tools[t]["available"]]
    if missing_required:
        console.print(f"[red bold]Missing required tool(s): {', '.join(missing_required)}[/red bold]")
        console.print("[dim]Run `pcp doctor` for full environment setup.[/dim]")
        if fatal_on_missing_required:
            sys.exit(2)

    optional_missing = [
        k for k in ("test_runner", "lint", "sast", "coverage", "audit", "opa", "temporal")
        if not tools[k]["available"]
    ]
    if optional_missing:
        console.print(
            f"[dim]Preflight: no {', '.join(optional_missing)} tool detected — those QA steps will skip. "
            f"Run `pcp doctor` to review.[/dim]"
        )

    report_dead_git_hooks(pcp_dir.parent)

    bloat = check_schema_bloat()
    if bloat and bloat["bloated"]:
        console.print(
            f"[yellow bold]⚠ Postgres schema bloat:[/yellow bold] {bloat['count']} schemas matching "
            f"'{bloat['pattern']}' (threshold {bloat['threshold']}) — a known cause of pytest timeouts "
            f"that look like a stuck build. Run `pcp doctor --fix-schema-bloat` to clean up."
        )

    from pcp import build_loop_bypass, telemetry
    bypass_findings = build_loop_bypass.check(pcp_dir, pcp_dir.parent)
    telemetry.record(
        pcp_dir, cycle="qa", check="build-loop-bypass", control_id="CTRL-037",
        module=None, submodule=None, criterion_id=None,
        files=[], result="pass", errors=bypass_findings, error_count=len(bypass_findings),
    )
    for f in bypass_findings:
        console.print(f"[yellow bold]⚠ Build-loop bypass:[/yellow bold] {f}")
    return tools


@click.command()
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root (default: cwd, walks up to find .pcp/).")
@click.option("--check", "check_only", is_flag=True,
              help="Non-interactive: detect and report only, don't prompt or write.")
@click.option("--fix-schema-bloat", "fix_bloat", is_flag=True,
              help="Drop stray Postgres test schemas matching PCP_TEST_SCHEMA_PATTERN. Destructive — always confirms.")
@click.option("--yes", "yes", is_flag=True, help="Skip the confirmation prompt for --fix-schema-bloat (CI/non-interactive use).")
def doctor(project_path: str | None, check_only: bool, fix_bloat: bool, yes: bool):
    """Check/configure CLI integrations for this project's PCP lifecycle."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    project_root = pcp_dir.parent
    tools = detect_tools()

    bloat = check_schema_bloat()
    if fix_bloat:
        if not bloat:
            console.print("[dim]--fix-schema-bloat: no postgres connection or psql detected — nothing to do.[/dim]")
            sys.exit(0)
        if bloat["count"] == 0:
            console.print(f"[green]No schemas matching '{bloat['pattern']}' — nothing to drop.[/green]")
            sys.exit(0)
        console.print(f"[bold]{bloat['count']} schema(s)[/bold] matching '{bloat['pattern']}' will be permanently dropped.")
        if not yes and not click.confirm("Proceed?", default=False):
            console.print("[yellow]Aborted.[/yellow]")
            sys.exit(0)
        result = fix_schema_bloat(bloat["pattern"])
        console.print(f"[green]Dropped {len(result['dropped'])} schema(s).[/green]")
        if result["errors"]:
            console.print(f"[red]{len(result['errors'])} error(s):[/red]")
            for e in result["errors"][:10]:
                console.print(f"  {e}")
        sys.exit(1 if result["errors"] else 0)

    if bloat and bloat["bloated"]:
        console.print(
            f"[yellow bold]⚠ Postgres schema bloat:[/yellow bold] {bloat['count']} schemas matching "
            f"'{bloat['pattern']}' (threshold {bloat['threshold']}) — run `pcp doctor --fix-schema-bloat` to clean up.\n"
        )

    from pcp import build_loop_bypass
    for f in build_loop_bypass.check(pcp_dir, project_root):
        console.print(f"[yellow bold]⚠ Build-loop bypass:[/yellow bold] {f}\n")

    if report_dead_git_hooks(project_root):
        console.print("")

    table = Table(title="PCP Environment Check")
    table.add_column("Integration")
    table.add_column("Status")
    table.add_column("Detail")

    def _row(name: str, info: dict, key_label: str = "tool"):
        if info.get("available"):
            detail = info.get("path") or info.get(key_label) or ""
            table.add_row(name, "[green]available[/green]", str(detail))
        else:
            table.add_row(name, "[yellow]not found[/yellow]", "—")

    _row("git (required)", tools["git"])
    _row("claude CLI (required)", tools["claude"])
    _row("gh (CI status, for `pcp watch`)", tools["gh"])
    _row("Test runner", tools["test_runner"])
    _row("Lint", tools["lint"])
    _row("SAST/secret-scan", tools["sast"])
    _row("Coverage", tools["coverage"])
    _row("Dead-code audit", tools["audit"])
    _row("Slack notifications", tools["slack_notify"])
    _row("npx (a11y scan via axe-core, CTRL-022)", tools["npx"])
    _row("OPA (policy/decision layer)", tools["opa"])
    _row("Temporal CLI (process layer)", tools["temporal"])
    _row("agy (cross-vendor verifier, opt-in via PCP_VERIFIER_CROSS_VENDOR=1)", tools["agy"])
    console.print(table)
    console.print("[dim]Browser automation (for `pcp uat`): assumed available via this environment's MCP tools — not directly verified.[/dim]")

    context7 = detect_context7(project_root)
    c7_status = "[green]configured[/green]" if context7["configured"] else (
        "[yellow]npx available, not configured[/yellow]" if context7["npx_available"]
        else "[dim]npx not found[/dim]"
    )
    console.print(f"Context7 (live library docs for `pcp build`'s coding agent): {c7_status}")

    # Context-route staleness (CTRL-021): a route resolving to zero files
    # silently starves agents of context — flag it here where humans look.
    from pcp import context_map
    for finding in context_map.validate(pcp_dir):
        console.print(f"[yellow]⚠[/yellow] {finding}")

    # Rung-aware tooling recommendations (2026-07-17): projects declaring
    # rung-6 criteria should schema-validate LLM output with a real library
    # (Outlines = constrained decoding, strongest guarantee; Instructor =
    # typed-retry, most portable); rung 4/5 declarations get pointed at the
    # mature reuse-whole options by name — same shape as the Context7 offer.
    _rung_recs = _rung_tooling_recommendations(pcp_dir, project_root)
    for line in _rung_recs:
        console.print(f"[yellow]⚠[/yellow] {line}")

    # Agent-config surface audit (ECC AgentShield reference-pattern, scoped to
    # a deterministic scan): the config an agent executes FROM — settings
    # hooks, .mcp.json servers, instruction files — was the one surface no
    # PCP gate ever looked at, even though pcp doctor itself scaffolds
    # .mcp.json entries. Advisory, never blocks.
    from pcp.config_audit import audit_agent_config
    config_findings = audit_agent_config(project_root)
    if config_findings:
        console.print(f"\n[yellow bold]Agent-config audit: {len(config_findings)} finding(s)[/yellow bold]")
        for f in config_findings:
            console.print(f"  [yellow]⚠[/yellow] {f['file']} [{f['category']}] — {f['detail']}")
    else:
        console.print("[dim]Agent-config audit: no secrets or suspicious commands in "
                      ".claude/settings*.json, .mcp.json, CLAUDE.md/AGENTS.md/GEMINI.md.[/dim]")

    # Version drift (2026-07-27): PCP's own fixes reach frozen-wheel installs
    # only when a human remembers to roll them, and nothing enumerated those
    # installs. Surfaced four times in one session, including a served wheel
    # that kept distributing a known-vulnerable version for two days, and two
    # abandoned build worktrees whose own .venv sat at 0.8.6. Advisory: a
    # project may legitimately pin an older PCP.
    from pcp.version_drift import check as _version_drift_check
    drift = _version_drift_check()
    if drift["status"] == "behind":
        console.print(f"\n[yellow bold]⚠ PCP version drift:[/yellow bold] {drift['message']}")
        console.print(
            f"[dim]This install will not have fixes made since {drift['installed']}. "
            f"Reinstall from {drift.get('source_root')} to catch up.[/dim]"
        )
    elif drift["status"] == "code_drift":
        console.print(f"\n[yellow bold]⚠ PCP code drift:[/yellow bold] {drift['message']}")
    elif drift["status"] == "stale_metadata":
        console.print(f"[dim]PCP version string: {drift['message']}[/dim]")

    existing = load_integrations(pcp_dir)
    deploy = existing.get("deploy", {})

    if check_only:
        return

    console.print("\n[bold]Deploy configuration[/bold] (used by `pcp deploy` and `pcp watch`)")
    guessed = _guess_deploy_command(project_root)
    default_cmd = deploy.get("command") or guessed or ""
    deploy_command = click.prompt(
        "Deploy command (blank to skip)", default=default_cmd, show_default=bool(default_cmd),
    )
    health_url = click.prompt(
        "Post-deploy health-check URL (blank to skip)", default=deploy.get("health_check_url", ""), show_default=False,
    )
    rollback_command = click.prompt(
        "Rollback command (blank to skip)", default=deploy.get("rollback_command", ""), show_default=False,
    )

    if context7["npx_available"] and not context7["configured"]:
        if click.confirm(
            "\nEnable Context7 (live library docs injected into pcp build's coding-agent "
            "context, reduces hallucinated/outdated API usage)? Adds an entry to .mcp.json.",
            default=True,
        ):
            if configure_context7(project_root):
                console.print(f"[green]✓[/green] Context7 configured in {_mcp_config_path(project_root).relative_to(project_root)}")
                context7 = detect_context7(project_root)

    data = {
        "version": "1.0",
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tools": tools,
        "deploy": {
            "command": deploy_command or None,
            "health_check_url": health_url or None,
            "rollback_command": rollback_command or None,
        },
        "browser_automation": {"assumed_available": True},
        "context7": context7,
        "agent_config_audit": {"findings": len(config_findings)},
        "version_drift": drift,
    }
    out = pcp_dir / "integrations.yaml"
    out.write_text(yaml.dump(data, default_flow_style=False))
    console.print(f"\n[green]✓[/green] Saved → {out.relative_to(project_root)}")
