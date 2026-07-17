"""Escalation ledger + staleness watchdog.

Closes the "Escalation Failure" mode (a human never actually sees the
escalation) — PCP has lived this exact incident once: a slack-notify SSL
failure silently fell back to log-only and a security STOP sat unread for
8 days. Recording an escalation is not the same thing as a human seeing it.

`.pcp/escalations.yaml` is a plain append list (operational record, not
hash-chained — same posture as prune_log.yaml). An entry is considered
RESOLVED when its criterion is no longer pending in that module's
acceptance.yaml (completed, or removed entirely) — a deterministic proxy
for "a human acted on it", no ack command or LLM needed. Anything still
unresolved past PCP_ESCALATION_STALE_HOURS (default 24) is stale and gets
surfaced loudly by `pcp watch` and `pcp status`.
"""

import os
from datetime import datetime, timezone
from pathlib import Path

import yaml

ESCALATIONS_FILE = "escalations.yaml"
DEFAULT_STALE_HOURS = 24


def _stale_hours() -> float:
    return float(os.environ.get("PCP_ESCALATION_STALE_HOURS", str(DEFAULT_STALE_HOURS)))


def record(pcp_dir: Path, module: str, criterion_id: str, route: str = "human",
           findings: list[str] | None = None) -> None:
    """Append one escalation entry. Never raises — an escalation must not be
    lost because the ledger write failed, the console/notify path still runs."""
    path = pcp_dir / ESCALATIONS_FILE
    entry = {
        "module": module,
        "criterion_id": criterion_id,
        "route": route,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "findings_count": len(findings or []),
        "findings_preview": (findings or [])[:3],
    }
    try:
        entries = load(pcp_dir)
        entries.append(entry)
        path.write_text(yaml.dump({"escalations": entries}, default_flow_style=False))
    except Exception:
        pass


def load(pcp_dir: Path) -> list[dict]:
    path = pcp_dir / ESCALATIONS_FILE
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return []
    entries = data.get("escalations", [])
    return entries if isinstance(entries, list) else []


def _criterion_still_pending(pcp_dir: Path, module: str, criterion_id: str) -> bool:
    """Resolution proxy: pending in acceptance.yaml means nobody has acted yet.
    A completed or removed criterion counts as resolved. A module that no
    longer exists also counts as resolved (dropped module)."""
    af = pcp_dir / "strategy" / "modules" / module / "acceptance.yaml"
    if not af.exists():
        return False
    try:
        data = yaml.safe_load(af.read_text()) or {}
    except yaml.YAMLError:
        return False
    for c in data.get("criteria", []):
        if c.get("id") == criterion_id:
            return c.get("status", "pending") != "complete"
    return False


def find_stale(pcp_dir: Path, now: datetime | None = None) -> list[dict]:
    """Unresolved escalations older than PCP_ESCALATION_STALE_HOURS."""
    now = now or datetime.now(timezone.utc)
    threshold_sec = _stale_hours() * 3600
    stale = []
    for e in load(pcp_dir):
        try:
            ts = datetime.strptime(e.get("timestamp", ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        age_sec = (now - ts).total_seconds()
        if age_sec < threshold_sec:
            continue
        if _criterion_still_pending(pcp_dir, e.get("module", ""), e.get("criterion_id", "")):
            e = dict(e)
            e["age_hours"] = round(age_sec / 3600, 1)
            stale.append(e)
    return stale
