"""Rolling project-level spend ceiling.

Closes the gap found in the 2026-07-17 market sweep: PCP bounded individual
attempts (PCP_BUILD_AGENT_MAX_BUDGET_USD) and runs (PCP_MAX_BUILD_SESSIONS)
but nothing capped cumulative project spend over time — the primitive
Portkey's auto-expiring budget keys and OpenRouter's hard spend guardrails
treat as table stakes. Reads .pcp/token_ledger.yaml (already written by every
LLM call), no new bookkeeping.

Ceilings (both optional, unset = unlimited, matching every other PCP cap):
  PCP_PROJECT_BUDGET_USD        — lifetime ceiling for this project
  PCP_PROJECT_DAILY_BUDGET_USD  — rolling ceiling for the current UTC day

Enforcement is refuse-loudly-at-session-start (build/watch check before
spawning a new agent session), never mid-attempt kill — an attempt already
paid for gets to finish and be recorded.
"""

import os
from datetime import datetime, timezone
from pathlib import Path

import yaml


def _ledger_costs(pcp_dir: Path) -> list[tuple[str, float]]:
    path = Path(pcp_dir) / "token_ledger.yaml"
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return []
    out = []
    for c in data.get("calls", []):
        cost = c.get("cost_usd")
        if isinstance(cost, (int, float)):
            out.append((str(c.get("timestamp", "")), float(cost)))
    return out


def project_spend(pcp_dir: Path) -> dict:
    """Total and today's (UTC) spend from the ledger."""
    costs = _ledger_costs(pcp_dir)
    today_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return {
        "total_usd": round(sum(c for _, c in costs), 4),
        "today_usd": round(sum(c for ts, c in costs if ts.startswith(today_prefix)), 4),
    }


def check_ceiling(pcp_dir: Path) -> tuple[bool, str]:
    """(allowed, reason). Allowed unless a configured ceiling is breached."""
    total_cap = os.environ.get("PCP_PROJECT_BUDGET_USD")
    daily_cap = os.environ.get("PCP_PROJECT_DAILY_BUDGET_USD")
    if not total_cap and not daily_cap:
        return True, "no project spend ceiling configured"
    spend = project_spend(pcp_dir)
    if total_cap:
        try:
            if spend["total_usd"] >= float(total_cap):
                return False, (
                    f"project spend ${spend['total_usd']:.2f} >= ceiling ${float(total_cap):.2f} "
                    "(PCP_PROJECT_BUDGET_USD) — raise the ceiling or archive the ledger to proceed"
                )
        except ValueError:
            pass
    if daily_cap:
        try:
            if spend["today_usd"] >= float(daily_cap):
                return False, (
                    f"today's spend ${spend['today_usd']:.2f} >= daily ceiling ${float(daily_cap):.2f} "
                    "(PCP_PROJECT_DAILY_BUDGET_USD) — resumes next UTC day or raise the ceiling"
                )
        except ValueError:
            pass
    return True, "within configured ceilings"
