"""Automatic hash-chain integrity guard for the 3 append-only evidence logs
(telemetry.jsonl, decision_log.jsonl, bypass_log.yaml).

`pcp provenance` already verified these chains (`verify_chain()`,
evidence_chain.py) — but only when a human remembered to run it. Ganesh
2026-08-07: a tampered record should block new work happening on top of it,
not sit undetected until someone happens to check. `assert_chain_integrity`
is the fail-loud version, called at the START of `pcp build`/`pcp verify`/
`pcp scan` (before any real work) rather than left as an opt-in audit.

Deterministic, no LLM — same as verify_chain() itself.
"""

from pathlib import Path

import yaml

from pcp import telemetry as telemetry_mod
from pcp import decision_log as decision_log_mod
from pcp.evidence_chain import verify_chain


def load_bypasses(pcp_dir: Path) -> list[dict]:
    path = Path(pcp_dir) / "bypass_log.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    return data.get("bypasses", [])


def check_all_chains(pcp_dir: Path) -> dict[str, list[dict]]:
    """Per-log break lists — empty list means that log's chain is intact."""
    return {
        "telemetry.jsonl": verify_chain(telemetry_mod.load(pcp_dir)),
        "decision_log.jsonl": verify_chain(decision_log_mod.load(pcp_dir)),
        "bypass_log.yaml": verify_chain(load_bypasses(pcp_dir)),
    }


class ChainIntegrityError(Exception):
    """Raised by assert_chain_integrity when a log has a CRITICAL finding —
    a record that claimed a hash and doesn't verify (edited, reordered, or
    deleted after the fact). An "info"-severity finding (an unchained
    legacy/ad-hoc entry — see verify_chain's docstring) never raises this;
    it's surfaced via `pcp provenance` instead, since it isn't tamper
    evidence, just an unverifiable one."""

    def __init__(self, broken: dict[str, list[dict]]):
        self.broken = broken
        lines = []
        for name, breaks in broken.items():
            for b in breaks:
                lines.append(f"  {name}[{b['index']}]: {b['issue']}")
        super().__init__(
            "Evidence chain integrity broken -- a record was edited, reordered, "
            "or deleted after the fact:\n" + "\n".join(lines)
        )


def assert_chain_integrity(pcp_dir: Path) -> None:
    """Raises ChainIntegrityError if any log has a critical-severity finding.
    Call before trusting these logs for new work. Silently returns for a
    project with no chained history yet, or one whose only findings are
    info-severity (unchained legacy/ad-hoc entries — real signal, but not
    tamper evidence, so it doesn't block; see `pcp provenance` for those)."""
    breaks = check_all_chains(pcp_dir)
    critical = {
        name: [b for b in bs if b.get("severity") == "critical"]
        for name, bs in breaks.items()
    }
    critical = {name: b for name, b in critical.items() if b}
    if critical:
        raise ChainIntegrityError(critical)
