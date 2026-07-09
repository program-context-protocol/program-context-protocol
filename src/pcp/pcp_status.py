"""Shared pcp.md governance snapshot writer.

Called by `pcp scan` (auto) and `pcp status` (on-demand).
pcp.md lives at project root — never inside .pcp/.
"""

from pathlib import Path
import re
import yaml


def _read_optional(path: Path, max_chars: int = 600) -> str:
    if not path.exists():
        return ""
    text = path.read_text().strip()
    return text[:max_chars] + ("\n…" if len(text) > max_chars else "")


def _evaluate_exit_criterion(c: dict, project_root: Path) -> bool:
    check = c.get("check", "manual")
    if check == "file_exists":
        return (project_root / c.get("target", "")).exists()
    if check == "ast_pattern":
        target = project_root / c.get("target", "")
        pattern = c.get("pattern", "")
        if not target.exists() or not pattern:
            return c.get("status") == "complete"
        import re
        return bool(re.search(pattern, target.read_text(errors="replace"), re.MULTILINE))
    return c.get("status") == "complete"


def _extract_phase(pcp_dir: Path) -> tuple[str, list[dict]]:
    sdlc_path = pcp_dir / "SDLC_phase.yaml"
    if not sdlc_path.exists():
        return "unknown", []
    data = yaml.safe_load(sdlc_path.read_text()) or {}
    current = data.get("current_phase", "unknown")
    phases = data.get("phases", [])
    current_phase_data = next((p for p in phases if p["name"] == current), {})
    project_root = pcp_dir.parent
    criteria = current_phase_data.get("exit_criteria", [])
    # Evaluate deterministic checks live
    evaluated = []
    for c in criteria:
        done = _evaluate_exit_criterion(c, project_root)
        evaluated.append({**c, "_done": done})
    return current, evaluated


def _extract_bypass_count(pcp_dir: Path) -> int:
    log_path = pcp_dir / "bypass_log.yaml"
    if not log_path.exists():
        return 0
    data = yaml.safe_load(log_path.read_text()) or {}
    return len(data.get("bypasses", []))


def _extract_audit_summary(pcp_dir: Path) -> str | None:
    audit_path = pcp_dir / "audit.md"
    if not audit_path.exists():
        return None
    generated, findings = "", None
    for line in audit_path.read_text().splitlines():
        if line.startswith("Generated:"):
            generated = line.split(":", 1)[1].strip()
        elif line.startswith("Findings:"):
            findings = line.split(":", 1)[1].strip()
    if findings is None:
        return f"_Last run {generated} — no audit tool detected._" if generated else None
    return f"{findings} dead-code finding(s) — last run {generated}"


def _extract_token_summary(pcp_dir: Path) -> str | None:
    ledger_path = pcp_dir / "token_ledger.yaml"
    if not ledger_path.exists():
        return None
    data = yaml.safe_load(ledger_path.read_text()) or {}
    calls = data.get("calls", [])
    if not calls:
        return None
    total_input = sum(c.get("input_tokens", 0) + c.get("cache_creation_tokens", 0) for c in calls)
    total_cache_read = sum(c.get("cache_read_tokens", 0) for c in calls)
    total_output = sum(c.get("output_tokens", 0) for c in calls)
    total_cost = sum(c.get("cost_usd") or 0 for c in calls)
    by_model = {}
    for c in calls:
        m = c.get("model", "default")
        by_model[m] = by_model.get(m, 0) + 1
    model_breakdown = ", ".join(f"{m}:{n}" for m, n in sorted(by_model.items()))
    return (
        f"{len(calls)} LLM call(s) — {total_input:,} input / {total_cache_read:,} cache-read / "
        f"{total_output:,} output tokens — ~${total_cost:.2f} — by model: {model_breakdown}"
    )


def _extract_test_coverage(pcp_dir: Path) -> str | None:
    cs = pcp_dir / "current_state.md"
    if not cs.exists():
        return None
    text = cs.read_text()
    m = re.search(r"## Test Coverage\n([\d.]+)% \(([^)]+)\)", text)
    return f"{m.group(1)}% ({m.group(2)})" if m else None


