"""Narrative-lint — flags CLAUDE.md-family narrative prose that has drifted
from PCP's own tracked state (current_state.md/architecture.md).

Fleet evidence (2026-07-24 context-hygiene pass across Event-Manager/
win2mac/agentberg/atacamaMDM): narrative prose in CLAUDE.md — stage
descriptions, "Open Decisions," "Pending" lists — drifted from tracked
state 3-for-3 in projects checked, because nothing checks free-text
against it. `~/.claude/scripts/session-hygiene-check.sh` already covers
the two purely mechanical checks (stale dates, missing referenced files)
as a SessionStart hook; this ports those into PCP's own enforcement
lifecycle (telemetry, CTRL-036) and adds the piece a bash regex can't do
— semantic contradiction between a narrative status claim and current_
state.md/architecture.md, which needs a judge call (rung 6, same posture
as CTRL-020's rung-necessity check: one batched call, advisory, fail-open).
"""

import re
from datetime import datetime, timezone
from pathlib import Path

STALE_DAYS_DEFAULT = 90

_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_BACKTICK_PATH_RE = re.compile(r"`([^`]*(?:/[^`]+)+)`")
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pcp"}

_STATUS_KEYWORDS = [
    "pending", "open decision", "planned", "pre-build", "not yet built",
    "not yet implemented", "coming soon", "todo:", "in progress", "wip",
    "not started", "deferred",
]

NARRATIVE_CONTRADICTION_SYSTEM_PROMPT = """You are a documentation-drift auditor. \
You are given status-shaped lines pulled from a project's CLAUDE.md-family files \
(narrative context docs) alongside excerpts of that project's auto-generated \
ground-truth state (current_state.md / architecture.md). Flag any narrative line \
that is CONTRADICTED by the tracked state — e.g. a line claims something is \
"Pending"/"Planned"/"Pre-build" but the tracked state shows it already built or \
decided; a tech-stack claim contradicted by the actual stack; an "Open Decision" \
already answered. Do not flag lines that are merely unrelated to the tracked \
state, or that are still accurate. Respond with JSON: \
{"contradictions": [{"index": <int, index of the narrative line>, \
"reason": "<short explanation citing the tracked-state evidence>"}]}. \
Empty list if none found."""


def find_claude_md_files(project_root: Path) -> list[Path]:
    """CLAUDE.md-family files: any CLAUDE.md/CLAUDE.local.md (path-scoped or
    root) plus .claude/*.md — same scope session-hygiene-check.sh already
    covers, ported to Python so PCP's own enforcement lifecycle can wire it
    in instead of only ever reaching a human via a SessionStart hook print."""
    found = []
    for p in project_root.rglob("*"):
        if p.is_dir() or any(seg in _SKIP_DIRS for seg in p.parts):
            continue
        if p.name in ("CLAUDE.md", "CLAUDE.local.md"):
            found.append(p)
    claude_dir = project_root / ".claude"
    if claude_dir.is_dir():
        found += [p for p in claude_dir.glob("*.md") if p.is_file()]
    return sorted(set(found))


def check_stale_dates(files: list[Path], stale_days: int = STALE_DAYS_DEFAULT) -> list[str]:
    """Deterministic — a dated reference (e.g. "RESOLVED 2026-01-01") older
    than stale_days nobody cleaned up."""
    findings = []
    now = datetime.now(timezone.utc)
    for f in files:
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for m in _DATE_RE.finditer(line):
                try:
                    d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
                except ValueError:
                    continue
                age_days = (now - d).days
                if age_days > stale_days:
                    findings.append(
                        f"{f}:{lineno}: date {m.group(0)} is {age_days}d old (>{stale_days}d threshold): "
                        f"{line.strip()[:140]}"
                    )
    return findings


def _looks_like_non_path(p: str) -> bool:
    if any(c in p for c in (" ", ":", "<", ">", "*")):
        return True
    if "://" in p:
        return True
    if any(seg in p for seg in (".com/", ".org/", ".io/", ".net/", ".dev/")) or p.endswith(".git"):
        return True
    if p.startswith("/") and "/" not in p[1:] and "." not in p:
        return True  # bare slash-command like /pcp, not a filesystem path
    return False


