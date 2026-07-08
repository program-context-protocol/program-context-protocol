"""pcp dashboard — a static, self-contained HTML status dashboard generated
from .pcp/ state: module status (grouped by dependency wave), SDLC phase/
milestone progress, build efficiency, evidence-chain integrity, and recent
drift/decision activity.

Reuses the same data pcp_status.py's pcp.md snapshot already computes —
this is a visual rendering of the same underlying state, not a second
source of truth. No server, no JS framework, no external font/asset CDN
(the whole point is one file that works offline, opened by any team member
who has the repo checked out — individual and team use alike, since a
shared team project's .pcp/ state is the same file everyone's `pcp
dashboard` run reads from after a `git pull`).

Written to <project_root>/dashboard.html — same location convention as
pcp.md (project root, never inside .pcp/, so it's easy to find and open).
"""

import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path

import click
import yaml
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, get_modules_dir, NoPCPDir
from pcp.schema.validator import load_yaml

console = Console()

STATUS_LABEL = {"complete": "Complete", "in_progress": "In Progress", "pending": "Pending"}


def _module_status(complete: int, total: int) -> str:
    if total > 0 and complete == total:
        return "complete"
    if complete > 0:
        return "in_progress"
    return "pending"


def build_dashboard_data(pcp_dir: Path) -> dict:
    """Pure aggregation — safe to call at any point, no side effects besides
    the reads `pcp_status.py`'s own helpers already do."""
    from pcp.commands.scan import _scan_module, _load_prior_manual_status
    from pcp.commands.build import compute_waves
    from pcp.commands.provenance import _check_chain_integrity
    from pcp import pcp_status as ps

    project_root = pcp_dir.parent
    modules_dir = get_modules_dir(pcp_dir)
    prior_manual = _load_prior_manual_status(pcp_dir / "current_state.md")

    acceptance_files = sorted(modules_dir.glob("*/acceptance.yaml")) if modules_dir.exists() else []
    modules_for_waves = []
    modules = []
    for af in acceptance_files:
        module_name = af.parent.name
        spec_path = af.parent / "spec.yaml"
        spec = load_yaml(spec_path) if spec_path.exists() else {}
        result = _scan_module(module_name, af, project_root, prior_manual)
        modules_for_waves.append({"name": module_name, "spec": spec})
        total = len(result["criteria"])
        complete = sum(1 for c in result["criteria"] if c["status"] == "complete")
        modules.append({
            "name": module_name,
            "dependencies": spec.get("dependencies") or [],
            "total": total, "complete": complete, "pending": total - complete,
            "status": _module_status(complete, total),
            "criteria": result["criteria"],
        })

    wave_of = compute_waves(modules_for_waves) if modules_for_waves else {}
    for m in modules:
        m["wave"] = wave_of.get(m["name"], 0)
    num_waves = max((m["wave"] for m in modules), default=-1) + 1

    total = sum(m["total"] for m in modules)
    complete = sum(m["complete"] for m in modules)
    score = complete / total if total else 0.0

    phase_name, _ = ps._extract_phase(pcp_dir)
    sdlc_path = pcp_dir / "SDLC_phase.yaml"
    phases = []
    if sdlc_path.exists():
        sdlc_data = yaml.safe_load(sdlc_path.read_text()) or {}
        for p in sdlc_data.get("phases", []):
            crits = p.get("exit_criteria", [])
            evaluated = [{**c, "_done": ps._evaluate_exit_criterion(c, project_root)} for c in crits]
            done = sum(1 for c in evaluated if c["_done"])
            phases.append({
                "name": p["name"], "is_current": p["name"] == phase_name,
                "exit_criteria": evaluated, "done": done, "total": len(evaluated),
            })

    bypass_count = ps._extract_bypass_count(pcp_dir)
    recent_bypasses = []
    bypass_log_path = pcp_dir / "bypass_log.yaml"
    if bypass_log_path.exists():
        data = yaml.safe_load(bypass_log_path.read_text()) or {}
        recent_bypasses = (data.get("bypasses") or [])[-5:]

    return {
        "project_name": project_root.name,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "phase_name": phase_name,
        "phases": phases,
        "modules": modules,
        "num_waves": num_waves,
        "total": total, "complete": complete, "score_pct": score,
        "bypass_count": bypass_count,
        "recent_bypasses": recent_bypasses,
        "test_coverage": ps._extract_test_coverage(pcp_dir),
        "token_summary": ps._extract_token_summary(pcp_dir),
        "telemetry_summary": ps._extract_telemetry_summary(pcp_dir),
        "brd_summary": ps._extract_brd_summary(pcp_dir),
        "decision_log_summary": ps._extract_decision_log_summary(pcp_dir),
        "chain_integrity": _check_chain_integrity(pcp_dir),
    }


