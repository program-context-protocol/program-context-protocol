"""Per-build-cycle telemetry — file/line/language/qa-result granularity for analysis.

Distinct from token_ledger.yaml (flat call-level cost rollup feeding pcp.md).
This is JSONL — one record per build-cycle event (a coding attempt, or a QA
check against that attempt) — so it loads straight into pandas/duckdb/jq for
analysis without parsing nested YAML. Auto-appended by `pcp build`. Never edit.
"""

import json
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from pcp.evidence_chain import chain_entry, set_append_only

LANGUAGE_BY_EXT = {
    ".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript",
    ".jsx": "JavaScript", ".go": "Go", ".rs": "Rust", ".java": "Java", ".rb": "Ruby",
    ".yaml": "YAML", ".yml": "YAML", ".json": "JSON", ".md": "Markdown",
    ".sql": "SQL", ".sh": "Shell", ".css": "CSS", ".html": "HTML", ".c": "C",
    ".cpp": "C++", ".h": "C/C++ header", ".swift": "Swift", ".kt": "Kotlin",
}


def infer_languages(file_paths: list[str]) -> list[str]:
    langs = set()
    for f in file_paths:
        ext = Path(f).suffix
        langs.add(LANGUAGE_BY_EXT.get(ext, ext.lstrip(".") or "unknown"))
    return sorted(langs)


def count_diff_lines(diff: str) -> tuple[int, int]:
    """(lines_added, lines_removed) from a unified diff, excluding +++/--- headers."""
    added = sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))
    return added, removed


