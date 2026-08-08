"""pcp design-audit — PCP Design lifecycle, stage 5 (Audit/rollup).

Feature Exposure Ladder: for every UI-facing acceptance criterion in a
project, classifies how discoverable it actually is to an end user, based
on the design_justification stage 2 (Decide) already recorded (or not) on
that criterion. Pure aggregation over what's already on disk -- no LLM,
never hand-edited, same posture as architecture_justification.py, but a
different audience: this is for product/PM ("can a user find and use this"),
architecture_justification.md is for engineering ("was this the right
technical call").

Maps directly onto Google's HEART framework's Adoption pillar, computed
statically from declared intent rather than live usage telemetry (Happiness/
Engagement/Retention need real instrumentation -- a separate, deferred
build-vs-buy decision, not something inferable from a build-time audit).
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml
from rich.console import Console

from pcp import nav_graph
from pcp.pcp_dir import find_pcp_dir, get_modules_dir, NoPCPDir
from pcp.commands.build import _is_ui_facing_criterion

console = Console()

RUNG_LABEL = {
    1: "Built, Hidden",
    2: "Exposed, Undiscoverable",
    3: "Exposed, Discoverable",
    4: "Exposed, Enriched",
}

# Crude, deterministic JTBD-shape check: a real "when X, do Y" conditional
# framing vs. a bare feature-name restatement. Good enough as a rung-3→4
# gate; false negatives just mean a genuinely good framing sits one rung
# lower than it deserves, which is a safe direction to err in.
_JTBD_MARKERS = ("when ", "if ", "whenever ")


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def _classify_rung(criterion: dict, nav: dict | None = None,
                   depth_threshold: int = 3) -> int | None:
    """Rung from the MEASURED artifact where possible, `None` when unknown.

    This used to read one field: no `design_justification` -> rung 1. Measured
    2026-07-27 on Project O that gave 101 at rung 1, 24 at rung 4, and
    **zero** at rungs 2 and 3 -- a binary condition wearing a four-rung costume,
    because nothing ever writes a partial justification. "101 Built, Hidden"
    described 101 missing fields, not 101 hidden features.

    Rungs 1-3 now come from `nav_graph`: is the criterion's screen reachable
    from the app's entry page, and how deep. Only rung 4 still consults the
    declaration, because "is this framed as a real job-to-be-done" is genuinely
    a property of the writing, not of the artifact.

    `None` means not determinable -- no front end, or a criterion that cannot be
    tied to a screen. That is reported separately and is NOT rung 1. Reporting
    an absent measurement as a bad measurement is the whole defect being fixed.
    """
    target = criterion.get("target") or ""
    screen = nav_graph.screen_for_target(target, nav) if nav else None
    if screen is None:
        return None

    depths = (nav or {}).get("depths", {})
    if screen not in depths:
        return 1                      # measured: genuinely unreachable from entry
    if depths[screen] > depth_threshold:
        return 2                      # reachable, but buried

    dj = criterion.get("design_justification") or {}
    checklist = dj.get("checklist_passed") or []
    jtbd = (dj.get("jtbd_framing") or "").strip()
    if jtbd and any(m in jtbd.lower() for m in _JTBD_MARKERS) and checklist:
        return 4
    return 3


def _nav_depth_threshold() -> int:
    import os
    return int(os.environ.get("PCP_NAV_DEPTH_THRESHOLD", "3"))


def build_design_audit(pcp_dir: Path) -> dict:
    """Pure aggregation, no LLM -- safe to call at any point."""
    modules_dir = get_modules_dir(pcp_dir)
    modules = []
    rung_counts = {r: 0 for r in RUNG_LABEL}
    # Criteria whose screen cannot be identified are counted here, NOT dumped
    # into rung 1. "We could not measure this" and "this is hidden" are
    # different facts and must not share a bucket.
    undetermined = 0
    nav = nav_graph.analyse(pcp_dir.parent)
    total_ui_criteria = 0
    nav_depths: list[int] = []
    nav_depth_missing = 0
    customizable_count = 0

    non_ui_exposed = []

    if modules_dir.exists():
        for mod_path in sorted(p for p in modules_dir.iterdir() if p.is_dir()):
            acceptance = _load_yaml(mod_path / "acceptance.yaml")
            ui_criteria = []
            for c in acceptance.get("criteria", []):
                if not _is_ui_facing_criterion(c):
                    continue
                exposure = c.get("exposure") or {}
                exposure_mode = exposure.get("mode", "ui")
                if exposure_mode != "ui":
                    # Deliberately not on the Feature Exposure Ladder at all --
                    # the ladder measures UI discoverability, and this
                    # criterion declared it isn't exposed through the UI.
                    # Listed separately so it's still visible in the audit
                    # trail, never silently dropped or mis-scored as rung 1.
                    non_ui_exposed.append({
                        "module": mod_path.name,
                        "id": c.get("id"),
                        "description": c.get("description"),
                        "mode": exposure_mode,
                        "justification": exposure.get("justification", ""),
                    })
                    continue
                rung = _classify_rung(c, nav, _nav_depth_threshold())
                if rung is None:
                    undetermined += 1
                else:
                    rung_counts[rung] += 1
                total_ui_criteria += 1
                dj = c.get("design_justification") or {}
                nav_depth = c.get("nav_depth")
                if nav_depth is None:
                    nav_depth_missing += 1
                else:
                    nav_depths.append(nav_depth)
                customizable = bool(dj.get("customizable"))
                if customizable:
                    customizable_count += 1
                ui_criteria.append({
                    "id": c.get("id"),
                    "description": c.get("description"),
                    "rung": rung,
                    "jtbd_framing": dj.get("jtbd_framing", ""),
                    "deviations_from_system": dj.get("deviations_from_system", ""),
                    "nav_depth": nav_depth,
                    "customizable": customizable,
                    "customization_notes": dj.get("customization_notes", ""),
                    "screen": c.get("screen"),
                })
            if ui_criteria:
                modules.append({"module": mod_path.name, "criteria": ui_criteria})

    threshold = _nav_depth_threshold()
    within_threshold = sum(1 for d in nav_depths if d <= threshold)
    nav_depth_summary = {
        "declared": len(nav_depths),
        "missing": nav_depth_missing,
        "max": max(nav_depths) if nav_depths else None,
        "avg": round(sum(nav_depths) / len(nav_depths), 1) if nav_depths else None,
        "within_threshold_pct": round(within_threshold / len(nav_depths), 2) if nav_depths else None,
        "threshold": threshold,
    }
    customization_summary = {
        "customizable_count": customizable_count,
        "total_ui_criteria": total_ui_criteria,
        "customizable_pct": round(customizable_count / total_ui_criteria, 2) if total_ui_criteria else None,
    }

    ui_archetype = None
    conventions_path = pcp_dir / "design_conventions.yaml"
    if conventions_path.exists():
        ui_archetype = (_load_yaml(conventions_path) or {}).get("ui_archetype")

    return {
        "modules": modules,
        "rung_counts": rung_counts,
        "undetermined": undetermined,
        "nav_analysis": nav,
        "total_ui_criteria": total_ui_criteria,
        "nav_depth": nav_depth_summary,
        "customization": customization_summary,
        "ui_archetype": ui_archetype,
        "non_ui_exposed": non_ui_exposed,
        "page_inventory": _build_page_inventory(modules),
    }


def _build_page_inventory(modules: list[dict]) -> list[dict]:
    """Groups each module's UI-facing criteria by their declared `screen`
    field -- the page inventory IS this grouping, computed, not a separate
    hand-maintained roster. Real gap this closes, 2026-08-08 dogfood: a
    module with ~15 UI-facing criteria had no shared page model at all, so
    nothing could distinguish "these 4 criteria are one page" from "these
    are 4 separate pages" -- design_justification's own checks operate
    per-criterion and never saw the module as a set of pages."""
    inventory = []
    for m in modules:
        screens: dict[str, list[dict]] = {}
        ungrouped = []
        for c in m["criteria"]:
            screen = c.get("screen")
            if screen:
                screens.setdefault(screen, []).append(c)
            else:
                ungrouped.append(c)
        inventory.append({
            "module": m["module"],
            "screens": [
                {"screen": name, "criteria": crits}
                for name, crits in sorted(screens.items())
            ],
            "ungrouped": ungrouped,
        })
    return inventory


def _render_markdown(data: dict, timestamp: str) -> str:
    lines = [
        "# Design Audit — Feature Exposure Ladder",
        "",
        f"_Auto-generated by `pcp design-audit` at {timestamp}. Never hand-edit — "
        "the rationale lives in each criterion's design_justification; this is a "
        "rollup view, not a second place to author it._",
        "",
        "PCP Design lifecycle, stage 5 (Audit/rollup). Maps to Google HEART's Adoption "
        "pillar. Rungs 1-3 are MEASURED from the built UI — pages discovered from the "
        "front end's own entry config, edges from its links, depth by shortest path from "
        "the entry page. Only rung 4 consults a declaration, because whether a screen is "
        "framed as a real job-to-be-done is a property of the writing, not the artifact.",
        "",
        "## Rung Distribution",
        "",
        "| Rung | Label | Criteria |",
        "|---|---|---|",
    ]
    for rung, label in RUNG_LABEL.items():
        lines.append(f"| {rung} | {label} | {data['rung_counts'].get(rung, 0)} |")
    lines.append("")

    # "Could not measure" is its own row and never folded into rung 1. The
    # previous version classified a missing `design_justification` as
    # "Built, Hidden" and reported 101 of them on Project O -- a
    # statement about absent paperwork dressed as a discoverability crisis.
    nav = data.get("nav_analysis") or {}
    undet = data.get("undetermined", 0)
    if undet:
        lines += [
            f"**{undet} UI-facing criteria could not be placed on the ladder.** This is "
            "NOT a finding about the product — it means the criterion could not be tied "
            "to a screen, so its discoverability was never measured.",
            "",
        ]
        if nav.get("available"):
            lines += [
                f"The app itself WAS measured: `{nav.get('ui_root')}` — "
                f"{len(nav.get('pages', []))} page(s), entry `{nav.get('entry')}`, "
                f"{len(nav.get('unreachable', []))} unreachable. What is missing is the "
                "link from criterion to screen: declare `target` on a UI-facing criterion "
                "and it becomes measurable.",
                "",
            ]
        else:
            lines += [
                f"No front end could be measured either ({nav.get('reason', 'unknown')}), "
                "so nothing here is a claim about discoverability.",
                "",
            ]
    if nav.get("available") and nav.get("unreachable"):
        lines += [
            "**Unreachable pages** (exist in the build, no path from the entry page): "
            + ", ".join(f"`{p}`" for p in nav["unreachable"]),
            "",
        ]

    nd = data["nav_depth"]
    lines += ["## Navigation Depth (clicks from entry point)", ""]
    if nd["declared"]:
        lines.append(
            f"Declared on {nd['declared']} UI-facing criteria ({nd['missing']} missing) — "
            f"max {nd['max']}, avg {nd['avg']}, {nd['within_threshold_pct']:.0%} within the "
            f"{nd['threshold']}-click threshold. Self-declared, not computed from a real routing "
            "graph — see CTRL-025."
        )
    else:
        lines.append(f"_No criteria declare nav_depth yet ({nd['missing']} missing)._")
    lines.append("")

    cz = data["customization"]
    lines += ["## Feature Customization", ""]
    if cz["total_ui_criteria"]:
        lines.append(
            f"{cz['customizable_count']}/{cz['total_ui_criteria']} UI-facing criteria "
            f"({cz['customizable_pct']:.0%}) declare `customizable: true` — see CTRL-026 for the "
            "structural check on those declarations."
        )
    else:
        lines.append("_No UI-facing criteria yet._")
    lines.append("")

    non_ui = data.get("non_ui_exposed") or []
    lines += ["## Explicitly Non-UI-Exposed", ""]
    if non_ui:
        lines.append(
            f"{len(non_ui)} UI-facing-looking criteria declare `exposure.mode` != `ui` — deliberately "
            "exposed via API/internal instead of a screen, so they are excluded from the ladder above "
            "rather than mis-scored as rung 1. See CTRL-039 for the justification-substance check."
        )
        lines.append("")
        lines.append("| Criterion | Mode | Justification |")
        lines.append("|---|---|---|")
        for n in non_ui:
            lines.append(
                f"| {n['module']}/{n['id']}: {n['description']} | {n['mode']} | {n['justification'] or '_(none)_'} |"
            )
        lines.append("")
    else:
        lines.append("_No criteria declare a non-ui exposure mode._")
        lines.append("")

    lines += ["## Top Menu Bar Convention", ""]
    if data["ui_archetype"] == "desktop_app":
        lines.append("`ui_archetype: desktop_app` — CTRL-027 checks for File/Edit/View/Help-style "
                      "menus; see provenance.md / telemetry for the latest result.")
    else:
        lines.append(f"`ui_archetype: {data['ui_archetype'] or 'web_app (default)'}` — "
                      "menu-bar convention check inert (desktop_app only).")
    lines.append("")

    lines += ["## Page Inventory", "",
              "Computed by grouping UI-facing criteria on their declared `screen` field — "
              "not a separate hand-maintained roster. A criterion with no `screen` declared "
              "is listed as ungrouped, not silently dropped.", ""]
    page_inventory = data.get("page_inventory") or []
    any_pages = any(pi["screens"] for pi in page_inventory)
    if not any_pages and not any(pi["ungrouped"] for pi in page_inventory):
        lines.append("_No UI-facing criteria yet._")
        lines.append("")
    else:
        for pi in page_inventory:
            if not pi["screens"] and not pi["ungrouped"]:
                continue
            lines.append(f"**`{pi['module']}`**")
            lines.append("")
            lines.append("| Page | Criteria |")
            lines.append("|---|---|")
            for s in pi["screens"]:
                ids = ", ".join(c["id"] for c in s["criteria"])
                lines.append(f"| {s['screen']} | {ids} |")
            if pi["ungrouped"]:
                ids = ", ".join(c["id"] for c in pi["ungrouped"])
                lines.append(
                    f"| _(ungrouped — no `screen` declared)_ | {ids} |"
                )
            lines.append("")

    if not data["modules"]:
        lines.append("_No UI-facing criteria found yet._")
        return "\n".join(lines)

    for m in data["modules"]:
        lines.append(f"## Module: `{m['module']}`")
        lines.append("")
        lines.append("| Criterion | Rung | JTBD Framing | Nav Depth | Customizable |")
        lines.append("|---|---|---|---|---|")
        for c in m["criteria"]:
            # rung None = the criterion's screen could not be identified, so
            # discoverability was never measured. It gets its own cell rather
            # than borrowing rung 1's "Built, Hidden" label and its ⚠.
            rung = c["rung"]
            flag = " ⚠" if rung == 1 else ""
            rung_cell = f"{rung} ({RUNG_LABEL[rung]})" if rung is not None else "— (not measured)"
            nav_depth_cell = c["nav_depth"] if c["nav_depth"] is not None else "—"
            customizable_cell = "✓" if c["customizable"] else "—"
            lines.append(
                f"| {c['id']}: {c['description']}{flag} | {rung_cell} | "
                f"{c['jtbd_framing'] or '—'} | {nav_depth_cell} | {customizable_cell} |"
            )
        lines.append("")

    return "\n".join(lines)


def write_design_audit(pcp_dir: Path) -> Path:
    data = build_design_audit(pcp_dir)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    md = _render_markdown(data, timestamp)
    out = pcp_dir / "design_audit.md"
    out.write_text(md)
    return out


@click.command(name="design-audit")
@click.option("--path", "project_path", type=click.Path(), default=None)
@click.option("--json", "output_json", is_flag=True, help="Print raw JSON instead of writing design_audit.md.")
def design_audit(project_path: str | None, output_json: bool):
    """Feature Exposure Ladder — roll up how discoverable UI-facing criteria actually are."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    if output_json:
        click.echo(json.dumps(build_design_audit(pcp_dir), indent=2, default=str))
        return

    out_path = write_design_audit(pcp_dir)
    console.print(f"[green]Design audit written[/green] -> {out_path.relative_to(pcp_dir.parent)}")