CSS = """
:root {
  --bg: #faf8f4; --surface: #ffffff; --ink: #1c1917; --ink-dim: #6b645c;
  --border: #e5e0d8; --accent: #0f6e70; --accent-soft: #e4f1f0;
  --complete: #2e8b57; --complete-soft: #e5f3ea;
  --progress: #b8790f; --progress-soft: #fbf0dc;
  --blocked: #b8383f; --blocked-soft: #fbe9e9;
  --pending: #8a8478; --pending-soft: #f1efe9;
  --mono: ui-monospace, "SF Mono", "Cascadia Code", "Roboto Mono", Consolas, monospace;
  --sans: -apple-system, "Segoe UI", ui-sans-serif, system-ui, sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #161513; --surface: #201f1c; --ink: #ede8e0; --ink-dim: #9a9288;
    --border: #38352f; --accent: #4fc3c6; --accent-soft: #1c3536;
    --complete: #5cbf87; --complete-soft: #1c3427;
    --progress: #e0a83f; --progress-soft: #3a2e12;
    --blocked: #e2696f; --blocked-soft: #3a1e1f;
    --pending: #948d80; --pending-soft: #2a2822;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2.5rem clamp(1rem, 4vw, 3rem); background: var(--bg); color: var(--ink);
  font-family: var(--sans); line-height: 1.5; font-size: 15px;
}
h1, h2, h3 { font-weight: 650; letter-spacing: -0.01em; text-wrap: balance; }
h1 { font-size: 1.6rem; margin: 0; }
h2 { font-size: 1.05rem; margin: 0 0 0.9rem; color: var(--ink); }
.eyebrow { text-transform: uppercase; letter-spacing: 0.08em; font-size: 0.7rem; color: var(--ink-dim); font-weight: 600; }
.mono { font-family: var(--mono); }
.container { max-width: 1180px; margin: 0 auto; }
header.top {
  display: flex; justify-content: space-between; align-items: flex-end; flex-wrap: wrap; gap: 1rem;
  border-bottom: 1px solid var(--border); padding-bottom: 1.25rem; margin-bottom: 1.75rem;
}
.updated { color: var(--ink-dim); font-size: 0.82rem; }
.phase-badge {
  display: inline-block; padding: 0.2rem 0.65rem; border-radius: 999px; font-size: 0.75rem;
  font-weight: 600; background: var(--accent-soft); color: var(--accent); font-family: var(--mono);
}

.stat-strip { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 0.75rem; margin-bottom: 2rem; }
.stat-card {
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 0.9rem 1rem;
}
.stat-card .value { font-size: 1.5rem; font-weight: 700; font-family: var(--mono); }
.stat-card .label { font-size: 0.72rem; color: var(--ink-dim); text-transform: uppercase; letter-spacing: 0.05em; margin-top: 0.15rem; }

section { margin-bottom: 2.25rem; }

.milestones { display: flex; gap: 0.6rem; overflow-x: auto; padding-bottom: 0.3rem; }
.milestone {
  flex: 0 0 auto; min-width: 190px; border: 1px solid var(--border); border-radius: 10px;
  padding: 0.85rem 1rem; background: var(--surface);
}
.milestone.current { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.milestone.done { opacity: 0.75; }
.milestone .name { font-weight: 650; font-size: 0.9rem; }
.milestone .frac { font-family: var(--mono); font-size: 0.78rem; color: var(--ink-dim); margin-top: 0.2rem; }
.bar { height: 6px; border-radius: 999px; background: var(--pending-soft); overflow: hidden; margin-top: 0.5rem; }
.bar > i { display: block; height: 100%; background: var(--accent); border-radius: 999px; }

.wave-row { margin-bottom: 1.25rem; }
.wave-label { font-family: var(--mono); font-size: 0.72rem; color: var(--ink-dim); margin-bottom: 0.5rem; }
.wave-modules { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 0.75rem; }

.module-card { border: 1px solid var(--border); border-radius: 10px; background: var(--surface); overflow: hidden; }
.module-card > summary {
  list-style: none; cursor: pointer; padding: 0.85rem 1rem; display: flex; flex-direction: column; gap: 0.4rem;
}
.module-card > summary::-webkit-details-marker { display: none; }
.module-card-top { display: flex; justify-content: space-between; align-items: center; gap: 0.5rem; }
.module-name { font-weight: 650; font-size: 0.92rem; }
.pill { font-size: 0.68rem; font-weight: 700; padding: 0.15rem 0.55rem; border-radius: 999px; text-transform: uppercase; letter-spacing: 0.03em; white-space: nowrap; }
.pill.complete { background: var(--complete-soft); color: var(--complete); }
.pill.in_progress { background: var(--progress-soft); color: var(--progress); }
.pill.pending { background: var(--pending-soft); color: var(--pending); }
.module-card .bar > i.complete { background: var(--complete); }
.module-card .bar > i.in_progress { background: var(--progress); }
.module-card .bar > i.pending { background: var(--pending); }
.module-frac { font-family: var(--mono); font-size: 0.75rem; color: var(--ink-dim); }
.deps { font-size: 0.72rem; color: var(--ink-dim); }
.deps .dep { font-family: var(--mono); background: var(--pending-soft); padding: 0.05rem 0.35rem; border-radius: 5px; margin-right: 0.2rem; }
.criteria-list { list-style: none; margin: 0; padding: 0 1rem 0.9rem; border-top: 1px solid var(--border); }
.criteria-list li { display: flex; gap: 0.5rem; padding: 0.4rem 0; font-size: 0.82rem; border-bottom: 1px dashed var(--border); }
.criteria-list li:last-child { border-bottom: none; }
.crit-id { font-family: var(--mono); color: var(--ink-dim); flex: 0 0 auto; }
.crit-status { flex: 0 0 auto; }

.info-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 0.85rem; }
.info-card { border: 1px solid var(--border); border-radius: 10px; background: var(--surface); padding: 0.9rem 1rem; font-size: 0.85rem; }
.info-card h3 { font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.04em; color: var(--ink-dim); margin: 0 0 0.4rem; }

.chain-ok { color: var(--complete); font-weight: 600; }
.chain-break { color: var(--blocked); font-weight: 700; }

footer { color: var(--ink-dim); font-size: 0.78rem; border-top: 1px solid var(--border); padding-top: 1rem; margin-top: 2.5rem; }
footer a { color: var(--accent); }
"""


