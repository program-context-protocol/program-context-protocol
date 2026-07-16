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
against build history. Honest limitation: bypass_log.yaml has no file/module
attribution today, so bypasses can't be placed on this timeline yet —
flagged in the rendered doc, not silently omitted.
"""

import re
import subprocess
import sys
from pathlib import Path

import click
import yaml
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, get_modules_dir, NoPCPDir
from pcp.schema.validator import load_yaml
from pcp import telemetry as telemetry_mod
from pcp import decision_log as decision_log_mod

console = Console()

KIND_LABEL = {
    "build": "🔨 built",
    "spec_change": "📋 spec.yaml changed",
    "acceptance_change": "📋 acceptance.yaml changed",
    "decision": "💡 decision",
}


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
        entries.append({"commit": parts[0][:8], "timestamp": parts[1], "subject": parts[2]})
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
    matched_brd = [i for i in all_brd_items if _keyword_match(i.get("description", ""), keywords)]

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
    timeline.sort(key=lambda e: e.get("timestamp") or "")

    return {
        "module_name": module_name, "spec": spec, "criteria": criteria,
        "matched_brd": matched_brd, "timeline": timeline,
    }


def _render_vision(data: dict) -> str:
    spec = data["spec"]
    lines = [
        f"# {data['module_name']} — Vision",
        "",
        "> Auto-generated by `pcp docs` from `spec.yaml` + this module's declared "
        "objective coverage. Do not edit manually — edit `spec.yaml` (human-written, "
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
        "(`.pcp/telemetry.jsonl`), `spec.yaml`/`acceptance.yaml` git history, and "
        "distilled decisions (`.pcp/decision_log.jsonl`) into one chronological "
        "timeline. A `spec.yaml changed` entry landing BETWEEN two `built` entries "
        "is a real drift signal — the module's declared intent moved while it was "
        "mid-build. Known gap: `bypass_log.yaml` has no file/module attribution "
        "yet, so gate bypasses can't be placed on this timeline — check "
        "`.pcp/bypass_log.yaml` directly.",
        "",
    ]
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
    return "\n".join(lines) + "\n"


def write_module_docs(pcp_dir: Path, module_dir: Path) -> Path:
    data = build_module_docs(pcp_dir, module_dir)
    docs_dir = module_dir / "docs"
    docs_dir.mkdir(exist_ok=True)
    (docs_dir / "vision.md").write_text(_render_vision(data))
    (docs_dir / "brd.md").write_text(_render_brd(data))
    (docs_dir / "built.md").write_text(_render_built(data))
    (docs_dir / "changelog.md").write_text(_render_changelog(data))
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