def _extract_telemetry_summary(pcp_dir: Path) -> str | None:
    from pcp import telemetry
    records = telemetry.load(pcp_dir)
    if not records:
        return None
    agg = telemetry.aggregate(records)
    by_module = agg["by_module"]
    if not by_module:
        return None
    total_qa = sum(v["qa_total"] for v in by_module.values())
    total_blocks = sum(v["qa_blocks"] for v in by_module.values())
    total_attempts = len(agg["build_records"])
    total_criteria = len({(m, c) for m, v in by_module.items() for c in v["criteria"]})
    avg_attempts = total_attempts / total_criteria if total_criteria else 0.0
    qa_rate = f"{total_blocks}/{total_qa}" if total_qa else "—"
    worst_module = max(
        by_module.items(),
        key=lambda kv: (kv[1]["attempts"] / (len(kv[1]["criteria"]) or 1)),
        default=None,
    )
    worst_note = ""
    if worst_module and len(by_module) > 1:
        wname, wv = worst_module
        wavg = wv["attempts"] / (len(wv["criteria"]) or 1)
        if wavg > avg_attempts:
            worst_note = f" — highest retry rate: `{wname}` ({wavg:.1f} attempts/criterion)"
    return (
        f"{total_criteria} criteria built, {avg_attempts:.1f} avg attempts/criterion, "
        f"QA blocks {qa_rate}{worst_note}. Run `pcp telemetry` for the per-module breakdown."
    )


def _extract_brd_summary(pcp_dir: Path) -> str | None:
    brd_path = pcp_dir / "brd.md"
    if not brd_path.exists():
        return None
    items_path = pcp_dir / "brd_items.yaml"
    items = (yaml.safe_load(items_path.read_text()) or {}).get("items", []) if items_path.exists() else []
    active = [i for i in items if i.get("status") == "active"]
    drift = [i for i in active if i.get("drift_flag")]
    if not active:
        return None
    note = f", {len(drift)} drift flag(s) vs objective.md" if drift else ""
    return f"{len(active)} active requirement(s){note}. See `brd.md`."


def _extract_decision_log_summary(pcp_dir: Path) -> str | None:
    from pcp import decision_log
    records = decision_log.load(pcp_dir)
    if not records:
        return None
    agg = decision_log.aggregate(records)
    by_category = agg["by_category"]
    breakdown = ", ".join(f"{cat}:{len(items)}" for cat, items in sorted(by_category.items()))
    return f"{len(records)} technical decision(s) captured — {breakdown}. Run `pcp telemetry` or see `.pcp/decision_log.jsonl`."


def _extract_pending_gaps(pcp_dir: Path) -> list[str]:
    cs = pcp_dir / "current_state.md"
    if not cs.exists():
        return []
    gaps = []
    for line in cs.read_text().splitlines():
        if re.match(r"\s*- \[ \]", line):
            gaps.append(line.strip()[6:])
    return gaps


def _module_table(modules_results: list[dict]) -> list[str]:
    lines = [
        "| Module | Criteria | Complete | Pending |",
        "|---|---|---|---|",
    ]
    for m in modules_results:
        total = len(m["criteria"])
        complete = sum(1 for c in m["criteria"] if c["status"] == "complete")
        pending = total - complete
        status = "✓" if pending == 0 else f"{pending} pending"
        lines.append(f"| `{m['module']}` | {total} | {complete} | {status} |")
    return lines


def _phase_exit_table(exit_criteria: list[dict]) -> list[str]:
    lines = []
    for c in exit_criteria:
        done = c.get("_done", c.get("status") == "complete")
        mark = "x" if done else " "
        lines.append(f"- [{mark}] **{c['id']}**: {c['description']}")
    return lines or ["_No exit criteria defined._"]


