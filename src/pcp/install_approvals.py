"""Install-only fast-path approval log.

A human confirms a priorart direct match before `pcp build` skips the full
TDD/architect-review/LLM-gate cycle for a criterion (or whole module) and
just installs a dependency. Hash-chained like bypass_log.yaml/telemetry.jsonl
-- see evidence_chain.py.
"""

from datetime import datetime, timezone
from pathlib import Path

import yaml

from pcp.evidence_chain import chain_entry

LOG_NAME = "install_approvals.yaml"


def log_install_approval(
    pcp_dir: Path, *, module: str, criterion_id: str | None,
    candidate: str, install_command: str, decision: str, actor: str = "human",
) -> None:
    """decision: 'confirm' or 'reject'. criterion_id=None means a
    module-level (whole-module) approval, not a single criterion."""
    log_path = pcp_dir / LOG_NAME
    existing = []
    if log_path.exists():
        data = yaml.safe_load(log_path.read_text()) or {}
        existing = data.get("approvals", [])

    prev_hash = existing[-1].get("entry_hash") if existing else None
    fields = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "actor": actor,
        "module": module,
        "criterion_id": criterion_id,
        "candidate": candidate,
        "install_command": install_command,
        "decision": decision,
    }
    existing.append(chain_entry(prev_hash, fields))

    with open(log_path, "w") as f:
        yaml.dump({"approvals": existing}, f, default_flow_style=False)
