"""run_log.py — pre/post audit bracket around any development or test run.

Answers "where is the bluffing happening" by recording a PRE entry before a
run starts and a POST entry after it ends, linked by run_id, hash-chained
(same mechanism as telemetry.jsonl/decision_log.jsonl). The anomaly_flags on
a POST entry are computed deterministically from git state and the PRE
entry's own data -- never an LLM judgment about itself. A run claiming
"success" with no new commit, no test evidence, and only LLM-judged checks
having run is exactly the pattern this exists to make visible instead of
indistinguishable from real work in the same ledger.

Two ways an entry gets written:
1. `pcp run-log start`/`end` -- a human or an interactive Claude Code session
   brackets its own manual/direct-edit work. Token/cost fields here are
   self-reported (self_reported_usage=True) -- there is no independent
   source for an interactive session's own usage, and this module does not
   pretend otherwise.
2. build.py's `_build_one_criterion` calls start_run()/end_run() directly,
   wired from the real `claude -p --output-format json` envelope
   (self_reported_usage=False) -- the same real numbers token_ledger.yaml
   already gets, just also bracketed here with git-state proof.
"""

import hashlib
import json
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pcp.evidence_chain import chain_entry
from pcp.objective_conflicts import objective_hash

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

_DETERMINISTIC_CHECKS = {
    "tests", "lint", "sast", "l1", "scope", "design_consistency",
    "bvb_justification", "customization", "lazy_marker", "a11y",
}
_LLM_JUDGED_CHECKS = {"arch", "gate", "design_justification", "visual_quality"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime(_TS_FMT)


def _git_head(project_root: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=project_root, capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _pushed_to_remote(project_root: Path, sha: str | None) -> bool:
    if not sha:
        return False
    result = subprocess.run(
        ["git", "branch", "-r", "--contains", sha], cwd=project_root, capture_output=True, text=True,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _ledger_path(pcp_dir: Path) -> Path:
    return Path(pcp_dir) / "run_ledger.jsonl"


def _load(path: Path) -> list[dict]:
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


def _append(pcp_dir: Path, fields: dict) -> dict:
    path = _ledger_path(pcp_dir)
    records = _load(path)
    prev_hash = records[-1].get("entry_hash") if records else None
    entry = chain_entry(prev_hash, fields)
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def start_run(
    pcp_dir: Path, *, module: str, feature: str, run_type: str, actor: str,
    business_objective: str | None = None, model: str | None = None,
) -> str:
    """PRE record. Returns run_id -- pass it to end_run() to close the bracket."""
    project_root = Path(pcp_dir).parent
    run_id = str(uuid.uuid4())
    obj_text = business_objective
    if obj_text is None:
        obj_path = Path(pcp_dir) / "objective.md"
        obj_text = obj_path.read_text()[:300] if obj_path.exists() else ""
    _append(pcp_dir, {
        "run_id": run_id, "phase": "pre", "timestamp": _now(),
        "business_objective": obj_text, "objective_hash": objective_hash(Path(pcp_dir)),
        "module": module, "feature": feature, "run_type": run_type, "actor": actor,
        "start_time": _now(), "model": model, "pre_commit_sha": _git_head(project_root),
    })
    return run_id


def end_run(
    pcp_dir: Path, run_id: str, *, result: str,
    model: str | None = None, token_input: int = 0, token_output: int = 0,
    token_cache_read: int = 0, cost_usd: float | None = None,
    tests_ran: bool | None = None, tests_passed: bool | None = None,
    real_gates_passed: list[str] | None = None, llm_judged_gates_passed: list[str] | None = None,
    note: str | None = None, self_reported_usage: bool = False,
) -> dict:
    """POST record, same run_id. anomaly_flags are computed here,
    deterministically, from git state + the PRE record -- never from an LLM
    asked to grade the run it just did."""
    project_root = Path(pcp_dir).parent
    records = _load(_ledger_path(pcp_dir))
    pre = next(
        (r for r in reversed(records) if r.get("run_id") == run_id and r.get("phase") == "pre"), None,
    )

    post_commit_sha = _git_head(project_root)
    pre_commit_sha = pre.get("pre_commit_sha") if pre else None
    committed = bool(post_commit_sha and post_commit_sha != pre_commit_sha)
    pushed = _pushed_to_remote(project_root, post_commit_sha) if committed else False
    real_gates_passed = real_gates_passed or []
    llm_judged_gates_passed = llm_judged_gates_passed or []

    proof = {
        "tests_ran": tests_ran, "tests_passed": tests_passed,
        "real_gates_passed": real_gates_passed, "llm_judged_gates_passed": llm_judged_gates_passed,
        "committed": committed, "pushed": pushed, "post_commit_sha": post_commit_sha,
    }

    anomalies = []
    if not committed:
        anomalies.append("no_commit: claimed run produced no new commit")
    if not real_gates_passed and llm_judged_gates_passed:
        anomalies.append("all_self_judged: only LLM-judged checks ran, zero deterministic verification")
    if tests_ran is False or tests_ran is None:
        anomalies.append("no_test_evidence: no test-suite result recorded for this run")
    if pre is not None and objective_hash(Path(pcp_dir)) != pre.get("objective_hash"):
        anomalies.append("objective_drifted: objective.md changed mid-run — result may target a stale spec")
    if result == "success" and not committed and not (tests_ran and tests_passed):
        anomalies.append("unverified_success: claimed success with no commit AND no passing test evidence")

    fields = {
        "run_id": run_id, "phase": "post", "timestamp": _now(), "end_time": _now(),
        "duration_sec": None, "model": model,
        "token_input": token_input, "token_output": token_output, "token_cache_read": token_cache_read,
        "cost_usd": cost_usd, "self_reported_usage": self_reported_usage,
        "result": result, "proof_of_delivery": proof, "note": note, "anomaly_flags": anomalies,
    }
    if pre is not None:
        try:
            start_dt = datetime.strptime(pre["start_time"], _TS_FMT).replace(tzinfo=timezone.utc)
            end_dt = datetime.strptime(fields["end_time"], _TS_FMT).replace(tzinfo=timezone.utc)
            fields["duration_sec"] = (end_dt - start_dt).total_seconds()
        except (KeyError, ValueError):
            pass

    return _append(pcp_dir, fields)


def load(pcp_dir: Path) -> list[dict]:
    return _load(_ledger_path(pcp_dir))


def pair_runs(records: list[dict]) -> list[dict]:
    """One row per completed run (pre+post merged), newest last. A pre with
    no matching post yet (run still open, or the process died mid-run)
    is surfaced separately by callers that want it -- never silently
    dropped here."""
    pres = {r["run_id"]: r for r in records if r.get("phase") == "pre" and r.get("run_id")}
    pairs = []
    for r in records:
        if r.get("phase") != "post":
            continue
        pre = pres.get(r.get("run_id"), {})
        pairs.append({**pre, **r})
    return pairs


def open_runs(records: list[dict]) -> list[dict]:
    """PRE records with no matching POST -- a run that started and never
    closed (crash, forgotten `end`, killed process). Not an error by
    itself, but worth surfacing: an open run is one more way real work can
    go unaccounted."""
    closed_ids = {r["run_id"] for r in records if r.get("phase") == "post" and r.get("run_id")}
    return [r for r in records if r.get("phase") == "pre" and r.get("run_id") not in closed_ids]
