"""Librarian -- deterministic, rung-4-shaped retrieval so a criterion's
builder doesn't independently re-explore the codebase for a pattern another
module already has. Per CLAUDE.md's own Logic-Tier ladder, this is scoped as
retrieval (keyword-overlap against existing top-level definitions), not a
conversational search agent -- a full semantic-search build wasn't earned
without first proving grep-level retrieval is insufficient.

Pure query/response, no correction: it never blocks or judges, only surfaces
possibly-related existing code for the agent to check before reusing.
Injected into the build prompt bounded by count and chars, same posture
decision_log.format_for_prompt already established.
"""

import re
from pathlib import Path

_SKIP_DIRS = {"__pycache__", ".venv", "venv", "node_modules", ".git", ".pcp", "dist", "build"}
_SOURCE_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rb", ".java"}

_DEF_PATTERN = re.compile(
    r"^\s*(?:def|class|function|const|export\s+function|export\s+default\s+function|"
    r"export\s+const|export\s+class)\s+([A-Za-z_][A-Za-z0-9_]*)",
)


def _keywords(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Za-z]{5,}", text or "")}


def find_related_definitions(project_root: Path, criterion: dict, max_results: int = 6) -> list[str]:
    """Keyword-overlap scan over existing function/class/const definitions
    across the project's own source files. Returns up to max_results
    "path:line: name" hints, ranked by keyword-hit count, highest first.
    Deterministic, zero LLM cost -- a cheap complementary signal, not
    semantic search."""
    keywords = _keywords(criterion.get("description", "")) | _keywords(criterion.get("id", ""))
    if not keywords or not project_root.is_dir():
        return []

    hits: list[tuple[int, str]] = []
    for path in project_root.rglob("*"):
        if not path.is_file() or path.suffix not in _SOURCE_EXTS:
            continue
        if any(seg in _SKIP_DIRS for seg in path.parts):
            continue
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, start=1):
            m = _DEF_PATTERN.match(line)
            if not m:
                continue
            name = m.group(1)
            name_lower = name.lower()
            name_words = _keywords(name.replace("_", " "))
            score = len(keywords & name_words) + sum(1 for k in keywords if k in name_lower)
            if score > 0:
                rel = path.relative_to(project_root)
                hits.append((score, f"{rel}:{lineno}: {name}"))

    hits.sort(key=lambda t: -t[0])
    seen: set[str] = set()
    out: list[str] = []
    for _score, hint in hits:
        if hint in seen:
            continue
        seen.add(hint)
        out.append(hint)
        if len(out) >= max_results:
            break
    return out


def format_for_prompt(project_root: Path, criterion: dict, max_results: int = 6, max_chars: int = 1200) -> list[str]:
    """Bounded-count, bounded-char rendering for direct inclusion in the
    build prompt -- same Token Discipline posture as decision_log's own
    format_for_prompt."""
    hints = find_related_definitions(project_root, criterion, max_results=max_results)
    if not hints:
        return []
    lines: list[str] = []
    budget = max_chars
    for h in hints:
        if budget - len(h) < 0:
            break
        lines.append(f"- {h}")
        budget -= len(h)
    return lines
