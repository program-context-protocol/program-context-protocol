"""Traceability-map review audit trail — append-only JSONL, one record per
human review decision (approve/reject/edit) on a feature-to-module link.
Same record/load/aggregate triad as decision_log.py/ontology_review_log.py.
Auto-appended by `pcp trace-review`. Never edit.
"""

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def record(pcp_dir: Path, **fields) -> None:
    """Suggested fields: link_id, action ("approve"|"reject"|"edit"),
    original_confidence_score (snapshotted at review time), new_label
    (only for "edit")."""
    entry = {"timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **fields}
    path = Path(pcp_dir) / "traceability_review_log.jsonl"
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def load(pcp_dir: Path) -> list[dict]:
    path = Path(pcp_dir) / "traceability_review_log.jsonl"
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


def aggregate(records: list[dict]) -> dict:
    by_action = defaultdict(list)
    for r in records:
        by_action[r.get("action") or "unknown"].append(r)
    return {"by_action": by_action, "records": records}


def rejected_ids(pcp_dir: Path) -> set[str]:
    """ids whose most recent review action was 'reject' — must stay excluded
    from any fresh classification merge (see traceability.merge_with_existing)."""
    latest_action: dict[str, str] = {}
    for r in load(pcp_dir):
        link_id = r.get("link_id")
        if link_id:
            latest_action[link_id] = r.get("action")
    return {link_id for link_id, action in latest_action.items() if action == "reject"}