def record(pcp_dir: Path, **fields) -> None:
    """Append one JSONL record to .pcp/telemetry.jsonl.

    Suggested fields (not enforced — callers pass whatever's available):
    module, submodule, criterion_id, cycle ("build"|"qa"), cycle_number (attempt #),
    check (for qa: "layer1"|"architect-review"|"gate"), result ("pass"|"block"|"error"),
    errors (list of finding strings), files (list of paths touched), languages,
    lines_added, lines_removed, model, session_id, token_input, token_output,
    token_cache_read, token_cache_creation, cost_usd, duration_ms.
    """
    fields = {"timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **fields}
    path = Path(pcp_dir) / "telemetry.jsonl"
    entry = chain_entry(_last_entry_hash(path), fields)
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")
    set_append_only(path)


def _last_entry_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    last_line = None
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            last_line = line
    if not last_line:
        return None
    try:
        return json.loads(last_line).get("entry_hash")
    except json.JSONDecodeError:
        return None


def load(pcp_dir: Path) -> list[dict]:
    path = Path(pcp_dir) / "telemetry.jsonl"
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


def productivity_by_week(records: list[dict]) -> list[dict]:
    """Spend and net lines written per ISO week, plus $ per net line.

    Nothing in PCP reported output-per-dollar over time, so a real 6x degradation
    went unseen. Measured by hand on Project O 2026-07-30: 07-16..07-22
    produced +4,954 net non-test LOC for $364.77 (~$0.07/line), while 07-23..07-30
    produced +596 for $263.89 (~$0.44/line) -- while commits/day ROSE. Commit count
    and output per dollar were pointing in opposite directions and only the flattering
    one was visible anywhere.

    Deliberately built from telemetry's own `lines_added`/`lines_removed`, not from
    git, so this stays a pure aggregation over data PCP already owns and needs no
    repo access. The honest reading of `net_lines` is therefore "lines this build
    loop wrote, net" -- it counts every attempt's diff, including work a later
    attempt superseded, so it is an upper bound on repo growth, not a measurement
    of it. `$/net line` is a trend signal, not an accounting figure: watch whether
    it moves, not what it equals.

    Weeks with no recorded lines report None rather than a division-by-zero-shaped
    number -- a week that spent money and wrote nothing is a real state and must not
    be rendered as "$0.00/line".
    """
    by_week: dict[str, dict] = defaultdict(
        lambda: {"cost": 0.0, "lines_added": 0, "lines_removed": 0, "attempts": 0}
    )
    for r in records:
        ts = str(r.get("timestamp") or "")
        if len(ts) < 10:
            continue
        try:
            year, week, _ = date.fromisoformat(ts[:10]).isocalendar()
        except ValueError:
            continue
        w = by_week[f"{year}-W{week:02d}"]
        w["cost"] += r.get("cost_usd") or 0
        w["lines_added"] += r.get("lines_added") or 0
        w["lines_removed"] += r.get("lines_removed") or 0
        if r.get("cycle") == "build":
            w["attempts"] += 1

    out = []
    for label in sorted(by_week):
        w = by_week[label]
        net = w["lines_added"] - w["lines_removed"]
        out.append({
            "week": label,
            "cost_usd": round(w["cost"], 2),
            "net_lines": net,
            "attempts": w["attempts"],
            "usd_per_net_line": round(w["cost"] / net, 3) if net > 0 else None,
        })
    return out


_EMPTY_REPO_LINES = {"by_week": {}, "bulk_commits_skipped": {}, "bulk_threshold": 0}


def repo_net_lines_by_week(project_root: Path, exclude_tests: bool = True,
                           bulk_commit_threshold: int = 5000) -> dict:
    """Net authored source lines that actually LANDED in the repo, per ISO week.

    Returns {"by_week": {week: net}, "bulk_commits_skipped": {week: n},
    "bulk_threshold": n}. Callers must surface `bulk_commits_skipped` -- a metric
    that quietly drops data is the failure mode this whole module is trying to fix.

    This exists because `productivity_by_week` alone is misleading, and shipping it
    alone would have repeated the exact failure this session kept finding: a metric
    that reads healthy while the situation it describes is not.

    On Project O, week 2026-W31: telemetry recorded **+12,342 net lines
    written** at $0.018/line, which looks like the most productive week of the run.
    Git says non-test code in the repo grew by **+599** over the same window. Both
    are true. Telemetry counts every attempt's diff, so superseded attempts, reverted
    work, rewrites and test code all inflate it; git counts what survived.

    Neither number is the interesting one. **The ratio is** — roughly 5% of what the
    loop wrote survived as net non-test product code. That is the number worth
    watching, and no single-source metric can express it.

    **Vendored third-party source is the hard case and the reason for
    `bulk_commit_threshold`.** Extension filtering is not enough: Project O
    committed an entire drawio distribution under `web/drawio-site/` and
    `web/public/drawio/` in one week and moved it the next -- ~450,000 lines of
    third-party `.js` sitting in no conventionally-named vendor directory. That
    produced survival rates of 107372% and -11249% before this filter existed.

    No path heuristic catches that reliably, so the discriminator is size: a single
    commit churning more than `bulk_commit_threshold` authored-source lines is a
    vendor import, a bulk move, or a generated dump -- not one criterion's work.
    Such commits are excluded WHOLE, and the count is returned per week so the
    caller can say so out loud. Never silently.

    Best-effort: returns empty structures when git is unavailable or the call fails,
    so callers degrade to the telemetry-only view rather than breaking.
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "log", "--numstat", "--date=short", "--pretty=format:__C__%cd"],
            cwd=project_root, capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return _EMPTY_REPO_LINES
    if proc.returncode != 0:
        return _EMPTY_REPO_LINES

    # Per-commit first, so a bulk vendor import can be excluded as a whole.
    per_commit: list[tuple[str, int, int]] = []   # (week, net, churn)
    current: str | None = None
    net = churn = 0

    def flush():
        if current is not None:
            per_commit.append((current, net, churn))

    for line in proc.stdout.splitlines():
        if line.startswith("__C__"):
            flush()
            net = churn = 0
            try:
                year, week, _ = date.fromisoformat(line[5:].strip()).isocalendar()
                current = f"{year}-W{week:02d}"
            except ValueError:
                current = None
            continue
        if not current or "\t" not in line:
            continue
        parts = line.split("\t")
        # Binary files report "-" for both counts; skip rather than crash.
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        path = parts[2]
        if not _is_authored_source(path):
            continue
        if exclude_tests and (path.startswith("tests/") or "/test_" in path
                              or path.split("/")[-1].startswith("test_")):
            continue
        added, removed = int(parts[0]), int(parts[1])
        net += added - removed
        churn += added + removed
    flush()

    by_week: dict[str, int] = defaultdict(int)
    skipped: dict[str, int] = defaultdict(int)
    for week, cnet, cchurn in per_commit:
        if cchurn > bulk_commit_threshold:
            skipped[week] += 1
            continue
        by_week[week] += cnet
    return {"by_week": dict(by_week), "bulk_commits_skipped": dict(skipped),
            "bulk_threshold": bulk_commit_threshold}


# Extensions a human or an agent actually authors. Everything else -- lockfiles,
# bundles, vendored trees, snapshots, data -- is generated or copied, and counting
# it destroys the metric rather than enriching it.
_SOURCE_EXTS = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".rb", ".kt",
    ".swift", ".c", ".cpp", ".h", ".cs", ".php", ".scala", ".ex", ".exs",
    ".sh", ".sql", ".vue", ".svelte", ".css", ".scss", ".html",
})
_GENERATED_DIR_MARKERS = (
    "node_modules/", ".venv/", "venv/", "vendor/", "dist/", "build/", "site-packages/",
    "__pycache__/", ".next/", "coverage/", "migrations/", "__snapshots__/", "generated/",
)
_GENERATED_NAMES = frozenset({
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "Cargo.lock", "go.sum", "uv.lock", "composer.lock",
})


def _is_authored_source(path: str) -> bool:
    """Is this a file a person or agent wrote, rather than one a tool emitted?

    Without this filter the metric is not merely noisy, it is nonsense. Run against
    Project O it reported **+1,523,5xx lines landed** in one week and
    **-1,470,6xx** in another, for survival rates of 362745% and -38701% -- lockfiles
    and generated bundles swamping the signal by three orders of magnitude.

    Worth stating plainly: that output was produced by a first version of this
    function and caught only by running it against a real repo before shipping.
    A metric that confidently reports a wrong number is worse than no metric, which
    is the same defect class as every other finding in this session's audit.
    """
    name = path.split("/")[-1]
    if name in _GENERATED_NAMES:
        return False
    if any(marker in path for marker in _GENERATED_DIR_MARKERS):
        return False
    dot = name.rfind(".")
    return dot > 0 and name[dot:].lower() in _SOURCE_EXTS


def issues_after_first_green(records: list[dict]) -> dict:
    """Per (module, criterion_id): how many later QA/build issues (result
    "block"/"error") were recorded AFTER that criterion's first "pass" --
    across ALL recorded history, not just one run, so a later `pcp verify`,
    `pcp pressure-test`, or manual re-check finding a real problem in an
    already-"complete" criterion counts exactly as much as a same-run retry.

    Real, measurable proxy for "did verification actually dig" vs. "did it
    pass" (backlog item #3, 2026-08-09): avrt alone had 5 stacked bugs found
    by peeling one at a time, several found only AFTER an earlier attempt
    had already reported pass. A build report that only shows the LAST
    attempt's result would never surface that a criterion needed repeated
    correction after looking done -- a high post-green count on a criterion
    is a real signal that layer marked it done too early, not proof of a
    bug in the metric.

    Criteria that never passed are excluded (not applicable -- there is no
    "after first green" to measure). Criteria that passed and never had a
    later issue are excluded too (clean, nothing to report) -- same
    report-first, findings-only posture as `audit.md`/`design_audit.md`."""
    by_key: dict[tuple, list[dict]] = defaultdict(list)
    for r in records:
        crit = r.get("criterion_id")
        if not crit:
            continue
        by_key[(r.get("module") or "?", crit)].append(r)

    flagged = []
    criteria_with_a_pass = 0
    for (module, crit), recs in by_key.items():
        recs_sorted = sorted(recs, key=lambda r: str(r.get("timestamp") or ""))
        first_pass_idx = next((i for i, r in enumerate(recs_sorted) if r.get("result") == "pass"), None)
        if first_pass_idx is None:
            continue
        criteria_with_a_pass += 1
        after = recs_sorted[first_pass_idx + 1:]
        issues = [r for r in after if r.get("result") in ("block", "error")]
        if not issues:
            continue
        flagged.append({
            "module": module,
            "criterion_id": crit,
            "first_pass_at": recs_sorted[first_pass_idx].get("timestamp"),
            "issues_found_after": len(issues),
            "issue_checks": [r.get("check") for r in issues],
        })

    flagged.sort(key=lambda f: -f["issues_found_after"])
    return {
        "criteria_with_a_pass": criteria_with_a_pass,
        "criteria_with_post_green_issues": len(flagged),
        "flagged": flagged,
    }


def aggregate(records: list[dict]) -> dict:
    """Roll up build/qa records per module. Shared by `pcp telemetry`, end-of-build
    summary, and the pcp.md 'Build Efficiency' section — one aggregation, three views."""
    build_records = [r for r in records if r.get("cycle") == "build"]
    qa_records = [r for r in records if r.get("cycle") == "qa"]

    by_module = defaultdict(lambda: {
        "attempts": 0, "criteria": set(), "tokens_in": 0, "tokens_out": 0,
        "tokens_cache_read": 0, "cost": 0.0, "qa_blocks": 0, "qa_total": 0, "languages": set(),
    })
    for r in build_records:
        m = by_module[r.get("module") or "?"]
        m["attempts"] += 1
        m["criteria"].add(r.get("criterion_id"))
        m["tokens_in"] += r.get("token_input", 0)
        m["tokens_out"] += r.get("token_output", 0)
        m["tokens_cache_read"] += r.get("token_cache_read", 0)
        m["cost"] += r.get("cost_usd") or 0
        m["languages"].update(r.get("languages") or [])
    for r in qa_records:
        m = by_module[r.get("module") or "?"]
        m["qa_total"] += 1
        if r.get("result") == "block":
            m["qa_blocks"] += 1

    return {"by_module": by_module, "build_records": build_records, "qa_records": qa_records}
