"""End-of-cycle build report (2026-07-24). `pcp build` already generates
real evidence -- run_log.jsonl's proof-of-delivery bracket, raw QA output
under .pcp/evidence/, per-attempt telemetry.jsonl records -- but until now
none of it ever got handed to the human at the moment a run finished, just
one dim summary line. This renders what actually happened THIS run, with
evidence, so "pcp build completed" reads as something more than a plain
commit does. Pure/testable: takes a since-timestamp, does no clock reads
or side effects of its own.
"""

from pathlib import Path

from pcp import run_log


def render(pcp_dir: Path, since_ts: str) -> str:
    """since_ts: ISO timestamp, run_log's own format (%Y-%m-%dT%H:%M:%SZ).
    Only paired runs (criteria) whose POST record lands at or after this
    count as "this run" -- string comparison is safe since the format is
    fixed-width and zero-padded."""
    pairs = [p for p in run_log.pair_runs(run_log.load(pcp_dir)) if p.get("timestamp", "") >= since_ts]

    lines = ["# Build Cycle Report", ""]
    if not pairs:
        lines.append("_No criteria completed this run._")
        return "\n".join(lines) + "\n"

    succeeded = [p for p in pairs if p.get("result") == "success"]
    lines.append(f"{len(succeeded)}/{len(pairs)} criteria succeeded this run.")
    lines.append("")

    for p in pairs:
        module = p.get("module", "?")
        feature = p.get("feature", "?")
        proof = p.get("proof_of_delivery", {})
        mark = "✓" if p.get("result") == "success" else "✗"
        lines.append(f"## {mark} {module}: {feature}")
        lines.append(f"- Deterministic gates passed: {', '.join(proof.get('real_gates_passed') or []) or 'none'}")
        lines.append(f"- LLM-judged gates passed: {', '.join(proof.get('llm_judged_gates_passed') or []) or 'none'}")
        commit_note = f" ({proof['post_commit_sha'][:8]})" if proof.get("post_commit_sha") else ""
        lines.append(f"- Committed: {proof.get('committed')}  Pushed: {proof.get('pushed')}{commit_note}")
        if p.get("anomaly_flags"):
            lines.append(f"- ⚠ Anomalies: {'; '.join(p['anomaly_flags'])}")
        lines.append(f"- Raw evidence: `.pcp/evidence/{module}/` (full test/lint/SAST/architect-review output)")
        lines.append("")

    lines.append("Full audit trail: `.pcp/provenance.md` · `pcp telemetry` for per-attempt token/cost breakdown.")
    return "\n".join(lines) + "\n"


def write(pcp_dir: Path, since_ts: str) -> Path:
    out = Path(pcp_dir) / "build_report.md"
    out.write_text(render(pcp_dir, since_ts))
    return out