def extract_objective_text(pcp_dir: Path) -> str:
    """First non-heading body text from objective.md. Real bug, found
    2026-07-08: a heading immediately followed by its body text on the very
    next line (no blank line between them -- completely normal markdown,
    e.g. "## Why This Exists\nBecause...") used to make the WHOLE block get
    rejected outright (the paragraph, heading+body glued together, "starts
    with #"), silently discarding real objective text and falling through
    to "No objective.md found" even though the file existed and had real
    content. Strips leading heading line(s) off each paragraph block first,
    and only rejects a block if nothing but headings remain. Factored out
    of write_pcp_md so dashboard.py can reuse the same extraction instead
    of duplicating this parsing."""
    obj_path = pcp_dir / "objective.md"
    if not obj_path.exists():
        return ""
    raw = obj_path.read_text()
    for block in re.split(r"\n{2,}", raw):
        lines = block.strip().split("\n")
        while lines and lines[0].strip().startswith("#"):
            lines.pop(0)
        body = "\n".join(lines).strip()
        if body:
            return body[:500]
    return ""


def write_pcp_md(
    pcp_dir: Path,
    modules_results: list[dict],
    timestamp: str,
    total: int,
    complete: int,
) -> Path:
    """Write pcp.md to project root. Returns path."""
    project_root = pcp_dir.parent
    project_name = project_root.name
    score = complete / total if total else 0.0
    score_pct = f"{score:.0%}"

    phase_name, exit_criteria = _extract_phase(pcp_dir)
    bypass_count = _extract_bypass_count(pcp_dir)
    pending_gaps = _extract_pending_gaps(pcp_dir)
    audit_summary = _extract_audit_summary(pcp_dir)
    token_summary = _extract_token_summary(pcp_dir)
    telemetry_summary = _extract_telemetry_summary(pcp_dir)
    test_coverage = _extract_test_coverage(pcp_dir)
    brd_summary = _extract_brd_summary(pcp_dir)
    decision_log_summary = _extract_decision_log_summary(pcp_dir)
    obj_text = extract_objective_text(pcp_dir)

    lines = [
        f"# PCP Governance — {project_name}",
        f"_Updated: {timestamp} | Phase: `{phase_name}` | Coverage: {score_pct} | Bypasses: {bypass_count}_",
        "",
        "> Auto-generated by `pcp scan`. Do not edit manually.",
        "",
        "## Objective",
        "",
        obj_text or "_No objective.md found. Run `pcp init`._",
        "",
        f"## SDLC Phase: `{phase_name}`",
        "",
        *_phase_exit_table(exit_criteria),
        "",
        "## Module Status",
        "",
        *_module_table(modules_results),
        "",
    ]

    if pending_gaps:
        lines += [
            "## Pending Gaps",
            "",
            *[f"- [ ] {g}" for g in pending_gaps],
            "",
        ]
    else:
        lines += [
            "## Pending Gaps",
            "",
            "_All acceptance criteria met._",
            "",
        ]

    if test_coverage:
        lines += ["## Test Coverage", "", test_coverage, ""]

    if audit_summary:
        lines += ["## Dead Code / Bloat", "", audit_summary, ""]

    if token_summary:
        lines += ["## Token Spend", "", token_summary, ""]

    if telemetry_summary:
        lines += ["## Build Efficiency", "", telemetry_summary, ""]

    if brd_summary:
        lines += ["## Business Requirements (Living)", "", brd_summary, ""]

    if decision_log_summary:
        lines += ["## Technical Decisions", "", decision_log_summary, ""]

    if bypass_count > 0:
        log_path = pcp_dir / "bypass_log.yaml"
        data = yaml.safe_load(log_path.read_text()) or {}
        bypasses = data.get("bypasses", [])
        lines += ["## Bypass Log", ""]
        for b in bypasses[-5:]:  # last 5 only
            lines.append(f"- `{b.get('timestamp', '')}` — {b.get('reason', '')} (rules: {', '.join(b.get('rules_bypassed', []))})")
        lines.append("")
    else:
        lines += ["## Bypass Log", "", "_No bypasses._", ""]

    out = project_root / "pcp.md"
    out.write_text("\n".join(lines))
    return out
