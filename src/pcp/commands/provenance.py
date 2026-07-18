"""pcp provenance — audit-evidence document: every file, every gate PCP has
approved, any skipped/bypassed step made visible.

Pure aggregation over already-persisted logs (.pcp/controls.yaml,
.pcp/telemetry.jsonl, .pcp/bypass_log.yaml) — no LLM call, no rebuild.
Callable at any point in a project's lifecycle, cheap, always a live
snapshot. This is the SLSA/in-toto-informed "quality card" half of the
CMMC/SSDF-style build-evidence document — controls.py's control catalog is
the other half.
"""

import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, NoPCPDir
from pcp import telemetry as telemetry_mod
from pcp import decision_log as decision_log_mod
from pcp.evidence_chain import verify_chain

console = Console()

RESULT_SYMBOL = {
    "pass": "OK", "block": "BLOCK", "skipped": "skip", "error": "ERROR", "bypassed": "bypass",
}

# Fallback for telemetry records written before control_id existed (2026-07-01).
CHECK_TO_CONTROL = {
    "test-suite": "CTRL-001",
    "wave-test-suite": "CTRL-001",
    "lint": "CTRL-002",
    "sast": "CTRL-003",
    "layer1": "CTRL-004",
    "layer1-bypass": "CTRL-004",
    "architect-review": "CTRL-005",
    "wave-architect-review": "CTRL-005",
    "gate": "CTRL-006",
    "wave-contract": "CTRL-007",
    "wave-validate-strategy": "CTRL-008",
    "deploy-phase-exit": "CTRL-009",
    "build-scope": "CTRL-018",
    "tier-presence": "CTRL-019",
    "rung-necessity": "CTRL-020",
}


def _load_controls(pcp_dir: Path) -> dict:
    path = pcp_dir / "controls.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return {c["id"]: c for c in data.get("controls", [])}


def _load_bypasses(pcp_dir: Path) -> list[dict]:
    path = pcp_dir / "bypass_log.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    return data.get("bypasses", [])


