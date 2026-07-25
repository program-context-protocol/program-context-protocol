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

import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

from pcp import decision_log
from pcp import objective_conflicts
from pcp import telemetry
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
    # Same predicate the build gate uses — a resolved or dismissed conflict is
    # not an open one, and must stop being reported as such (see
    # objective_conflicts.is_unresolved_conflict for the divergence this fixes).
    drift = [i for i in active if objective_conflicts.is_unresolved_conflict(i)]
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


def _normalize_desc(s: str) -> str:
    return " ".join(s.split()).strip().lower()


def apply_business_items(pcp_dir: Path, items: list[dict], source: str) -> Path:
    """Merge distilled business items into brd_items.yaml, mark supersessions,
    regenerate brd.md. Returns the brd.md path.

    Dedupes against already-active items by normalized description text. Fixes
    a confirmed bug: the classifier can (and did, in one real run) emit the same
    substantive point twice — once per mention across a long transcript — with
    byte-identical description text; nothing previously stopped both copies
    from becoming separate BRD-NNN entries."""
    existing = _load_brd_items(pcp_dir)
    by_id = {i["id"]: i for i in existing}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    seen_active = {_normalize_desc(i["description"]) for i in existing if i.get("status") == "active"}

    for item in items:
        desc_key = _normalize_desc(item["description"])
        if desc_key in seen_active:
            continue
        supersedes = item.get("supersedes")
        new_id = _next_brd_id(existing)
        if supersedes and supersedes in by_id:
            by_id[supersedes]["status"] = "superseded"
            by_id[supersedes]["superseded_by"] = new_id
            by_id[supersedes]["last_updated"] = now
        drift_flag = item.get("drift_flag")
        new_entry = {
            "id": new_id,
            "description": item["description"],
            "status": "active",
            "superseded_by": None,
            "first_seen": now,
            "last_updated": now,
            "source": source,
            "drift_flag": drift_flag,
            # Stamped only when flagged -- this is the "proof of edit" hash
            # `pcp build`'s objective-conflict gate compares against
            # objective.md/target_state.md's current content. See
            # objective_conflicts.py for why this exists.
            "objective_hash_at_flag": objective_conflicts.objective_hash(pcp_dir) if drift_flag else None,
        }
        existing.append(new_entry)
        by_id[new_id] = new_entry
        seen_active.add(desc_key)

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


def archive_transcript(pcp_dir: Path, transcript_path: Path | None, session_id: str | None) -> str | None:
    """Copies the raw session transcript (gzip-compressed) into
    .pcp/transcripts/ — so the full action-level record (every tool call,
    every edit, not just the classified business/technical summary
    classify_transcript() extracts) durably belongs to the PROJECT, not just
    to Claude Code's own ~/.claude/projects/ retention, which PCP doesn't
    control and which could be pruned independently of this project's life.

    Returns the relative path (under pcp_dir), or None if there was nothing
    to archive. Honest tradeoff, not silently ignored: long-running sessions
    produce multi-MB transcripts, so this does grow .pcp/ over time — no
    retention/pruning policy exists yet, same as telemetry.jsonl/evidence/
    growing unbounded. Compression (typically 5-10x on JSONL) is the only
    mitigation applied so far."""
    if not transcript_path or not Path(transcript_path).exists():
        return None
    out_dir = Path(pcp_dir) / "transcripts"
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"{session_id or Path(transcript_path).stem}.jsonl.gz"
    out_path = out_dir / name
    with open(transcript_path, "rb") as src, gzip.open(out_path, "wb") as dst:
        dst.write(src.read())
    return str(out_path.relative_to(pcp_dir))


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
    """Orchestrates archive + classify + apply. Never raises — advisory, same
    posture as `pcp audit`. Returns a summary dict.

    Archival happens unconditionally, even for an empty/routine-chatter
    transcript — the raw record is evidence regardless of whether the
    classifier found anything substantive enough to distill into BRD/
    decision-log entries."""
    archived_path = None
    try:
        archived_path = archive_transcript(pcp_dir, transcript_path, session_id)
        if archived_path:
            telemetry.record(
                pcp_dir, cycle="capture", check="transcript-archive", control_id="CTRL-011",
                session_id=session_id, evidence_path=archived_path, result="pass",
            )
    except Exception as e:
        telemetry.record(
            pcp_dir, cycle="capture", check="transcript-archive", control_id="CTRL-011",
            session_id=session_id, result="error", errors=[str(e)], error_count=1,
        )

    try:
        conversation_text = extract_conversation_text(transcript_path)
        if not conversation_text.strip():
            return {"skipped": "empty transcript", "archived_path": archived_path}

        result = classify_transcript(pcp_dir, conversation_text, source)
        business_items = result.get("business_items", []) or []
        technical_items = result.get("technical_items", []) or []

        if business_items:
            apply_business_items(pcp_dir, business_items, source)
        if technical_items:
            apply_technical_items(pcp_dir, technical_items, source, session_id)

        return {
            "business_count": len(business_items), "technical_count": len(technical_items),
            "archived_path": archived_path,
        }
    except Exception as e:
        return {"skipped": f"capture failed: {e}", "archived_path": archived_path}
