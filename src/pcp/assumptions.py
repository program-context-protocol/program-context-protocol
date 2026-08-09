"""Assumptions log -- persisted store of the assumptions a program's strategy
relies on, populated by kickoff/pm's `assumptions_enumerated` field (DECOMPOSE
FIRST discipline, same posture as capabilities_enumerated) and rolled up into
assumptions.md.

Real gap this closes: IEEE/ISO 29148's SRS template makes an explicit
"Assumptions" section mandatory -- what the strategy is relying on being true
(an external API's behavior, a data volume, a user's technical level) that
nobody actually stated anywhere PCP tracks. An unstated assumption drifting
silently is the same shape of incident as the objective-staleness gate
(CTRL-035) closes, just one level earlier in the pipeline -- an assumption,
not the objective itself, turns out to be false, and nothing was ever
written down to check it against.

Not a protected/human-authorized file (Hard Rule 2's ten paths) -- same
posture as brd_items.yaml/decision_log.jsonl: the LLM proposes items inline
in its own kickoff/pm response, this module is the only thing that ever
writes them to disk, and a human confirms/invalidates individual items
afterward (`pcp assumptions --confirm/--invalidate`) rather than approving a
full-file rewrite the way `pcp amend` works for the ten protected files."""

import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _yaml_path(pcp_dir: Path) -> Path:
    return pcp_dir / "assumptions.yaml"


def load(pcp_dir: Path) -> list[dict]:
    path = _yaml_path(pcp_dir)
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return []
    items = data.get("assumptions", [])
    return items if isinstance(items, list) else []


def _save(pcp_dir: Path, items: list[dict]) -> None:
    _yaml_path(pcp_dir).write_text(
        yaml.dump({"assumptions": items}, default_flow_style=False, sort_keys=False)
    )


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-zA-Z]{4,}", text.lower()))


def _is_duplicate(statement: str, existing_statements: list[str]) -> bool:
    """Deterministic keyword-overlap dedup -- same primitive as kickoff.py's
    _is_genuinely_new (loop_until_dry_breakdown), applied here so the same
    assumption re-stated across a kickoff and several later pm calls doesn't
    pile up near-identical duplicate entries."""
    stmt_words = _words(statement)
    if not stmt_words:
        return False
    for existing in existing_statements:
        if len(stmt_words & _words(existing)) >= max(1, len(stmt_words) // 2):
            return True
    return False


def render_markdown(items: list[dict], timestamp: str) -> str:
    lines = [
        "# Assumptions Log",
        "",
        f"_Auto-generated at {timestamp} by `pcp kickoff`/`pcp pm`/`pcp assumptions`. "
        "Never hand-edit -- items are populated by kickoff/pm's `assumptions_enumerated` "
        "field; this is a rollup view, not a second place to author them._",
        "",
    ]
    if not items:
        lines.append("_No assumptions recorded yet._")
        return "\n".join(lines)

    open_items = [i for i in items if i.get("status") == "open"]
    lines.append(f"**{len(open_items)} open / {len(items)} total.**")
    lines.append("")
    lines.append("| ID | Statement | Status | Source | Added |")
    lines.append("|---|---|---|---|---|")
    for i in items:
        lines.append(
            f"| {i.get('id', '')} | {i.get('statement', '')} | {i.get('status', '')} "
            f"| {i.get('source', '')} | {i.get('added_at', '')} |"
        )
    if open_items:
        lines.append("")
        lines.append(
            "Confirm: `pcp assumptions --confirm ASxxx` · "
            'Invalidate: `pcp assumptions --invalidate ASxxx --reason "..."`'
        )
    return "\n".join(lines)


def _write_md(pcp_dir: Path, items: list[dict]) -> Path:
    timestamp = datetime.now(timezone.utc).strftime(_TS_FMT)
    out = pcp_dir / "assumptions.md"
    out.write_text(render_markdown(items, timestamp))
    return out


def merge_new(pcp_dir: Path, statements: list[str], *, source: str) -> list[dict]:
    """Appends genuinely-new assumption statements to assumptions.yaml
    (deterministic dedup against everything already on disk, including prior
    kickoff/pm runs) and unconditionally regenerates assumptions.md -- same
    always-refresh posture as capture.py's apply_business_items/brd.md, so the
    rollup never goes stale between runs even when nothing new was added.
    Returns only the newly-added items (empty list = nothing new, not an
    error). `source` is the command that produced them ("kickoff" or "pm"),
    stamped per item for audit -- same posture as decision_log.jsonl's own
    `source` field."""
    existing = load(pcp_dir)
    existing_statements = [i.get("statement", "") for i in existing]
    max_num = 0
    for item in existing:
        m = re.match(r"^AS(\d+)$", str(item.get("id", "")))
        if m:
            max_num = max(max_num, int(m.group(1)))

    now = datetime.now(timezone.utc).strftime(_TS_FMT)
    added = []
    for raw in statements:
        stmt = (raw or "").strip()
        if not stmt or _is_duplicate(stmt, existing_statements):
            continue
        max_num += 1
        item = {
            "id": f"AS{max_num:03d}",
            "statement": stmt,
            "status": "open",
            "source": source,
            "added_at": now,
        }
        existing.append(item)
        existing_statements.append(stmt)
        added.append(item)

    if added:
        pcp_dir.mkdir(parents=True, exist_ok=True)
        _save(pcp_dir, existing)
    _write_md(pcp_dir, existing)
    return added


_VALID_STATUSES = {"confirmed", "invalidated"}


def set_status(pcp_dir: Path, item_id: str, status: str, reason: str = "") -> bool:
    """Human marks an open assumption confirmed (verified true) or
    invalidated (turned out false -- requires a non-empty reason, same
    accountability posture as `[pcp-bypass: reason]` and
    objective_conflicts.dismiss). Returns True if a matching OPEN item was
    found; regenerates assumptions.md either way is the caller's job (the
    CLI command does it), since this function's job is just the state
    transition."""
    if status not in _VALID_STATUSES:
        raise ValueError(f"status must be one of {sorted(_VALID_STATUSES)}")
    if status == "invalidated" and not (reason or "").strip():
        raise ValueError("invalidating an assumption requires a non-empty reason")

    items = load(pcp_dir)
    now = datetime.now(timezone.utc).strftime(_TS_FMT)
    found = False
    for item in items:
        if item.get("id") == item_id and item.get("status") == "open":
            item["status"] = status
            item[f"{status}_at"] = now
            if reason:
                item[f"{status}_reason"] = reason.strip()
            found = True
            break
    if found:
        _save(pcp_dir, items)
        _write_md(pcp_dir, items)
    return found
