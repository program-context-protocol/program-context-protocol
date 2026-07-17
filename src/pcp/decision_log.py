"""Technical decision log — distilled from session/build-loop conversation.

Distinct from telemetry.jsonl (build-cycle QA/cost events) — this is the
*technical input* half of conversational drift capture (see capture.py for
the classifier and its business-logic counterpart, brd_items.yaml). Append-only
JSONL, one record per distilled technical decision/rationale. Auto-appended by
`pcp capture`. Never edit.
"""

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from pcp.evidence_chain import chain_entry


def record(pcp_dir: Path, **fields) -> None:
    """Append one JSONL record to .pcp/decision_log.jsonl.

    Suggested fields: source ("session:<id>"|"build:<module>:<criterion_id>"),
    session_id, category (freeform, e.g. "library-choice"|"architecture"|"workaround"),
    summary, evidence (quoted excerpt), module, criterion_id.
    """
    fields = {"timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **fields}
    path = Path(pcp_dir) / "decision_log.jsonl"
    entry = chain_entry(_last_entry_hash(path), fields)
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _last_entry_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    last_line = None
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            last_line = line
    if not last_line:
        return None
    try:
        return json.loads(last_line).get("entry_hash")
    except json.JSONDecodeError:
        return None


def load(pcp_dir: Path) -> list[dict]:
    path = Path(pcp_dir) / "decision_log.jsonl"
    if not path.exists():
        return []
    records = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def select_relevant(pcp_dir: Path, module: str, limit: int | None = None,
                    max_chars: int | None = None) -> list[dict]:
    """Deterministic selection of decisions worth injecting into a build
    agent's prompt (no LLM — rung 1). This closes the loop this file's own
    docstring promised ("intended to feed future build-loop context") but
    that never got built: records went in, nothing ever came out, so
    criterion agent #14 re-discovered what agent #3 already learned.

    Priority: this module's own decisions first (module field or
    build:<module>: source), newest first; then module-less project-wide
    decisions, newest first. Other modules' decisions are never injected —
    cross-module context is what architecture.md is for.

    Bounded twice (count and chars) — prompt injection must never become
    its own token-discipline violation. PCP_BUILD_MAX_DECISIONS (default 6),
    PCP_BUILD_DECISIONS_MAX_CHARS (default 2500)."""
    import os
    limit = limit if limit is not None else int(os.environ.get("PCP_BUILD_MAX_DECISIONS", "6"))
    max_chars = max_chars if max_chars is not None else int(os.environ.get("PCP_BUILD_DECISIONS_MAX_CHARS", "2500"))
    if limit <= 0:
        return []

    records = load(pcp_dir)
    module_prefix = f"build:{module}:"

    def _is_module_match(r: dict) -> bool:
        return r.get("module") == module or str(r.get("source", "")).startswith(module_prefix)

    def _ts(r: dict) -> str:
        return r.get("timestamp", "")

    matched = sorted([r for r in records if _is_module_match(r) and r.get("summary")], key=_ts, reverse=True)
    global_ = sorted(
        [r for r in records if not _is_module_match(r) and not r.get("module") and r.get("summary")],
        key=_ts, reverse=True,
    )

    selected, used_chars = [], 0
    for r in matched + global_:
        if len(selected) >= limit:
            break
        line_len = len(_render_line(r))
        if used_chars + line_len > max_chars:
            continue
        selected.append(r)
        used_chars += line_len
    return selected


def _render_line(r: dict) -> str:
    category = r.get("category") or "decision"
    summary = str(r.get("summary", "")).strip()
    return f"- [{category}] {summary}"


def format_for_prompt(pcp_dir: Path, module: str) -> list[str]:
    """Rendered lines for _build_agent_prompt. Empty list when there's nothing
    to inject (or PCP_BUILD_INJECT_DECISIONS=0) — caller skips the section."""
    import os
    if os.environ.get("PCP_BUILD_INJECT_DECISIONS", "1") in ("0", "false", "no"):
        return []
    return [_render_line(r) for r in select_relevant(pcp_dir, module)]


def aggregate(records: list[dict]) -> dict:
    """Roll up decisions per category. Shared by `pcp telemetry`-style reporting
    and the pcp.md 'Technical Decisions' section."""
    by_category = defaultdict(list)
    for r in records:
        by_category[r.get("category") or "uncategorized"].append(r)
    return {"by_category": by_category, "records": records}
