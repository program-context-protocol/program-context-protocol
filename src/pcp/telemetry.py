"""Per-build-cycle telemetry — file/line/language/qa-result granularity for analysis.

Distinct from token_ledger.yaml (flat call-level cost rollup feeding pcp.md).
This is JSONL — one record per build-cycle event (a coding attempt, or a QA
check against that attempt) — so it loads straight into pandas/duckdb/jq for
analysis without parsing nested YAML. Auto-appended by `pcp build`. Never edit.
"""

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

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
    entry = {"timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **fields}
    path = Path(pcp_dir) / "telemetry.jsonl"
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


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