def _git_head(project_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=project_root,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _control_id_for(record: dict) -> str | None:
    return record.get("control_id") or CHECK_TO_CONTROL.get(record.get("check"))


def _check_chain_integrity(pcp_dir: Path) -> dict:
    """Verifies the hash chain on every append-only evidence log. Any non-empty
    break list here means a record was edited, reordered, or deleted after
    the fact — this is the one check in this whole document that isn't just
    reporting what ran, it's reporting whether the report itself can be
    trusted."""
    telemetry_breaks = verify_chain(telemetry_mod.load(pcp_dir))
    decision_breaks = verify_chain(decision_log_mod.load(pcp_dir))
    bypass_breaks = verify_chain(_load_bypasses(pcp_dir))
    return {
        "telemetry.jsonl": telemetry_breaks,
        "decision_log.jsonl": decision_breaks,
        "bypass_log.yaml": bypass_breaks,
    }


def build_provenance(pcp_dir: Path) -> dict:
    """Pure aggregation — safe to call at any point, no side effects besides read."""
    controls = _load_controls(pcp_dir)
    # Not just cycle=="qa" -- cross-cutting controls (e.g. CTRL-011 transcript
    # archival, recorded under cycle="capture") carry a control_id too and
    # belong in the same per-file/per-control coverage view.
    qa_records = [r for r in telemetry_mod.load(pcp_dir) if _control_id_for(r)]
    bypasses = _load_bypasses(pcp_dir)
    chain_integrity = _check_chain_integrity(pcp_dir)

    per_file: dict[str, dict[str, dict]] = defaultdict(dict)
    per_control: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for r in qa_records:
        cid = _control_id_for(r)
        if not cid:
            continue
        result = r.get("result", "pass")
        per_control[cid]["total"] += 1
        per_control[cid][result] += 1
        for f in (r.get("files") or []):
            existing = per_file[f].get(cid)
            if not existing or r.get("timestamp", "") >= existing.get("timestamp", ""):
                per_file[f][cid] = r

    # A control whose every recorded outcome is "skipped" never actually ran
    # anywhere in this project — a project-wide blind spot, not per-file noise.
    standing_gap_cids = {
        cid for cid, totals in per_control.items()
        if totals["total"] > 0 and totals.get("skipped", 0) == totals["total"]
    }
    never_exercised_cids = {
        cid for cid in controls if cid not in per_control and cid != "CTRL-010"
    }

    return {
        "controls": controls,
        "per_file": {f: dict(v) for f, v in per_file.items()},
        "per_control": {cid: dict(v) for cid, v in per_control.items()},
        "standing_gap_cids": sorted(standing_gap_cids),
        "never_exercised_cids": sorted(never_exercised_cids),
        "bypasses": bypasses,
        "chain_integrity": chain_integrity,
    }


def _render_markdown(project_root: Path, data: dict, timestamp: str) -> str:
    controls = data["controls"]
    per_file = data["per_file"]
    per_control = data["per_control"]

    lines = [
        "# PCP Audit Evidence",
        f"Generated: {timestamp} | Git: `{_git_head(project_root)}`",
        "",
        "> Auto-generated by `pcp provenance`. Aggregates `.pcp/controls.yaml` + "
        "`.pcp/telemetry.jsonl` + `.pcp/bypass_log.yaml`. Do not edit manually. "
        "SSDF mapping is informed, not a certified assessment.",
        "",
        "## Per-File Gate Coverage",
        "",
    ]

    if not per_file:
        lines += ["_No telemetry recorded yet — run `pcp build` first._", ""]
    else:
        control_ids = sorted(controls.keys()) or sorted({cid for f in per_file.values() for cid in f})
        lines.append("| File | " + " | ".join(control_ids) + " |")
        lines.append("|---|" + "---|" * len(control_ids))
        for f in sorted(per_file.keys()):
            row = [f"`{f}`"]
            for cid in control_ids:
                rec = per_file[f].get(cid)
                row.append("-" if not rec else RESULT_SYMBOL.get(rec.get("result"), rec.get("result", "?")))
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")
        legend = ", ".join(f"`{cid}`={controls.get(cid, {}).get('name', cid)}" for cid in control_ids)
        lines += [f"Legend: {legend}", ""]

    lines += ["## Standing Gaps (control never actually ran project-wide)", ""]
    gap_lines = []
    for cid in data["standing_gap_cids"]:
        name = controls.get(cid, {}).get("name", cid)
        total = per_control.get(cid, {}).get("total", 0)
        gap_lines.append(f"- {cid} ({name}): {total}/{total} checks skipped — tool never detected project-wide.")
    for cid in data["never_exercised_cids"]:
        name = controls.get(cid, {}).get("name", cid)
        gap_lines.append(f"- {cid} ({name}): never invoked in this project yet.")
    lines += gap_lines if gap_lines else ["_None — every cataloged control has run with a real tool at least once._"]
    lines.append("")

    lines += ["## Bypass Ledger", ""]
    if data["bypasses"]:
        for b in data["bypasses"]:
            rules = ", ".join(b.get("rules_bypassed", []))
            lines.append(f"- `{b.get('timestamp', '')}` — {b.get('reason', '')} (rules: {rules})")
    else:
        lines.append("_No bypasses._")
    lines.append("")

    # Escalation responsiveness — proves the escalation ledger is watched,
    # not just written (PagerDuty Escalation Policy Insights pattern).
    from pcp import escalations as _esc
    _esc_dir = project_root / ".pcp"
    mtta = _esc.mtta_hours(_esc_dir)
    esc_entries = _esc.load(_esc_dir)
    lines += ["## Escalation Responsiveness", ""]
    if esc_entries:
        acked = sum(1 for e in esc_entries if e.get("acknowledged_at"))
        lines.append(f"- Escalations recorded: {len(esc_entries)}; acknowledged: {acked}")
        lines.append(f"- MTTA (median time-to-acknowledge): {mtta}h" if mtta is not None
                     else "- MTTA: no escalation has ever been acknowledged — ledger may be unwatched")
    else:
        lines.append("_No escalations recorded._")
    lines.append("")

    # Auditability Card (adapted from "Auditable Agents", arXiv:2604.05485 —
    # its 5-dimension framework maps 1:1 onto PCP's mechanisms). Deterministic
    # self-score from artifacts that exist, not aspiration.
    _pcp = project_root / ".pcp"
    card = [
        ("Action recoverability", (_pcp / "telemetry.jsonl").exists() and (_pcp / "evidence").exists(),
         "per-attempt telemetry + raw untruncated QA evidence"),
        ("Lifecycle coverage", (_pcp / "controls.yaml").exists() and (_pcp / "deploy_log.yaml").exists(),
         "controls span commit->PR->wave->deploy; deploy log present"),
        ("Policy checkability", (_pcp / "ci_rules.yaml").exists() and (_pcp / "policies").exists(),
         "machine-checkable rules (ci_rules.yaml) + human-editable OPA policies"),
        ("Responsibility attribution", (_pcp / "bypass_log.yaml").exists() or (_pcp / "escalations.yaml").exists(),
         "attributed bypasses and/or escalation ledger with ack states"),
        ("Evidence integrity", bool(data.get("chain_integrity")),
         "hash-chained logs, verified each provenance run (unsigned — see roadmap)"),
    ]
    lines += ["## Auditability Card", "",
              "Five dimensions from the 'Auditable Agents' framework (arXiv:2604.05485), "
              "scored from artifacts actually present in this project:", "",
              "| Dimension | Present | Basis |", "|---|---|---|"]
    for name, ok, basis in card:
        lines.append(f"| {name} | {'✓' if ok else '✗'} | {basis} |")
    lines.append("")

    lines += ["## Chain Integrity", "", "Each evidence log is hash-chained — an entry's hash covers its own "
              "content plus the previous entry's hash, so an edit/reorder/deletion after the fact is "
              "detectable even though the files themselves are plain, editable JSON/YAML.", ""]
    any_break = False
    for log_name, breaks in data["chain_integrity"].items():
        if not breaks:
            lines.append(f"- `{log_name}`: intact.")
        else:
            any_break = True
            lines.append(f"- `{log_name}`: **{len(breaks)} break(s) detected**")
            for b in breaks:
                lines.append(f"  - index {b['index']}: {b['issue']}")
    if any_break:
        lines.append("")
        lines.append("**A break here means this evidence document cannot be trusted as-is — "
                      "investigate before relying on anything above.**")
    lines.append("")

    lines += ["## SSDF Crosswalk", "", "| Control | SSDF Practice | Enforcement | Status |", "|---|---|---|---|"]
    for cid, c in sorted(controls.items()):
        totals = per_control.get(cid, {})
        if cid == "CTRL-010":
            status = f"{len(data['bypasses'])} bypass(es) logged" if data["bypasses"] else "0 bypasses"
        elif cid in data["never_exercised_cids"]:
            status = "GAP — never invoked"
        elif cid in data["standing_gap_cids"]:
            status = "GAP — tool never detected"
        else:
            status = f"{totals.get('pass', 0)}/{totals.get('total', 0)} pass"
        practices = ", ".join(c.get("ssdf_practice", []))
        lines.append(f"| {cid} {c.get('name', '')} | {practices} | {c.get('enforcement', '')} | {status} |")
    lines.append("")

    return "\n".join(lines)


def write_provenance(pcp_dir: Path) -> Path:
    data = build_provenance(pcp_dir)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    md = _render_markdown(pcp_dir.parent, data, timestamp)
    out = pcp_dir / "provenance.md"
    out.write_text(md)
    return out


@click.command()
@click.option("--path", "project_path", type=click.Path(), default=None)
@click.option("--json", "output_json", is_flag=True, help="Print raw JSON instead of writing provenance.md.")
@click.option("--attest", "attest_flag", is_flag=True,
              help="Also export in-toto Statement v1 / DSSE envelopes to .pcp/attestations.jsonl "
                   "(unsigned unless --sign and cosign present).")
@click.option("--sign", "sign_flag", is_flag=True,
              help="Sign the attestation export via cosign sign-blob (requires cosign; interactive OIDC).")
def provenance(project_path: str | None, output_json: bool, attest_flag: bool, sign_flag: bool):
    """Audit-evidence document — every file, every gate, any skip/bypass made visible."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    if attest_flag or sign_flag:
        from pcp.attest import export_attestations
        out, count, signing_note = export_attestations(pcp_dir, sign=sign_flag)
        console.print(f"[green]✓[/green] {count} in-toto attestation(s) -> {out.name}  [{signing_note}]")

    if output_json:
        click.echo(json.dumps(build_provenance(pcp_dir), indent=2, default=str))
        return

    out_path = write_provenance(pcp_dir)
    console.print(f"[green]Provenance written[/green] -> {out_path.relative_to(pcp_dir.parent)}")
