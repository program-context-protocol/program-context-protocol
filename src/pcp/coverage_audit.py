"""Coverage-score audit trail — mitigates Goodhart risk on validate-strategy's
LLM-judged coverage_score. Unlike coupling_score (see coupling.py), coverage
can't be made deterministic — "does this cover the objective" is genuinely
semantic. What CAN be deterministic: catching internal inconsistency (a high
score reported alongside real open gaps) and drift (the same inputs producing
a meaningfully different score run to run). Both are surfaced, never silently
corrected — same append-only audit-trail posture as decision_log.py/telemetry.py.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

# A score this high alongside any open gap is internally inconsistent —
# gaps are the real gate, the number shouldn't read as "basically done".
INCONSISTENCY_THRESHOLD = 0.85

# Score delta on unchanged objective/modules worth flagging as possible
# LLM non-determinism rather than a real coverage change.
DRIFT_THRESHOLD = 0.15


def _hash_inputs(objective: str, modules: dict) -> str:
    material = objective + "".join(f"{k}:{v}" for k, v in sorted(modules.items()))
    return hashlib.sha256(material.encode()).hexdigest()[:12]


def load(pcp_dir: Path) -> list[dict]:
    path = Path(pcp_dir) / "coverage_audit.jsonl"
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


def record(pcp_dir: Path, coverage_score: float, gaps: list, objective: str, modules: dict) -> list[str]:
    """Appends one entry to .pcp/coverage_audit.jsonl. Returns any findings
    (inconsistency/drift) for the caller to surface — never raises, never
    silently corrects the score itself."""
    path = Path(pcp_dir) / "coverage_audit.jsonl"
    inputs_hash = _hash_inputs(objective, modules)
    findings: list[str] = []

    if gaps and coverage_score >= INCONSISTENCY_THRESHOLD:
        findings.append(
            f"coverage_score reported {coverage_score:.0%} despite {len(gaps)} open gap(s) — "
            "internally inconsistent, treat the score with skepticism (the gaps are the real gate)."
        )

    prior = load(pcp_dir)
    same_input_runs = [r for r in prior if r.get("inputs_hash") == inputs_hash]
    if same_input_runs:
        last = same_input_runs[-1]
        delta = abs(coverage_score - last["coverage_score"])
        if delta >= DRIFT_THRESHOLD:
            findings.append(
                f"coverage_score drifted {last['coverage_score']:.0%} -> {coverage_score:.0%} "
                "on an unchanged objective/module set — possible LLM non-determinism, not a real coverage change."
            )

    entry = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "coverage_score": coverage_score, "gap_count": len(gaps), "inputs_hash": inputs_hash,
        "findings": findings,
    }
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return findings
