"""CTRL-037: build-loop bypass detector (2026-07-24).

Real, recurring incident (Project O, 07-08 / mid-July / 07-21-onward,
3 occurrences): `pcp build`'s formal gated loop (architect-review, QA,
wave-merge, telemetry) silently stops being used -- not by decision, just by
default -- while real commits keep landing via `pcp pm` + ad-hoc/manual
work. `telemetry.jsonl` going stale IS the finding, not evidence the project
went idle; nothing previously surfaced that at the moment it started
happening. Deterministic: git log commit dates vs telemetry.jsonl's last
entry timestamp, no LLM judgment.
"""

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pcp import telemetry

DEFAULT_THRESHOLD_DAYS = 3
_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _last_telemetry_ts(pcp_dir: Path) -> datetime | None:
    records = telemetry.load(pcp_dir)
    if not records:
        return None
    timestamps = [r["timestamp"] for r in records if r.get("timestamp")]
    if not timestamps:
        return None
    latest = max(timestamps)
    try:
        return datetime.strptime(latest, _TS_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _commits_since(project_root: Path, since: datetime) -> int:
    result = subprocess.run(
        ["git", "log", f"--since={since.isoformat()}", "--format=%H"],
        cwd=project_root, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return 0
    return len([l for l in result.stdout.splitlines() if l.strip()])


def check(pcp_dir: Path, project_root: Path, threshold_days: int | None = None) -> list[str]:
    """Returns findings; [] if inert (no telemetry history yet, not a git
    repo) or nothing to flag. Never raises -- advisory, same posture as
    every other doctor-time check."""
    threshold_days = threshold_days if threshold_days is not None else int(
        os.environ.get("PCP_BUILD_LOOP_BYPASS_THRESHOLD_DAYS", DEFAULT_THRESHOLD_DAYS)
    )
    last_ts = _last_telemetry_ts(pcp_dir)
    if last_ts is None:
        return []  # never built via pcp build at all yet -- nothing to compare against
    if not (project_root / ".git").exists():
        return []

    cutoff = last_ts + timedelta(days=threshold_days)
    now = datetime.now(timezone.utc)
    if cutoff >= now:
        return []  # still inside the grace window

    commit_count = _commits_since(project_root, cutoff)
    if commit_count == 0:
        return []

    age_days = (now - last_ts).days
    return [
        f"{commit_count} commit(s) landed since {cutoff.strftime('%Y-%m-%d')} "
        f"(>{threshold_days}d past telemetry.jsonl's last entry, {age_days}d ago) — "
        "`pcp build`'s formal gated loop (architect-review/QA/wave-merge/telemetry) "
        "may have been bypassed; work is happening outside it"
    ]
