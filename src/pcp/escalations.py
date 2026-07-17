"""Escalation ledger + staleness watchdog.

Closes the "Escalation Failure" mode (a human never actually sees the
escalation) — PCP has lived this exact incident once: a slack-notify SSL
failure silently fell back to log-only and a security STOP sat unread for
8 days. Recording an escalation is not the same thing as a human seeing it.

v2 (2026-07-17, PagerDuty/incident.io reference patterns):
- ACK and RESOLVE are separate states with separate timers. "Acknowledged"
  (a human saw it — `pcp escalations --ack module/criterion`) is distinct
  from "resolved" (the criterion actually left pending). An acked-but-stalled
  escalation re-screams after 2x the threshold — a glance at Slack must not
  silence the watchdog permanently.
- Stakes-scaled timeout: a module that other modules depend on blocks future
  waves — its escalations go stale at HALF the normal threshold
  (incident.io's SLO-derived-timeout pattern, deterministic here).
- MTTA: median time-to-acknowledge across acked entries, surfaced in
  provenance — the feedback loop proving the ledger is actually watched.
- Failure category (memory/planning/action/system, from the AgentDebug
  taxonomy arXiv:2509.25370) recorded per escalation, keyword-classified
  deterministically from the block findings — sharpens human routing.

`.pcp/escalations.yaml` is a plain append list (operational record, not
hash-chained — same posture as prune_log.yaml). Resolution stays the
deterministic proxy: criterion no longer pending in acceptance.yaml.
"""

import os
from datetime import datetime, timezone
from pathlib import Path

import yaml

ESCALATIONS_FILE = "escalations.yaml"
DEFAULT_STALE_HOURS = 24
_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _stale_hours() -> float:
    return float(os.environ.get("PCP_ESCALATION_STALE_HOURS", str(DEFAULT_STALE_HOURS)))


def classify_failure(findings: list[str]) -> str:
    """Deterministic keyword classification into the AgentDebug taxonomy —
    advisory routing signal, not ground truth."""
    text = " ".join(findings).lower()
    if "timeout" in text or "killed" in text or "exited with" in text or "errored" in text:
        return "system"
    if "test suite" in text or "lint" in text or "sast" in text or "scope guard" in text:
        return "action"
    if "alignment" in text or "architect" in text or "regression" in text or "coverage" in text:
        return "planning"
    if "context" in text or "session" in text:
        return "memory"
    return "uncategorized"


def record(pcp_dir: Path, module: str, criterion_id: str, route: str = "human",
           findings: list[str] | None = None) -> None:
    """Append one escalation entry. Never raises — an escalation must not be
    lost because the ledger write failed, the console/notify path still runs."""
    path = pcp_dir / ESCALATIONS_FILE
    entry = {
        "module": module,
        "criterion_id": criterion_id,
        "route": route,
        "category": classify_failure(findings or []),
        "timestamp": datetime.now(timezone.utc).strftime(_TS_FMT),
        "acknowledged_at": None,
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


def acknowledge(pcp_dir: Path, module: str, criterion_id: str) -> int:
    """Stamp acknowledged_at on all un-acked entries for module/criterion.
    Returns count acked."""
    entries = load(pcp_dir)
    now = datetime.now(timezone.utc).strftime(_TS_FMT)
    count = 0
    for e in entries:
        if e.get("module") == module and e.get("criterion_id") == criterion_id and not e.get("acknowledged_at"):
            e["acknowledged_at"] = now
            count += 1
    if count:
        (pcp_dir / ESCALATIONS_FILE).write_text(yaml.dump({"escalations": entries}, default_flow_style=False))
    return count


def _criterion_still_pending(pcp_dir: Path, module: str, criterion_id: str) -> bool:
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


def _module_has_dependents(pcp_dir: Path, module: str) -> bool:
    """True if any OTHER module declares this one as a dependency — its
    failure blocks future waves, so its escalations get half the stale
    window (stakes-scaled timeout)."""
    modules_dir = pcp_dir / "strategy" / "modules"
    if not modules_dir.exists():
        return False
    for spec_path in modules_dir.glob("*/spec.yaml"):
        if spec_path.parent.name == module:
            continue
        try:
            spec = yaml.safe_load(spec_path.read_text()) or {}
        except yaml.YAMLError:
            continue
        if module in (spec.get("dependencies") or []):
            return True
    return False


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, _TS_FMT).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def find_stale(pcp_dir: Path, now: datetime | None = None) -> list[dict]:
    """Unresolved escalations needing a scream. Two states:
    - state="unacked": no ack, older than the (stakes-scaled) threshold
    - state="acked-stalled": acked, but criterion still pending past 2x
      threshold since the ack — seen is not fixed."""
    now = now or datetime.now(timezone.utc)
    base_sec = _stale_hours() * 3600
    stale = []
    for e in load(pcp_dir):
        ts = _parse_ts(e.get("timestamp", ""))
        if ts is None:
            continue
        if not _criterion_still_pending(pcp_dir, e.get("module", ""), e.get("criterion_id", "")):
            continue
        threshold_sec = base_sec / 2 if _module_has_dependents(pcp_dir, e.get("module", "")) else base_sec
        ack_ts = _parse_ts(e.get("acknowledged_at") or "")
        e = dict(e)
        if ack_ts is None:
            age_sec = (now - ts).total_seconds()
            if age_sec >= threshold_sec:
                e["state"] = "unacked"
                e["age_hours"] = round(age_sec / 3600, 1)
                stale.append(e)
        else:
            since_ack = (now - ack_ts).total_seconds()
            if since_ack >= 2 * threshold_sec:
                e["state"] = "acked-stalled"
                e["age_hours"] = round(since_ack / 3600, 1)
                stale.append(e)
    return stale


def mtta_hours(pcp_dir: Path) -> float | None:
    """Median time-to-acknowledge in hours across acked entries — the
    'is anyone actually watching this ledger' metric (PagerDuty Escalation
    Policy Insights pattern). None when nothing has ever been acked."""
    deltas = []
    for e in load(pcp_dir):
        ts, ack = _parse_ts(e.get("timestamp", "")), _parse_ts(e.get("acknowledged_at") or "")
        if ts and ack:
            deltas.append((ack - ts).total_seconds() / 3600)
    if not deltas:
        return None
    deltas.sort()
    mid = len(deltas) // 2
    return round(deltas[mid] if len(deltas) % 2 else (deltas[mid - 1] + deltas[mid]) / 2, 2)
