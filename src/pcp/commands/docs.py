"""pcp docs — per-module doc kit: vision, BRD slice, current-built list, and
a chronological changelog that doubles as a drift ledger.

Pure aggregation over already-persisted sources (spec.yaml, acceptance.yaml,
brd_items.yaml, telemetry.jsonl, decision_log.jsonl, git history on the
module's own spec/acceptance files) — no LLM call. Same "one data source,
many views" convention as provenance.py/architecture_justification.py.

Drift-control framing, not just documentation: changelog.md merges build
completions with spec/acceptance.yaml git history into ONE chronological
timeline, so a spec change landing BETWEEN two criterion completions is
visible as a real drift signal — the module's declared intent moved while
it was mid-build — rather than buried in a separate `git log` nobody checks
against build history. bypass_log.yaml entries now carry a `modules` field
(check.py's `_attributed_modules`, added alongside this doc kit) computed
from staged-file → module-dir / criterion-target matching, so bypasses land
on this timeline too. Entries logged before that field existed have no
`modules` value and stay excluded — a known, stated limitation, not a
silent gap.
"""

import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, get_modules_dir, NoPCPDir
from pcp.schema.validator import load_yaml
from pcp import telemetry as telemetry_mod
from pcp import decision_log as decision_log_mod
from pcp.commands.build import _is_ui_facing_criterion
from pcp.commands.design_audit import _classify_rung, RUNG_LABEL

console = Console()

KIND_LABEL = {
    "build": "🔨 built",
    "spec_change": "📋 spec.yaml changed",
    "acceptance_change": "📋 acceptance.yaml changed",
    "decision": "💡 decision",
    "bypass": "⚠ gate bypassed",
}


def _load_module_bypasses(pcp_dir: Path, module_name: str) -> list[dict]:
    """Bypass entries attributed to this module via check.py's
    `_attributed_modules`. Entries logged before that field existed carry no
    `modules` key and are silently excluded — legacy data, not a bug."""
    bypass_path = pcp_dir / "bypass_log.yaml"
    if not bypass_path.exists():
        return []
    data = yaml.safe_load(bypass_path.read_text()) or {}
    return [b for b in data.get("bypasses", []) if module_name in (b.get("modules") or [])]


def _normalize_to_utc_z(ts: str) -> str:
    """git's %aI format carries the commit's local timezone offset (e.g.
    +05:30), while telemetry.jsonl timestamps are always UTC `Z`. Comparing
    the two as raw strings (this module's own timeline sort, and
    _compute_drift_score's in-flight window check) silently misorders events
    on any machine not in UTC — a +05:30 offset timestamp sorts as "later"
    than a same-instant-or-earlier Z timestamp purely because '+' > digits
    lexicographically. Normalize every git timestamp to Z before it enters
    the timeline so all timestamps are directly, correctly comparable as
    strings. Falls back to the raw value if parsing fails rather than
    dropping the entry."""
    try:
        return datetime.fromisoformat(ts).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return ts


