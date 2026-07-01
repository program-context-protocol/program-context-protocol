"""Conversational drift capture — classifies a session transcript into business-logic
drift (-> .pcp/brd_items.yaml / .pcp/brd.md, a living BRD distinct from the immutable
objective.md) and technical input (-> .pcp/decision_log.jsonl).

Closes a real gap in the PCP lifecycle: requirement/scope changes surfaced in
PM/UAT conversation, or technical rationale stated mid-build, were never
captured anywhere in .pcp/ — only an explicit `pcp pm "<intent>"` call updated
specs. This module is advisory and additive: it never touches objective.md or
any spec.yaml/acceptance.yaml directly. Drift against the objective is flagged
in brd.md for a human to act on via `pcp pm`, not auto-applied.

Two trigger points call into run_capture(): the `pcp capture` CLI command
(wired to a Claude Code SessionEnd hook, for human/PM/UAT sessions) and
`pcp build`'s per-criterion loop (for autonomous coding-agent sessions).
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

from pcp import decision_log
from pcp.llm import client as llm

MAX_TRANSCRIPT_CHARS = 40000

SYSTEM_PROMPT = """\
You are analyzing a software development session transcript to classify what was \
discussed into two categories, so nothing gets lost between sessions.

1. business_items — requirement, scope, priority, or product-behavior changes/clarifications \
(the kind of thing that should update a Business Requirements Document). Only include \
substantive drift or elaboration, not routine implementation chatter.
2. technical_items — implementation choices, tool/library picks, architecture tradeoffs, \
workarounds, or root causes discussed (useful context for future coding sessions).

You are given the program's existing immutable objective, and the currently active \
business requirements already on file (so you can mark supersession instead of duplicating).

For each business item, if it appears to conflict with the given objective text, set \
drift_flag to a short explanation of the conflict; otherwise null. If it clearly replaces \
an existing active requirement (by id), set supersedes to that id; otherwise null.

Skip anything that's just routine back-and-forth with no durable decision (e.g. "run the \
tests", "looks good", debugging noise). If nothing substantive was discussed, return empty lists.

You must output ONLY valid JSON — no prose, no markdown, no code fences.

