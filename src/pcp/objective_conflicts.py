"""Objective-conflict gate.

Closes a real incident, 2026-07-22 (Project O dogfood): a business
decision to stop storing business-transaction instances was made and agreed
2026-07-20; objective.md/target_state.md (human-AUTHORIZED, Hard Rule 2 — see
`pcp correct-objective` for the propose/diff/approve path) never
got rewritten; two days later a 30+-agent, multi-million-token `pcp build`
cycle built exactly the rejected shape end-to-end (storage layer, agent tools,
UI) — every gate passed, because every gate validates the build against
objective.md as given, never against whether objective.md is still true.

`capture.py`'s classifier already sets `drift_flag` on a brd_items.yaml entry
when a captured business item conflicts with objective.md's text — that
machinery existed and, if it had run, would have caught this. But the flag was
purely advisory (buried in brd.md prose, three days before the incident, in a
session nobody reread). This module gives it teeth: an active item with a live
drift_flag hard-blocks `pcp build` until objective.md/target_state.md is
actually edited (verified by content hash — proof of an edit, not a checkbox a
human can click without doing the work) or a human explicitly dismisses it
with a reason.
"""

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import yaml

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def objective_hash(pcp_dir: Path) -> str:
    """Deterministic fingerprint of the immutable spec files a business
    correction would need to change. Comparing this at flag-time vs.
    check-time is the proof an edit actually happened."""
    parts = []
    for name in ("objective.md", "target_state.md"):
        p = pcp_dir / name
        parts.append(p.read_text() if p.exists() else "")
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


def _load_items(pcp_dir: Path) -> list[dict]:
    path = pcp_dir / "brd_items.yaml"
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return []
    items = data.get("items", [])
    return items if isinstance(items, list) else []


def _save_items(pcp_dir: Path, items: list[dict]) -> None:
    (pcp_dir / "brd_items.yaml").write_text(yaml.dump({"items": items}, default_flow_style=False))


def is_unresolved_conflict(item: dict) -> bool:
    """One definition of "this conflict is still open", shared by the build
    gate and every renderer.

    It existed only inside reconcile()'s loop until 2026-07-25, when a real
    divergence surfaced: `pcp objective-conflicts --dismiss` writes
    drift_dismissed_at/_reason but deliberately leaves drift_flag set (the flag
    is the historical record of what was flagged). The gate honoured the
    dismissal; capture._write_brd_md() filtered on drift_flag alone, so a
    dismissed item kept rendering under "Drift Flags" in brd.md forever —
    reporting an open conflict the build no longer had. Any new consumer must
    call this rather than re-deriving the predicate."""
    if item.get("status") != "active" or not item.get("drift_flag"):
        return False
    return not (item.get("drift_resolved_at") or item.get("drift_dismissed_at"))


def reconcile(pcp_dir: Path) -> list[dict]:
    """Auto-clears drift flags whose objective_hash_at_flag no longer matches
    current objective.md/target_state.md content -- the file actually got
    edited since the conflict was raised. Deterministic, no LLM judgment
    involved in the resolution itself (only in the original flagging, inside
    capture.py). Returns the still-unresolved conflicts: active, drift_flag
    set, neither hash-cleared nor dismissed.

    An item with a drift_flag but no objective_hash_at_flag (written before
    this mechanism existed, or by any other path) is treated as unresolved —
    fails loud rather than silently trusting an unstamped flag, same posture
    as this project's other fail-open-gate fixes."""
    items = _load_items(pcp_dir)
    current_hash = objective_hash(pcp_dir)
    now = datetime.now(timezone.utc).strftime(_TS_FMT)
    changed = False
    unresolved = []

    for item in items:
        if not is_unresolved_conflict(item):
            continue
        flagged_hash = item.get("objective_hash_at_flag")
        if flagged_hash and flagged_hash != current_hash:
            item["drift_resolved_at"] = now
            item["drift_resolved_reason"] = "objective.md/target_state.md edited since this conflict was flagged"
            changed = True
            continue
        unresolved.append(item)

    if changed:
        _save_items(pcp_dir, items)
    return unresolved


def dismiss(pcp_dir: Path, item_id: str, reason: str) -> bool:
    """Human explicitly dismisses a flagged conflict without editing
    objective.md -- for real false positives (classifier flagged something
    that doesn't actually require a spec change). Requires a non-empty
    reason -- same accountability posture as `[pcp-bypass: reason]`. Returns
    True if a matching active, undismissed, unresolved item was found."""
    if not reason or not reason.strip():
        raise ValueError("dismiss requires a non-empty reason")
    items = _load_items(pcp_dir)
    now = datetime.now(timezone.utc).strftime(_TS_FMT)
    found = False
    for item in items:
        if item.get("id") == item_id and item.get("drift_flag") and not item.get("drift_resolved_at") and not item.get("drift_dismissed_at"):
            item["drift_dismissed_at"] = now
            item["drift_dismissed_reason"] = reason.strip()
            found = True
            break
    if found:
        _save_items(pcp_dir, items)
    return found