def _git_log_for_file(project_root: Path, path: Path) -> list[dict]:
    """Chronological (oldest-first) commit history for one file — deterministic,
    no LLM. Used to surface spec/acceptance.yaml changes as drift signals."""
    if not path.exists():
        return []
    result = subprocess.run(
        ["git", "log", "--follow", "--format=%H|%aI|%s", "--", str(path)],
        cwd=project_root, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []
    entries = []
    for line in reversed(result.stdout.strip().splitlines()):
        if not line.strip():
            continue
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        entries.append({"commit": parts[0][:8], "timestamp": _normalize_to_utc_z(parts[1]), "subject": parts[2]})
    return entries


def _module_dirs(pcp_dir: Path, module_name: str | None) -> list[Path]:
    modules_dir = get_modules_dir(pcp_dir)
    if not modules_dir.exists():
        return []
    if module_name:
        d = modules_dir / module_name
        return [d] if d.exists() else []
    return sorted(p for p in modules_dir.iterdir() if p.is_dir())


def _brd_keywords(module_name: str, spec: dict) -> set[str]:
    """Best-effort attribution — brd_items.yaml carries no `module` field
    today (items are sourced from freeform session transcripts, see
    capture.py). Cheap rung-1 keyword match, no LLM: module name variants
    plus the description's own significant words."""
    kws = {
        module_name.lower(),
        module_name.replace("-", " ").lower(),
        module_name.replace("_", " ").lower(),
    }
    kws.update(re.findall(r"[a-zA-Z]{5,}", spec.get("description", "").lower())[:8])
    return kws


def _keyword_match(text: str, keywords: set[str]) -> bool:
    text_l = text.lower()
    return any(kw in text_l for kw in keywords if kw)


def _specificity_rank(items: list[dict], keywords: set[str]) -> list[dict]:
    """Specificity reordering (deterministic half of T-SimCSE's trick,
    arXiv:2603.11800): a match on a RARE keyword (appears in few BRD items)
    is stronger evidence of real attribution than a match on a word half the
    items contain. Rank matched items by summed inverse-document-frequency of
    their matched keywords — no embeddings, no LLM, pure counting; the
    embedding-similarity half of the technique is deliberately not built
    (PCP's zero-API-key architecture has no embedding provider)."""
    if not items:
        return items
    texts = [str(i.get("description", "")).lower() for i in items]
    doc_freq = {kw: sum(1 for t in texts if kw in t) for kw in keywords if kw}
    n = len(items)

    def score(idx: int) -> float:
        t = texts[idx]
        return sum((n / doc_freq[kw]) for kw in doc_freq if doc_freq[kw] and kw in t)

    order = sorted(range(n), key=score, reverse=True)
    return [items[i] for i in order]


def _compute_drift_score(timeline: list[dict], build_records: list[dict], bypass_count: int) -> dict:
    """Promotes the visual-adjacency signal this doc kit already showed (a
    human has to notice a `spec.yaml changed` entry sitting between two
    `built` entries) into an explicit, computed flag — the exact next step
    CLAUDE.md names for this doc kit. No new data source: everything here
    is already present in this module's own timeline/telemetry.

    Three signals, weighted (deliberately simple, not tuned against real
    outcome data yet — phase 1, same honest framing as the rest of this doc
    kit): in-flight spec/acceptance changes weigh heaviest since they're the
    closest thing to a directly-observed drift event (the module's declared
    intent moved while a build was still active); bypass count next (a gate
    was overridden on this module's own files); retry count least (a
    criterion can need extra attempts for reasons unrelated to drift, e.g.
    a flaky test — correlated with instability, not proof of it)."""
    build_timestamps = sorted(e["timestamp"] for e in timeline if e["kind"] == "build" and e.get("timestamp"))
    in_flight: list[dict] = []
    if len(build_timestamps) >= 2:
        window_start, window_end = build_timestamps[0], build_timestamps[-1]
        in_flight = [
            e for e in timeline
            if e["kind"] in ("spec_change", "acceptance_change")
            and window_start < (e.get("timestamp") or "") < window_end
        ]

    retries_by_criterion: dict[str, int] = {}
    for r in build_records:
        cid = r.get("criterion_id")
        if cid:
            retries_by_criterion[cid] = retries_by_criterion.get(cid, 0) + 1
    retry_count = sum(max(0, n - 1) for n in retries_by_criterion.values())

    score = min(1.0, 0.5 * len(in_flight) + 0.2 * bypass_count + 0.1 * retry_count)
    return {
        "score": round(score, 2),
        "in_flight_changes": in_flight,
        "bypass_count": bypass_count,
        "retry_count": retry_count,
    }


def build_module_docs(pcp_dir: Path, module_dir: Path) -> dict:
    """Pure aggregation for ONE module — everything the four rendered files
    need. Callable at any point, no side effects besides read."""
    project_root = pcp_dir.parent
    module_name = module_dir.name
    spec_path = module_dir / "spec.yaml"
    acc_path = module_dir / "acceptance.yaml"
    spec = load_yaml(spec_path) if spec_path.exists() else {}
    acc = load_yaml(acc_path) if acc_path.exists() else {}
    criteria = acc.get("criteria", [])

    brd_path = pcp_dir / "brd_items.yaml"
    all_brd_items = (yaml.safe_load(brd_path.read_text()) or {}).get("items", []) if brd_path.exists() else []
    keywords = _brd_keywords(module_name, spec)
    matched_brd = _specificity_rank(
        [i for i in all_brd_items if _keyword_match(i.get("description", ""), keywords)], keywords,
    )

    telemetry_records = telemetry_mod.load(pcp_dir)
    build_records = [
        r for r in telemetry_records if r.get("cycle") == "build" and r.get("module") == module_name
    ]
    # Latest attempt per criterion — a status view, not a retry history
    # (telemetry.jsonl/evidence/ already are that), same convention as
    # dashboard.py's _qa_lookup.
    latest_by_criterion: dict[str, dict] = {}
    for r in build_records:
        cid = r.get("criterion_id")
        if not cid:
            continue
        if cid not in latest_by_criterion or r.get("timestamp", "") >= latest_by_criterion[cid].get("timestamp", ""):
            latest_by_criterion[cid] = r

    decisions = [d for d in decision_log_mod.load(pcp_dir) if d.get("module") == module_name]
    spec_history = _git_log_for_file(project_root, spec_path)
    acc_history = _git_log_for_file(project_root, acc_path)
    bypasses = _load_module_bypasses(pcp_dir, module_name)

    timeline: list[dict] = []
    for cid, r in latest_by_criterion.items():
        timeline.append({
            "kind": "build", "timestamp": r.get("timestamp", ""),
            "criterion_id": cid, "files": r.get("files") or [],
            "lines_added": r.get("lines_added", 0), "lines_removed": r.get("lines_removed", 0),
        })
    for h in spec_history:
        timeline.append({"kind": "spec_change", "timestamp": h["timestamp"], "commit": h["commit"], "subject": h["subject"]})
    for h in acc_history:
        timeline.append({"kind": "acceptance_change", "timestamp": h["timestamp"], "commit": h["commit"], "subject": h["subject"]})
    for d in decisions:
        timeline.append({"kind": "decision", "timestamp": d.get("timestamp", ""), "summary": d.get("summary", ""), "category": d.get("category", "")})
    for b in bypasses:
        timeline.append({
            "kind": "bypass", "timestamp": b.get("timestamp", ""),
            "reason": b.get("reason", ""), "files": b.get("files") or [],
        })
    timeline.sort(key=lambda e: e.get("timestamp") or "")
    drift = _compute_drift_score(timeline, build_records, len(bypasses))

    ui_criteria = [c for c in criteria if _is_ui_facing_criterion(c)]

    # Best-effort BRD-vs-built cross-reference (deterministic keyword overlap,
    # no LLM, same honesty posture as the BRD attribution above): brd_items.yaml
    # only tracks active/superseded, nothing links a BRD item to the specific
    # criterion that addressed it, so "addressed" here means "some COMPLETE
    # criterion's description keyword-overlaps this item's description" --
    # a starting point for human review, not a verified claim.
    active_brd = [i for i in matched_brd if i.get("status") == "active"]
    complete_criteria = [c for c in criteria if c.get("status") == "complete"]
    brd_diff = []
    for item in active_brd:
        item_words = set(re.findall(r"[a-zA-Z]{5,}", item.get("description", "").lower()))
        likely_matches = [
            c["id"] for c in complete_criteria
            if item_words & set(re.findall(r"[a-zA-Z]{5,}", c.get("description", "").lower()))
        ]
        brd_diff.append({"item": item, "likely_addressed_by": likely_matches})

    return {
        "module_name": module_name, "spec": spec, "criteria": criteria,
        "matched_brd": matched_brd, "timeline": timeline, "bypass_count": len(bypasses),
        "drift": drift, "ui_criteria": ui_criteria, "brd_diff": brd_diff,
    }


def _render_vision(data: dict) -> str:
    spec = data["spec"]
    lines = [
        f"# {data['module_name']} — Vision",
        "",
        "> Auto-generated by `pcp docs` from `spec.yaml` + this module's declared "
        "objective coverage. Do not edit manually — edit `spec.yaml` (human-authorized, "
        "spec-immutable) instead.",
        "",
        "## What this module is for",
        "",
        spec.get("description", "_no description in spec.yaml_"),
        "",
        "## How it serves the program objective",
        "",
    ]
    lines += [f"- {c}" for c in spec.get("objective_coverage", [])] or ["_none declared_"]
    if spec.get("dependencies"):
        lines += ["", "## Depends on", ""] + [f"- `{d}`" for d in spec["dependencies"]]
    if spec.get("constraints"):
        lines += ["", "## Constraints", ""] + [f"- {c}" for c in spec["constraints"]]
    bvb = spec.get("build_vs_buy")
    if bvb:
        lines += ["", "## Build vs buy", "", f"**Decision:** `{bvb.get('decision', '')}`", "", bvb.get("rationale", "")]
    return "\n".join(lines) + "\n"


def _render_brd(data: dict) -> str:
    lines = [
        f"# {data['module_name']} — Requirements Drift (BRD slice)",
        "",
        "> Auto-generated by `pcp docs`. Best-effort keyword match against "
        "`.pcp/brd_items.yaml` — no `module` field exists on BRD items today, "
        "so this can miss real matches or include unrelated ones. Treat as a "
        "starting point, not ground truth.",
        "",
    ]
    if not data["matched_brd"]:
        lines.append("_No BRD items keyword-matched to this module yet._")
    else:
        for i in data["matched_brd"]:
            lines.append(f"- **{i.get('id')}** ({i.get('status')}): {i.get('description')}")
            if i.get("drift_flag"):
                lines.append(f"  - ⚠ drift flag: {i['drift_flag']}")
    return "\n".join(lines) + "\n"


def _render_built(data: dict) -> str:
    lines = [
        f"# {data['module_name']} — Current Built List",
        "",
        "> Auto-generated by `pcp docs` from `acceptance.yaml`. Never edit manually.",
        "",
        "| ID | Description | Status | Check | Logic Tier | Build vs Buy |",
        "|---|---|---|---|---|---|",
    ]
    for c in data["criteria"]:
        bvb = (c.get("build_vs_buy") or {}).get("decision", "—")
        lines.append(
            f"| {c['id']} | {c['description']} | {c.get('status', 'pending')} | "
            f"{c.get('check', 'manual')} | {c.get('logic_tier', '—')} | {bvb} |"
        )
    complete = sum(1 for c in data["criteria"] if c.get("status") == "complete")
    lines += ["", f"**{complete}/{len(data['criteria'])} criteria complete.**"]
    return "\n".join(lines) + "\n"


def _render_changelog(data: dict) -> str:
    lines = [
        f"# {data['module_name']} — Changelog / Drift Ledger",
        "",
        "> Auto-generated by `pcp docs`. Merges build completions "
        "(`.pcp/telemetry.jsonl`), `spec.yaml`/`acceptance.yaml` git history, "
        "attributed gate bypasses (`.pcp/bypass_log.yaml`), and distilled decisions "
        "(`.pcp/decision_log.jsonl`) into one chronological timeline. A `spec.yaml "
        "changed` entry landing BETWEEN two `built` entries is a real drift signal "
        "— the module's declared intent moved while it was mid-build. Bypasses "
        "logged before module-attribution existed have no `modules` field and are "
        "excluded here — check `.pcp/bypass_log.yaml` directly for those.",
        "",
    ]

    drift = data.get("drift", {})
    lines += [
        f"## Drift Score: {drift.get('score', 0):.2f}",
        "",
        "> Computed, not just visually adjacent: weighted from in-flight spec/"
        "acceptance changes (0.5 each), attributed bypasses (0.2 each), and "
        "criterion retries (0.1 each). Phase 1 — weights are deliberately "
        "simple, not tuned against real outcome data yet.",
        "",
        f"- In-flight spec/acceptance changes: {len(drift.get('in_flight_changes', []))}",
        f"- Attributed bypasses: {drift.get('bypass_count', 0)}",
        f"- Criterion retries: {drift.get('retry_count', 0)}",
        "",
    ]
    if drift.get("in_flight_changes"):
        lines.append("**In-flight changes** (spec moved while this module was still mid-build):")
        for e in drift["in_flight_changes"]:
            lines.append(f"- `{e.get('timestamp', '?')}` `{e.get('commit', '')}` {e.get('subject', '')}")
        lines.append("")

    if not data["timeline"]:
        lines.append("_No recorded activity for this module yet._")
        return "\n".join(lines) + "\n"

    for e in data["timeline"]:
        ts = e.get("timestamp") or "?"
        kind = KIND_LABEL.get(e["kind"], e["kind"])
        if e["kind"] == "build":
            files = ", ".join(e["files"][:5]) + (" …" if len(e["files"]) > 5 else "")
            lines.append(f"- `{ts}` {kind} **{e['criterion_id']}** — +{e['lines_added']}/-{e['lines_removed']} lines ({files})")
        elif e["kind"] in ("spec_change", "acceptance_change"):
            lines.append(f"- `{ts}` {kind} — `{e['commit']}` {e['subject']}")
        elif e["kind"] == "decision":
            category = e.get("category") or "uncategorized"
            lines.append(f"- `{ts}` {kind} ({category}) — {e['summary']}")
        elif e["kind"] == "bypass":
            files = ", ".join(e["files"][:5]) + (" …" if len(e["files"]) > 5 else "")
            lines.append(f"- `{ts}` {kind} — {e['reason']} ({files})")
    return "\n".join(lines) + "\n"


def _render_ui_ux(data: dict) -> str:
    """Per-module UI/UX rollup -- forced into existence (always written,
    even when empty) for any module with UI-facing criteria, rather than an
    optional file a human has to remember to generate separately. Same
    source data pcp design-audit already computes project-wide
    (_classify_rung), just scoped to this one module for a quick local
    reference instead of hunting through the project-wide rollup."""
    ui_criteria = data["ui_criteria"]
    lines = [
        f"# {data['module_name']} — UI/UX",
        "",
        "> Auto-generated by `pcp docs`. Rolls up this module's UI-facing criteria: "
        "screen archetype(s), building-block organisms, design_justification, nav "
        "depth, and customization. Never edit manually -- edit the criterion's own "
        "fields in acceptance.yaml instead.",
        "",
    ]
    if not ui_criteria:
        lines.append("_No UI-facing criteria in this module._")
        return "\n".join(lines) + "\n"

    undeclared = [c for c in ui_criteria if not c.get("design_justification")]
    if undeclared:
        lines += [
            f"**⚠ {len(undeclared)}/{len(ui_criteria)} UI-facing criteria have no "
            "`design_justification` declared** -- Built, Hidden rung per "
            "`pcp design-audit`'s Feature Exposure Ladder: "
            + ", ".join(c["id"] for c in undeclared),
            "",
        ]

    lines += ["| Criterion | Archetypes | Organisms | Rung | JTBD | Nav Depth | Customizable |",
              "|---|---|---|---|---|---|---|"]
    for c in ui_criteria:
        dj = c.get("design_justification") or {}
        rung = _classify_rung(c)
        archetypes = ", ".join(c.get("screen_archetypes") or []) or "—"
        organisms = ", ".join(c.get("ui_organisms") or []) or "—"
        nav_depth = c.get("nav_depth") if c.get("nav_depth") is not None else "—"
        customizable = "✓" if dj.get("customizable") else "—"
        lines.append(
            f"| {c['id']}: {c['description']} | {archetypes} | {organisms} | "
            f"{rung} ({RUNG_LABEL[rung]}) | {dj.get('jtbd_framing') or '—'} | "
            f"{nav_depth} | {customizable} |"
        )
    return "\n".join(lines) + "\n"


def _render_module_diff(data: dict) -> str:
    """Per-module diff: this module's active BRD items vs. what's actually
    built, and the pending criteria gap. Deliberately does NOT claim to know
    a BRD item was satisfied by a specific criterion -- brd_items.yaml has
    no field linking the two, so "likely addressed by" is a best-effort
    keyword-overlap hint (same honesty posture as brd.md's own attribution),
    not a verified fact. The pending-criteria section, by contrast, is fully
    deterministic -- straight from acceptance.yaml's own status field."""
    lines = [
        f"# {data['module_name']} — Diff (BRD vs Built)",
        "",
        "> Auto-generated by `pcp docs`. Compares this module's active BRD items "
        "against completed acceptance criteria (best-effort keyword overlap -- "
        "brd_items.yaml has no field linking a BRD item to the criterion that "
        "addressed it, so this is a starting point for human review, not a "
        "verified match) and lists pending criteria (deterministic).",
        "",
        "## Active BRD Items",
        "",
    ]
    if not data["brd_diff"]:
        lines.append("_No active BRD items keyword-matched to this module._")
    else:
        for entry in data["brd_diff"]:
            item = entry["item"]
            matches = entry["likely_addressed_by"]
            status = f"likely addressed by: {', '.join(matches)}" if matches else "⚠ no completed criterion keyword-matches this item"
            lines.append(f"- **{item.get('id')}**: {item.get('description')} — _{status}_")

    lines += ["", "## Pending Acceptance Criteria", ""]
    pending = [c for c in data["criteria"] if c.get("status") != "complete"]
    if not pending:
        lines.append("_None -- all acceptance criteria in this module are complete._")
    else:
        for c in pending:
            lines.append(f"- **{c['id']}** ({c.get('status', 'pending')}): {c['description']}")
    return "\n".join(lines) + "\n"


def write_module_docs(pcp_dir: Path, module_dir: Path) -> Path:
    data = build_module_docs(pcp_dir, module_dir)
    docs_dir = module_dir / "docs"
    docs_dir.mkdir(exist_ok=True)
    (docs_dir / "vision.md").write_text(_render_vision(data))
    (docs_dir / "brd.md").write_text(_render_brd(data))
    (docs_dir / "built.md").write_text(_render_built(data))
    (docs_dir / "changelog.md").write_text(_render_changelog(data))
    (docs_dir / "ui_ux.md").write_text(_render_ui_ux(data))
    (docs_dir / "diff.md").write_text(_render_module_diff(data))
    return docs_dir


@click.command()
@click.option("--module", "module_name", default=None, help="Generate docs for one module only.")
@click.option("--path", "project_path", type=click.Path(), default=None)
def docs(module_name: str | None, project_path: str | None):
    """Per-module doc kit: vision, BRD slice, current-built list, changelog/drift ledger."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    module_dirs = _module_dirs(pcp_dir, module_name)
    if not module_dirs:
        console.print(f"[yellow]No module(s) found{f' matching {module_name}' if module_name else ''}.[/yellow]")
        sys.exit(0)

    for module_dir in module_dirs:
        out_dir = write_module_docs(pcp_dir, module_dir)
        console.print(f"[green]✓[/green] {module_dir.name} -> {out_dir.relative_to(pcp_dir.parent)}/")
