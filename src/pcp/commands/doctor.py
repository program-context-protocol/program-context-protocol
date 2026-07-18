"""pcp doctor — environment preflight. Checks which CLI integrations are
available, writes .pcp/integrations.yaml. Interactive mode asks for the ones
that need human-provided config (deploy command, health-check URL).

pcp build/watch/deploy call check_environment() at the start of their run in
non-interactive report-only mode — never blocks on missing optional tooling,
only on git/claude (required for the lifecycle to function at all).
"""

import json
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


def detect_tools() -> dict:
    """Pure detection, no prompts. Used by both `pcp doctor` and the
    non-interactive preflight other commands run automatically."""
    return {
        "git": {"available": _which("git") is not None, "path": _which("git")},
        "claude": {"available": _which("claude") is not None, "path": _which("claude")},
        "gh": {"available": _which("gh") is not None, "path": _which("gh")},
        "test_runner": _detect_one(["pytest", "npm", "go"]),
        "lint": _detect_one(["ruff", "eslint"]),
        "sast": _detect_one(["semgrep"]),
        "coverage": _detect_one(["coverage"]),
        "audit": _detect_one(["vulture", "knip"]),
        "slack_notify": {"available": _which("slack-notify") is not None, "path": _which("slack-notify")},
        "opa": _detect_one(["opa"]),
        "temporal": _detect_one(["temporal"]),
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


def load_integrations(pcp_dir: Path) -> dict:
    path = pcp_dir / "integrations.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


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
    return tools


@click.command()
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root (default: cwd, walks up to find .pcp/).")
@click.option("--check", "check_only", is_flag=True,
              help="Non-interactive: detect and report only, don't prompt or write.")
def doctor(project_path: str | None, check_only: bool):
    """Check/configure CLI integrations for this project's PCP lifecycle."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    project_root = pcp_dir.parent
    tools = detect_tools()

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
    _row("OPA (policy/decision layer)", tools["opa"])
    _row("Temporal CLI (process layer)", tools["temporal"])
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
    }
    out = pcp_dir / "integrations.yaml"
    out.write_text(yaml.dump(data, default_flow_style=False))
    console.print(f"\n[green]✓[/green] Saved → {out.relative_to(project_root)}")
