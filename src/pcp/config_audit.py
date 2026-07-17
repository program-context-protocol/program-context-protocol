"""Agent-config surface audit — deterministic, no LLM, no external tool.

PCP's QA gates scan the *built code* (SAST/secret-scan via semgrep), but
nothing audited the project's *agent-facing configuration* — the surface an
agent actually executes from: `.claude/settings.json` hooks (arbitrary shell
commands), `.mcp.json` servers (which `pcp doctor` itself now scaffolds for
Context7), and instruction files (CLAUDE.md/AGENTS.md). Reference-pattern
from ECC's AgentShield (affaan-m/ECC), scoped down to a deterministic scan:
secret literals, and hook/server commands that fetch-and-execute remote code.

Advisory only — surfaced by `pcp doctor`, never blocks. A finding here is a
signal for human review, same posture as `pcp audit`'s dead-code findings.
"""

import json
import re
from pathlib import Path

# Anchored to real token shapes, not bare prefixes — `sk-` alone would flag
# prose. Patterns matched against raw file text.
SECRET_PATTERNS: list[tuple[str, str]] = [
    ("Anthropic API key", r"sk-ant-[A-Za-z0-9_-]{20,}"),
    ("OpenAI-style API key", r"sk-[A-Za-z0-9]{20,}"),
    ("GitHub token", r"gh[pousr]_[A-Za-z0-9]{36,}"),
    ("GitHub fine-grained PAT", r"github_pat_[A-Za-z0-9_]{22,}"),
    ("AWS access key", r"AKIA[0-9A-Z]{16}"),
    ("Slack token", r"xox[bpoas]-[0-9A-Za-z-]{10,}"),
    ("Google API key", r"AIza[0-9A-Za-z_-]{35}"),
    ("Private key block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]

SUSPICIOUS_COMMAND_PATTERNS: list[tuple[str, str]] = [
    ("remote script piped to shell", r"(curl|wget)[^|;&\n]*\|\s*(ba|z)?sh"),
    ("base64-decoded payload piped to shell", r"base64\s+(-d|--decode)[^|\n]*\|\s*(ba|z)?sh"),
    ("recursive delete from root/home", r"rm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\s+[/~]"),
]

INSTRUCTION_FILES = ("CLAUDE.md", "AGENTS.md", "GEMINI.md")
SETTINGS_FILES = (".claude/settings.json", ".claude/settings.local.json")


def _finding(file: str, category: str, detail: str) -> dict:
    return {"file": file, "category": category, "detail": detail}


def _scan_text_for_secrets(text: str, rel_path: str) -> list[dict]:
    findings = []
    for name, pattern in SECRET_PATTERNS:
        for m in re.finditer(pattern, text):
            token = m.group(0)
            findings.append(_finding(
                rel_path, "secret",
                f"{name} literal ({token[:8]}…{token[-4:]}) — move to an env var/secret store",
            ))
    return findings


def _scan_text_for_suspicious_commands(text: str, rel_path: str, context: str) -> list[dict]:
    findings = []
    for name, pattern in SUSPICIOUS_COMMAND_PATTERNS:
        if re.search(pattern, text):
            findings.append(_finding(
                rel_path, "suspicious-command",
                f"{name} in {context} — an agent session executes this without review",
            ))
    return findings


def _iter_hook_commands(settings: dict):
    """Yield every command string from Claude Code's hooks structure:
    {"hooks": {"<Event>": [{"hooks": [{"type": "command", "command": "..."}]}]}}"""
    for event_entries in (settings.get("hooks") or {}).values():
        if not isinstance(event_entries, list):
            continue
        for entry in event_entries:
            for h in (entry.get("hooks") or []) if isinstance(entry, dict) else []:
                if isinstance(h, dict) and h.get("command"):
                    yield str(h["command"])


def _audit_settings_file(project_root: Path, rel_path: str) -> list[dict]:
    path = project_root / rel_path
    if not path.exists():
        return []
    text = path.read_text(errors="replace")
    findings = _scan_text_for_secrets(text, rel_path)
    try:
        settings = json.loads(text)
    except json.JSONDecodeError:
        findings.append(_finding(rel_path, "unparseable", "not valid JSON — hooks could not be audited"))
        return findings
    for cmd in _iter_hook_commands(settings):
        findings.extend(_scan_text_for_suspicious_commands(cmd, rel_path, "a hook command"))
    return findings


def _audit_mcp_config(project_root: Path) -> list[dict]:
    path = project_root / ".mcp.json"
    if not path.exists():
        return []
    text = path.read_text(errors="replace")
    findings = _scan_text_for_secrets(text, ".mcp.json")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        findings.append(_finding(".mcp.json", "unparseable", "not valid JSON — servers could not be audited"))
        return findings
    for server_name, server in (data.get("mcpServers") or {}).items():
        if not isinstance(server, dict):
            continue
        blob = " ".join([str(server.get("command", ""))] + [str(a) for a in server.get("args", [])])
        findings.extend(_scan_text_for_suspicious_commands(
            blob, ".mcp.json", f"MCP server '{server_name}' launch command"))
        for env_key, env_val in (server.get("env") or {}).items():
            for name, pattern in SECRET_PATTERNS:
                if re.fullmatch(pattern, str(env_val)):
                    findings.append(_finding(
                        ".mcp.json", "secret",
                        f"MCP server '{server_name}' env {env_key} holds an inline {name} — "
                        "reference an environment variable instead",
                    ))
    return findings


def audit_agent_config(project_root: Path) -> list[dict]:
    """Full sweep. Returns findings, deduplicated, stable order."""
    findings: list[dict] = []
    for rel in SETTINGS_FILES:
        findings.extend(_audit_settings_file(project_root, rel))
    findings.extend(_audit_mcp_config(project_root))
    for rel in INSTRUCTION_FILES:
        path = project_root / rel
        if path.exists():
            findings.extend(_scan_text_for_secrets(path.read_text(errors="replace"), rel))
    seen, unique = set(), []
    for f in findings:
        key = (f["file"], f["category"], f["detail"])
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique
