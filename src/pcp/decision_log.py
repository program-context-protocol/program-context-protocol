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


def aggregate(records: list[dict]) -> dict:
    """Roll up decisions per category. Shared by `pcp telemetry`-style reporting
    and the pcp.md 'Technical Decisions' section."""
    by_category = defaultdict(list)
    for r in records:
        by_category[r.get("category") or "uncategorized"].append(r)
    return {"by_category": by_category, "records": records}