def _bar(pct: float, cls: str = "") -> str:
    return f'<div class="bar"><i class="{cls}" style="width:{pct * 100:.0f}%"></i></div>'


def _milestone_html(p: dict) -> str:
    pct = p["done"] / p["total"] if p["total"] else 0.0
    cls = "current" if p["is_current"] else ("done" if pct >= 1 else "")
    return f"""
  <div class="milestone {cls}">
    <div class="name">{escape(p["name"])}</div>
    <div class="frac">{p["done"]}/{p["total"]} exit criteria</div>
    {_bar(pct)}
  </div>"""


def _criterion_html(c: dict) -> str:
    status = c.get("status", "pending")
    mark = {"complete": "✓", "pending": "—"}.get(status, "—")
    return f"""
      <li><span class="crit-id">{escape(c["id"])}</span>
          <span>{escape(c["description"])}</span>
          <span class="crit-status">{mark}</span></li>"""


def _module_card_html(m: dict) -> str:
    pct = m["complete"] / m["total"] if m["total"] else 0.0
    status = m["status"]
    open_attr = " open" if status == "in_progress" else ""
    deps_html = ""
    if m["dependencies"]:
        chips = "".join(f'<span class="dep">{escape(d)}</span>' for d in m["dependencies"])
        deps_html = f'<div class="deps">depends on {chips}</div>'
    criteria_html = "".join(_criterion_html(c) for c in m["criteria"])
    return f"""
    <details class="module-card"{open_attr}>
      <summary>
        <div class="module-card-top">
          <span class="module-name mono">{escape(m["name"])}</span>
          <span class="pill {status}">{STATUS_LABEL[status]}</span>
        </div>
        <div class="module-frac">{m["complete"]}/{m["total"]} criteria</div>
        {_bar(pct, status)}
        {deps_html}
      </summary>
      <ul class="criteria-list">{criteria_html}</ul>
    </details>"""