Output schema:
{
  "business_items": [
    {"description": "...", "evidence": "short quoted excerpt", "supersedes": "BRD-001 or null", "drift_flag": "explanation or null"}
  ],
  "technical_items": [
    {"category": "library-choice|architecture|workaround|root-cause|other", "summary": "...", "evidence": "short quoted excerpt"}
  ]
}
"""


def extract_conversation_text(transcript_path: Path) -> str:
    """Read a Claude Code session transcript JSONL, keep only human + assistant
    text turns (drop tool_use/tool_result blocks), return as plain text, capped
    to MAX_TRANSCRIPT_CHARS (keeps the most recent content) for token discipline."""
    lines_out = []
    for line in Path(transcript_path).read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        content = message.get("content")
        texts = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                    texts.append(block["text"])
        text = "\n".join(t for t in texts if t.strip())
        if text.strip():
            lines_out.append(f"{role.capitalize()}: {text.strip()}")
    full = "\n\n".join(lines_out)
    return full[-MAX_TRANSCRIPT_CHARS:] if len(full) > MAX_TRANSCRIPT_CHARS else full


def _load_brd_items(pcp_dir: Path) -> list[dict]:
    path = pcp_dir / "brd_items.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    return data.get("items", [])


def classify_transcript(pcp_dir: Path, conversation_text: str, source: str) -> dict:
    """Single Haiku call classifying conversation_text. Returns
    {"business_items": [...], "technical_items": [...]}."""
    objective = (pcp_dir / "objective.md").read_text() if (pcp_dir / "objective.md").exists() else ""
    active_items = [i for i in _load_brd_items(pcp_dir) if i.get("status") == "active"]
    active_summary = "\n".join(f"- [{i['id']}] {i['description']}" for i in active_items) or "(none yet)"

    user_prompt = "\n\n".join([
        f"## Source\n{source}",
        f"## Program Objective (immutable)\n{objective}",
        f"## Currently Active Business Requirements\n{active_summary}",
        f"## Conversation Transcript\n{conversation_text}",
    ])
    return llm.call_json(SYSTEM_PROMPT, user_prompt, model=llm.JUDGE_MODEL, pcp_dir=pcp_dir, command="capture-classify")


def _write_brd_md(pcp_dir: Path, items: list[dict], timestamp: str) -> Path:
    active = [i for i in items if i.get("status") == "active"]
    drift = [i for i in active if i.get("drift_flag")]
    superseded = [i for i in items if i.get("status") == "superseded"]

    lines = [
        "# Business Requirements (Living)",
        f"Generated: {timestamp}",
        "",
        "> Auto-generated by `pcp capture`. Distills PM/UAT conversation drift. Do not edit manually.",
        "> See objective.md for the immutable program objective — conflicts here mean objective.md "
        "may need a deliberate update (via `pcp pm`), not that this file is wrong.",
        "",
    ]

    if drift:
        lines += ["## Drift Flags", ""]
        for i in drift:
            lines.append(f"- **{i['id']}**: {i['description']} — _{i['drift_flag']}_")
        lines.append("")

    lines += ["## Active Requirements", ""]
    if active:
        for i in active:
            lines.append(f"- **{i['id']}** (`{i.get('source', '?')}`): {i['description']}")
    else:
        lines.append("_None captured yet._")
    lines.append("")

    if superseded:
        lines += ["## Superseded", ""]
        for i in superseded:
            lines.append(f"- **{i['id']}** → superseded by {i.get('superseded_by')}: {i['description']}")
        lines.append("")

    out = pcp_dir / "brd.md"
    out.write_text("\n".join(lines))
    return out


def _next_brd_id(items: list[dict]) -> str:
    nums = []
    for i in items:
        try:
            nums.append(int(str(i["id"]).split("-")[-1]))
        except (KeyError, ValueError):
            continue
    return f"BRD-{max(nums, default=0) + 1:03d}"


def apply_business_items(pcp_dir: Path, items: list[dict], source: str) -> Path:
    """Merge distilled business items into brd_items.yaml, mark supersessions,
    regenerate brd.md. Returns the brd.md path."""
    existing = _load_brd_items(pcp_dir)
    by_id = {i["id"]: i for i in existing}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for item in items:
        supersedes = item.get("supersedes")
        new_id = _next_brd_id(existing)
        if supersedes and supersedes in by_id:
            by_id[supersedes]["status"] = "superseded"
            by_id[supersedes]["superseded_by"] = new_id
            by_id[supersedes]["last_updated"] = now
        new_entry = {
            "id": new_id,
            "description": item["description"],
            "status": "active",
            "superseded_by": None,
            "first_seen": now,
            "last_updated": now,
            "source": source,
            "drift_flag": item.get("drift_flag"),
        }
        existing.append(new_entry)
        by_id[new_id] = new_entry

    (pcp_dir / "brd_items.yaml").write_text(yaml.dump({"items": existing}, default_flow_style=False))
    return _write_brd_md(pcp_dir, existing, now)


def apply_technical_items(pcp_dir: Path, items: list[dict], source: str, session_id: str | None) -> None:
    for item in items:
        decision_log.record(
            pcp_dir,
            source=source, session_id=session_id,
            category=item.get("category", "other"),
            summary=item.get("summary", ""),
            evidence=item.get("evidence", ""),
        )


def find_transcript_for_session(session_id: str) -> Path | None:
    """Locate a Claude Code CLI session's own transcript file — same location
    `claude -p --resume <session_id>` reads from."""
    if not session_id:
        return None
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.is_dir():
        return None
    matches = list(projects_dir.glob(f"*/{session_id}.jsonl"))
    return matches[0] if matches else None


def run_capture(pcp_dir: Path, transcript_path: Path, source: str, session_id: str | None = None) -> dict:
    """Orchestrates classify + apply. Never raises — advisory, same posture as
    `pcp audit`. Returns a summary dict."""
    try:
        conversation_text = extract_conversation_text(transcript_path)
        if not conversation_text.strip():
            return {"skipped": "empty transcript"}

        result = classify_transcript(pcp_dir, conversation_text, source)
        business_items = result.get("business_items", []) or []
        technical_items = result.get("technical_items", []) or []

        if business_items:
            apply_business_items(pcp_dir, business_items, source)
        if technical_items:
            apply_technical_items(pcp_dir, technical_items, source, session_id)

        return {"business_count": len(business_items), "technical_count": len(technical_items)}
    except Exception as e:
        return {"skipped": f"capture failed: {e}"}