def check_missing_files(files: list[Path], project_root: Path) -> list[str]:
    """Deterministic — a backtick-quoted path referenced in a CLAUDE.md-family
    file that no longer exists on disk."""
    findings = []
    for f in files:
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for m in _BACKTICK_PATH_RE.finditer(line):
                p = m.group(1)
                if _looks_like_non_path(p) or "*" in p:
                    continue
                expanded = Path(p).expanduser()
                if not expanded.is_absolute():
                    expanded = project_root / expanded
                if not expanded.exists():
                    findings.append(f"{f}:{lineno}: referenced path not found on disk: `{p}`")
    return findings


def collect_status_lines(files: list[Path]) -> list[tuple[str, int, str]]:
    """Deterministic pre-filter — only status-shaped lines get sent to the
    judge call (Token Discipline: never paste whole CLAUDE.md files)."""
    hits = []
    for f in files:
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            low = line.lower()
            if any(k in low for k in _STATUS_KEYWORDS):
                hits.append((str(f), lineno, line.strip()))
    return hits


def _tracked_state_excerpt(pcp_dir: Path, max_chars: int) -> str:
    parts = []
    for name in ("current_state.md", "architecture.md"):
        p = pcp_dir / name
        if p.exists():
            parts.append(f"### {name}\n{p.read_text(errors='replace')}")
    excerpt = "\n\n".join(parts)
    return excerpt[:max_chars]


def check_narrative_contradictions(
    pcp_dir: Path, status_lines: list[tuple[str, int, str]],
    max_state_chars: int = 8000,
) -> list[str]:
    """ONE batched judge call (rung 6 — semantic contradiction is the one
    irreducibly judgment-shaped part of this lint, same posture CTRL-020's
    rung-necessity check already uses). Advisory; fails open on any error
    or if there's nothing tracked to compare against."""
    if not status_lines:
        return []
    state_excerpt = _tracked_state_excerpt(pcp_dir, max_state_chars)
    if not state_excerpt.strip():
        return []

    from pcp.llm import client as llm

    numbered = "\n".join(f"[{i}] {path}:{lineno}: {text}" for i, (path, lineno, text) in enumerate(status_lines))
    user_prompt = f"Narrative lines:\n{numbered}\n\nTracked state:\n{state_excerpt}"
    findings: list[str] = []
    try:
        res = llm.call_json(
            NARRATIVE_CONTRADICTION_SYSTEM_PROMPT, user_prompt, model=llm.JUDGE_MODEL,
            pcp_dir=pcp_dir, command="narrative-lint",
        )
        for c in res.get("contradictions", []):
            if not isinstance(c, dict):
                continue
            i = c.get("index")
            if isinstance(i, int) and 0 <= i < len(status_lines):
                path, lineno, text = status_lines[i]
                findings.append(
                    f"{path}:{lineno}: narrative claim contradicted by tracked state — "
                    f"{c.get('reason', '')[:200]} (claim: {text[:140]!r})"
                )
    except Exception:
        pass  # advisory judge call — fail open, same posture as every other wave-merge judge check
    return findings


def run(pcp_dir: Path, stale_days: int = STALE_DAYS_DEFAULT, skip_llm: bool = False) -> dict:
    """Full lint: deterministic checks always run; the semantic contradiction
    check is skippable (CI/cost-sensitive callers) via skip_llm."""
    project_root = pcp_dir.parent
    files = find_claude_md_files(project_root)
    stale = check_stale_dates(files, stale_days)
    missing = check_missing_files(files, project_root)
    contradictions: list[str] = []
    if not skip_llm:
        status_lines = collect_status_lines(files)
        contradictions = check_narrative_contradictions(pcp_dir, status_lines)
    return {
        "files_scanned": [str(f) for f in files],
        "stale_dates": stale,
        "missing_files": missing,
        "contradictions": contradictions,
    }


def render_markdown(result: dict, timestamp: str) -> str:
    lines = [
        "# Narrative Lint",
        f"Generated: {timestamp}",
        "",
        f"CLAUDE.md-family files scanned: {len(result['files_scanned'])}",
        "",
    ]
    sections = [
        ("Stale dated references", result["stale_dates"]),
        ("Missing referenced files", result["missing_files"]),
        ("Narrative vs. tracked-state contradictions (advisory judge call)", result["contradictions"]),
    ]
    for title, findings in sections:
        lines.append(f"## {title}")
        lines.append("")
        if findings:
            for f in findings:
                lines.append(f"- {f}")
        else:
            lines.append("_None found._")
        lines.append("")
    return "\n".join(lines)