def render_html(data: dict) -> str:
    stats = [
        ("Criteria", f'{data["complete"]}/{data["total"]}'),
        ("Coverage", f'{data["score_pct"]:.0%}'),
        ("Test Coverage", data["test_coverage"] or "—"),
        ("Bypasses", str(data["bypass_count"])),
    ]
    stat_cards = "".join(
        f'<div class="stat-card"><div class="value">{escape(v)}</div><div class="label">{escape(l)}</div></div>'
        for l, v in stats
    )

    milestones_html = "".join(_milestone_html(p) for p in data["phases"]) or \
        '<div class="milestone">No SDLC phases defined.</div>'

    waves_html = ""
    for w in range(data["num_waves"]):
        wave_modules = [m for m in data["modules"] if m["wave"] == w]
        if not wave_modules:
            continue
        cards = "".join(_module_card_html(m) for m in wave_modules)
        wave_label = f"Wave {w}" if data["num_waves"] > 1 else "Modules"
        waves_html += f'<div class="wave-row"><div class="wave-label">{wave_label}</div><div class="wave-modules">{cards}</div></div>'
    if not waves_html:
        waves_html = '<p class="mono" style="color:var(--ink-dim)">No modules found.</p>'

    ci = data["chain_integrity"]
    ci_lines = []
    for log_name, breaks in ci.items():
        if breaks:
            ci_lines.append(f'<div><span class="chain-break">✗ {escape(log_name)}: {len(breaks)} break(s)</span></div>')
        else:
            ci_lines.append(f'<div><span class="chain-ok">✓ {escape(log_name)}: intact</span></div>')

    info_cards = []
    if data["telemetry_summary"]:
        info_cards.append(("Build Efficiency", data["telemetry_summary"]))
    if data["token_summary"]:
        info_cards.append(("Token Spend", data["token_summary"]))
    if data["brd_summary"]:
        info_cards.append(("Business Requirements", data["brd_summary"]))
    if data["decision_log_summary"]:
        info_cards.append(("Technical Decisions", data["decision_log_summary"]))
    info_cards_html = "".join(
        f'<div class="info-card"><h3>{escape(t)}</h3><div>{escape(v)}</div></div>' for t, v in info_cards
    )

    bypass_html = ""
    if data["recent_bypasses"]:
        items = "".join(
            f'<li>{escape(b.get("timestamp", ""))} — {escape(b.get("reason", ""))}</li>'
            for b in data["recent_bypasses"]
        )
        bypass_html = f'<div class="info-card"><h3>Recent Bypasses</h3><ul class="mono" style="margin:0;padding-left:1.1rem;font-size:0.8rem">{items}</ul></div>'

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PCP Dashboard — {escape(data["project_name"])}</title>
<style>{CSS}</style>
</head>
<body>
<div class="container">
  <header class="top">
    <div>
      <div class="eyebrow">PCP Dashboard</div>
      <h1>{escape(data["project_name"])}</h1>
    </div>
    <div style="text-align:right">
      <span class="phase-badge">{escape(data["phase_name"])}</span>
      <div class="updated">Updated {escape(data["timestamp"])}</div>
    </div>
  </header>

  <div class="stat-strip">{stat_cards}</div>

  <section>
    <h2>Milestones</h2>
    <div class="milestones">{milestones_html}</div>
  </section>

  <section>
    <h2>Module Status</h2>
    {waves_html}
  </section>

  <section>
    <h2>Evidence Integrity</h2>
    <div class="info-card">{"".join(ci_lines)}</div>
  </section>

  {"<section><h2>Activity</h2><div class='info-grid'>" + info_cards_html + bypass_html + "</div></section>" if (info_cards or bypass_html) else ""}

  <footer>
    Generated by <code class="mono">pcp dashboard</code>. Static snapshot — re-run to refresh.
    See <code class="mono">pcp.md</code>, <code class="mono">.pcp/provenance.md</code>,
    <code class="mono">.pcp/brd.md</code> for deeper drill-down.
  </footer>
</div>
</body>
</html>
"""


def write_dashboard(pcp_dir: Path) -> Path:
    data = build_dashboard_data(pcp_dir)
    html = render_html(data)
    out = pcp_dir.parent / "dashboard.html"
    out.write_text(html)
    return out


@click.command()
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root (default: cwd, walks up to find .pcp/).")
def dashboard(project_path: str | None):
    """Generate a static HTML status dashboard — module status by wave, milestones, evidence integrity."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    out_path = write_dashboard(pcp_dir)
    console.print(f"[green]Dashboard written[/green] → {out_path.relative_to(pcp_dir.parent)}")
