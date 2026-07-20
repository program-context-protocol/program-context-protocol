"""Integrity Auditor -- generalizes coverage_audit.py's statistical-drift
pattern across telemetry/evidence signals PCP already records but never
analyzed this way: criteria completing suspiciously fast relative to their
declared logic_tier, a module with an outlier concentration of coerced-
placeholder flags vs. project average, the same gate finding recurring
across many criteria without genuinely resolving, and evidence files
suspiciously uniform/templated across criteria.

Deterministic-only in this version -- no LLM call. Retrospective by nature
(reads already-complete criteria): it cannot correct what's already built,
only flag for human review, the same advisory posture every other audit
pass in this codebase (coverage_audit, audit.py) already has. Runs at wave
boundaries, not per-criterion -- the value is seeing patterns across many
completed criteria that no single-criterion CTRL check can see by design.
"""

import hashlib
from collections import defaultdict
from pathlib import Path

import yaml

# Below this wall-clock duration, a criterion declaring a rung this
# demanding (real ML/RAG/cache work expected) finishing implies either a
# trivial implementation or a rubber-stamped gate -- worth a human glance,
# not proof of either.
FAST_COMPLETION_MS_THRESHOLD = 45_000
FAST_COMPLETION_MIN_TIER = 4

# A module whose placeholder-flag rate is this many times the project
# average reads as a systemic pattern, not noise from one lazy criterion.
PLACEHOLDER_OUTLIER_RATIO = 2.0
_PLACEHOLDER_CHECKS = frozenset({"design-justification", "build-vs-buy-justification", "customization"})

# A finding (or identical evidence) recurring across at least this many
# distinct criteria without ever landing a "pass" afterward reads as a
# stuck pattern, not a one-off.
RECURRING_MIN_CRITERIA = 3


def _module_acceptance(pcp_dir: Path) -> dict:
    modules_dir = pcp_dir / "strategy" / "modules"
    out = {}
    if not modules_dir.is_dir():
        return out
    for mod_dir in modules_dir.iterdir():
        acc_path = mod_dir / "acceptance.yaml"
        if acc_path.is_file():
            try:
                out[mod_dir.name] = yaml.safe_load(acc_path.read_text()) or {}
            except yaml.YAMLError:
                continue
    return out


def _signal_fast_completions(records: list[dict], acceptance_by_module: dict) -> list[str]:
    tier_by_crit = {}
    for mod_name, acc in acceptance_by_module.items():
        for c in acc.get("criteria", []) or []:
            tier_by_crit[(mod_name, c.get("id"))] = c.get("logic_tier")

    findings = []
    for r in records:
        if r.get("cycle") != "build" or not r.get("duration_ms"):
            continue
        key = (r.get("module"), r.get("criterion_id"))
        tier = tier_by_crit.get(key)
        if not isinstance(tier, int) or tier < FAST_COMPLETION_MIN_TIER:
            continue
        if r["duration_ms"] < FAST_COMPLETION_MS_THRESHOLD:
            findings.append(
                f"{key[0]}/{key[1]}: declares logic_tier={tier} but completed in "
                f"{r['duration_ms'] / 1000:.1f}s -- suspiciously fast for a rung expecting "
                "real implementation work, worth a human glance"
            )
    return findings


def _signal_placeholder_concentration(records: list[dict]) -> list[str]:
    flagged_by_module = defaultdict(int)
    total_by_module = defaultdict(int)
    for r in records:
        if r.get("cycle") != "qa" or r.get("check") not in _PLACEHOLDER_CHECKS:
            continue
        mod = r.get("module")
        total_by_module[mod] += 1
        if r.get("errors"):
            flagged_by_module[mod] += 1

    total_checks = sum(total_by_module.values())
    if not total_checks:
        return []
    overall_rate = sum(flagged_by_module.values()) / total_checks
    if overall_rate == 0:
        return []

    findings = []
    for mod, total in total_by_module.items():
        flagged = flagged_by_module[mod]
        rate = flagged / total
        if flagged >= 2 and rate >= overall_rate * PLACEHOLDER_OUTLIER_RATIO:
            findings.append(
                f"{mod}: placeholder-flag rate {rate:.0%} vs. project average {overall_rate:.0%} "
                f"({flagged}/{total} checks flagged) -- outlier concentration, worth reviewing "
                "this module's declarations specifically"
            )
    return findings


def _signal_recurring_findings(records: list[dict]) -> list[str]:
    seen: dict = defaultdict(set)
    for r in records:
        if r.get("cycle") != "qa" or r.get("result") != "block":
            continue
        crit_key = (r.get("module"), r.get("criterion_id"))
        for err in r.get("errors") or []:
            sig = " ".join(err.split()[:8]).lower()
            if sig:
                seen[(r.get("check"), sig)].add(crit_key)

    findings = []
    for (check, sig), crits in seen.items():
        if len(crits) >= RECURRING_MIN_CRITERIA:
            examples = ", ".join(f"{m}/{c}" for m, c in sorted(crits, key=lambda t: (t[0] or "", t[1] or ""))[:5])
            findings.append(
                f"[{check}] finding recurring near-verbatim across {len(crits)} distinct criteria "
                f"({examples}) -- \"{sig}...\" -- same gate keeps firing without genuinely resolving, "
                "may need a fix upstream of any one criterion"
            )
    return findings


def _signal_uniform_evidence(pcp_dir: Path, records: list[dict]) -> list[str]:
    by_check_hash: dict = defaultdict(set)
    for r in records:
        if r.get("cycle") != "qa" or not r.get("evidence_path"):
            continue
        path = pcp_dir / r["evidence_path"]
        try:
            content = path.read_text(errors="replace").strip()
        except OSError:
            continue
        if not content:
            continue
        digest = hashlib.sha256(content.encode()).hexdigest()
        by_check_hash[(r.get("check"), digest)].add((r.get("module"), r.get("criterion_id")))

    findings = []
    for (check, _digest), crits in by_check_hash.items():
        if len(crits) >= RECURRING_MIN_CRITERIA:
            examples = ", ".join(f"{m}/{c}" for m, c in sorted(crits, key=lambda t: (t[0] or "", t[1] or ""))[:5])
            findings.append(
                f"[{check}] identical evidence content across {len(crits)} distinct criteria "
                f"({examples}) -- suspiciously uniform/templated, check whether these attempts "
                "genuinely differ or one got copy-pasted"
            )
    return findings


def analyze(pcp_dir: Path) -> list[str]:
    """Deterministic-only pass. Returns findings for the caller to
    print/record -- advisory, never blocks, never corrects an already-
    complete criterion (retrospective by nature)."""
    from pcp import telemetry

    records = telemetry.load(pcp_dir)
    if not records:
        return []
    acceptance_by_module = _module_acceptance(pcp_dir)

    findings: list[str] = []
    findings += _signal_fast_completions(records, acceptance_by_module)
    findings += _signal_placeholder_concentration(records)
    findings += _signal_recurring_findings(records)
    findings += _signal_uniform_evidence(pcp_dir, records)
    return findings
