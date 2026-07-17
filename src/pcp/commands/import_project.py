"""pcp import — brownfield project onboarding.

Reverse-engineers .pcp/ from an existing codebase using:
  1. PM description → objective.md
  2. Stack detection + source file inventory
  3. AST-based import graph → cluster detection (natural module boundaries)
  4. Cross-cluster coupling analysis
  5. Draft .pcp/ scaffold with _generated: true markers
  6. baseline_violations.yaml from existing pcp check violations
"""

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.table import Table
from rich import print as rprint

from pcp.discovery.scanner import detect_stack, collect_source_files, detect_entry_points, read_manifest_deps
from pcp.discovery.graph import build_dependency_graph
from pcp.discovery.clusters import detect_clusters, compute_coupling_matrix
from pcp.llm import client as llm
from pcp.commands.kickoff import _normalize_spec, _normalize_acceptance

console = Console()

# Brownfield modules get the same v2.0 schema (logic_tier/build_vs_buy
# enforcement) a fresh `pcp kickoff` already produces for a new project --
# previously this command wrote the older, ungated v1 acceptance shape via
# its own terse prompt, so every post-import feature silently bypassed the
# Logic-Tier Selection framework the same way `pcp pm` did before that gap
# was closed. _normalize_spec/_normalize_acceptance (kickoff.py) are reused
# unchanged as the safety net against an LLM-invented enum value, same
# coercion posture kickoff itself relies on.
GENERATE_MODULE_PROMPT = """\
You are a software architect generating draft PCP module specs for an existing (brownfield) codebase.

Given a PM description of the whole project, a detected cluster of files (a natural module \
boundary from import-graph analysis), and that cluster's cross-module import edges, generate \
BOTH a spec.yaml and an acceptance.yaml for this module -- schema version 2.0, the same shape \
a fresh `pcp kickoff` produces for a new project.

Acceptance criteria for a brownfield module are characterization/decoupling work, not new \
features: always include exactly one BF_001 characterization-test criterion ("write \
characterization tests -- golden master, no code changes"), then one BF_00N decouple criterion \
per cross-cluster import edge given below (up to 3), each describing removing that direct \
coupling and routing through an interface instead.

Output ONLY valid JSON, no prose, no code fences. Format:
{
  "spec": {
    "module": "<cluster_name>",
    "description": "<one sentence: what this module actually does, inferred from its files>",
    "dependencies": [<other module names this cluster imports to/from, from the edges given>],
    "constraints": ["<key constraint visible from the code>"],
    "build_vs_buy": {"decision": "not_applicable", "rationale": "brownfield characterization module, no whole-module tool-adoption choice here", "candidates_considered": []}
  },
  "acceptance": {
    "criteria": [
      {
        "id": "BF_001",
        "description": "Write characterization tests for <cluster_name> (golden master -- no code changes)",
        "check": "test_passes",
        "status": "pending",
        "logic_tier": 1,
        "build_vs_buy": {"decision": "build_fresh", "rationale": "one-sentence rationale", "candidates_considered": []}
      }
    ]
  }
}
"""


def _default_module_shape(cluster_name: str, files: list[str], deps: list[str]) -> tuple[dict, dict]:
    """Deterministic fallback used both when the LLM call fails and for
    --skip-specs -- same v2.0 shape either way, so a project's module set
    is never a mix of gated and ungated modules depending on which path
    happened to generate which one."""
    spec = {
        "module": cluster_name,
        "description": f"Auto-detected cluster ({len(files)} files) -- description not yet reviewed",
        "_generated": True,
        "dependencies": deps,
        "constraints": [],
    }
    acceptance = {
        "criteria": [
            {
                "id": "BF_001",
                "description": f"Write characterization tests for {cluster_name} (golden master — no code changes)",
                "check": "test_passes",
                "status": "pending",
                "logic_tier": 1,
                "build_vs_buy": {
                    "decision": "build_fresh",
                    "rationale": "Characterization tests are project-specific by definition -- nothing to reuse.",
                    "candidates_considered": [],
                },
            }
        ]
    }
    for i, target in enumerate(deps[:3], 2):
        acceptance["criteria"].append({
            "id": f"BF_00{i}",
            "description": f"Remove direct {cluster_name}→{target} coupling — route through interface",
            "check": "test_passes",
            "status": "pending",
            "logic_tier": 1,
            "build_vs_buy": {
                "decision": "build_fresh",
                "rationale": "Decoupling work is project-specific by definition -- nothing to reuse.",
                "candidates_considered": [],
            },
        })
    return spec, acceptance


def _generate_module(
    cluster_name: str,
    files: list[str],
    cross_edges: dict[tuple[str, str], int],
    pm_description: str,
    pcp_dir: Path | None = None,
) -> tuple[dict, dict]:
    """Returns (spec, acceptance, coercion_warnings), spec/acceptance both
    v2.0-shaped and already normalized via kickoff.py's own coercion
    functions -- callers append coercion_warnings to their own list."""
    deps = sorted({
        b for (a, b), count in cross_edges.items()
        if a == cluster_name and count > 0
    } | {
        a for (a, b), count in cross_edges.items()
        if b == cluster_name and count > 0
    })

    user_prompt = f"""PM description: {pm_description}

Cluster name: {cluster_name}
Files in cluster ({len(files)} files):
{chr(10).join(f"  {f}" for f in files[:30])}
{"  ... (truncated)" if len(files) > 30 else ""}

Cross-cluster imports to/from: {deps or ["none detected"]}
"""
    try:
        result = llm.call_json(GENERATE_MODULE_PROMPT, user_prompt, pcp_dir=pcp_dir, command="import-generate-module")
        spec = result["spec"]
        acceptance = result["acceptance"]
        if not isinstance(spec, dict) or not isinstance(acceptance, dict):
            raise ValueError("spec/acceptance not a dict")
        spec["module"] = cluster_name
        spec["_generated"] = True
        acceptance.setdefault("criteria", [])
    except Exception:
        spec, acceptance = _default_module_shape(cluster_name, files, deps)

    spec["version"] = "2.0"
    acceptance["module"] = cluster_name
    acceptance["version"] = "2.0"
    warnings = _normalize_spec(spec, cluster_name)
    warnings += _normalize_acceptance(acceptance, cluster_name)
    return spec, acceptance, warnings


def _coupling_score(cross_edges: dict, clusters: dict) -> float:
    total_cluster_pairs = len(clusters) * (len(clusters) - 1)
    if total_cluster_pairs == 0:
        return 1.0
    coupled_pairs = len({(min(a, b), max(a, b)) for a, b in cross_edges if cross_edges[(a, b)] > 0})
    return round(1.0 - (coupled_pairs / total_cluster_pairs), 2)


@click.command(name="import")
@click.argument("description")
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root (default: cwd).")
@click.option("--dry-run", is_flag=True, help="Print discovery results, write nothing.")
@click.option("--skip-specs", is_flag=True, help="Skip LLM spec generation (faster, manual specs).")
def import_project(description: str, project_path: str | None, dry_run: bool, skip_specs: bool):
    """Onboard an existing codebase into PCP.

    DESCRIPTION: one paragraph describing what this project does.

    Runs AST-based import analysis, detects module clusters, generates
    draft .pcp/ structure for PM review.
    """
    root = Path(project_path).resolve() if project_path else Path.cwd().resolve()
    pcp_dir = root / ".pcp"

    if pcp_dir.exists() and not dry_run:
        console.print("[yellow]⚠  .pcp/ already exists.[/yellow]")
        console.print("Use [bold]pcp init[/bold] to reinitialise, or [bold]--dry-run[/bold] to preview discovery only.")
        sys.exit(1)

    console.print(f"\n[bold]PCP Import[/bold] — {root.name}")
    console.print(f"[dim]{root}[/dim]\n")

    # ── 1. Stack detection ────────────────────────────────────────────────────
    console.print("[dim]Detecting stack...[/dim]")
    stack = detect_stack(root)
    entry_points = detect_entry_points(root, stack)
    manifest_deps = read_manifest_deps(root)
    console.print(f"  Stack:        {', '.join(stack)}")
    console.print(f"  Entry points: {', '.join(entry_points) or 'none detected'}")

    # ── 2. Source file inventory ──────────────────────────────────────────────
    console.print("[dim]Collecting source files...[/dim]")
    files = collect_source_files(root, stack)
    console.print(f"  Source files: {len(files)}")
    if not files:
        console.print("[red]No source files found. Check --path or stack detection.[/red]")
        sys.exit(1)

    # ── 3. Build import graph ─────────────────────────────────────────────────
    console.print("[dim]Building import dependency graph...[/dim]")
    graph = build_dependency_graph(files, root)
    total_edges = sum(len(v) for v in graph.values())
    console.print(f"  Import edges: {total_edges}")

    # ── 4. Cluster detection ──────────────────────────────────────────────────
    console.print("[dim]Detecting module clusters...[/dim]")
    clusters = detect_clusters(graph, files, root)
    cross_edges = compute_coupling_matrix(graph, clusters)
    coupling = _coupling_score(cross_edges, clusters)

    # ── 5. Print cluster map ──────────────────────────────────────────────────
    console.print(f"\n[bold]Clusters detected:[/bold]  {len(clusters)}")
    table = Table(show_header=True, header_style="bold")
    table.add_column("Cluster", style="cyan")
    table.add_column("Files", justify="right")
    table.add_column("Cross-cluster imports", style="yellow")

    for cluster_name, cluster_files in sorted(clusters.items()):
        outbound = sum(
            count for (a, b), count in cross_edges.items() if a == cluster_name
        )
        table.add_row(cluster_name, str(len(cluster_files)), str(outbound) if outbound else "—")
    console.print(table)

    coupling_color = "green" if coupling >= 0.7 else "yellow" if coupling >= 0.5 else "red"
    console.print(f"\n[bold]Coupling score:[/bold]  [{coupling_color}]{coupling:.0%}[/{coupling_color}]  "
                  f"[dim](1.0 = fully decoupled)[/dim]")

    # Print coupling violations
    if cross_edges:
        violations = sorted(cross_edges.items(), key=lambda x: -x[1])[:10]
        console.print("\n[bold yellow]Cross-cluster dependencies (coupling violations):[/bold yellow]")
        for (a, b), count in violations:
            console.print(f"  {a} ↔ {b}: {count} import{'s' if count != 1 else ''}")

    if dry_run:
        console.print("\n[dim]--dry-run: no files written.[/dim]")
        return

    # ── 6. Write .pcp/ scaffold ───────────────────────────────────────────────
    console.print("\n[dim]Writing .pcp/ scaffold...[/dim]")
    pcp_dir.mkdir(exist_ok=True)
    (pcp_dir / "strategy").mkdir(exist_ok=True)
    (pcp_dir / "strategy" / "modules").mkdir(exist_ok=True)

    # objective.md
    (pcp_dir / "objective.md").write_text(
        f"# Objective\n\n{description}\n\n"
        f"---\n_Imported from existing codebase on "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}._\n"
    )

    # architecture.md
    dep_lines = []
    for lang, deps in manifest_deps.items():
        if deps:
            dep_lines.append(f"- **{lang}**: {', '.join(deps[:10])}")
    (pcp_dir / "architecture.md").write_text(
        f"# Architecture\n\n"
        f"## Stack\n{', '.join(stack)}\n\n"
        f"## Entry Points\n"
        + ("\n".join(f"- `{e}`" for e in entry_points) or "- (none detected)") + "\n\n"
        f"## Dependencies\n"
        + ("\n".join(dep_lines) or "- (see manifest)") + "\n\n"
        f"---\n_Auto-detected. Review and update._\n"
    )

    # decomposition.md
    cluster_lines = "\n".join(
        f"- **{name}** ({len(fs)} files) — _review and describe_"
        for name, fs in sorted(clusters.items())
    )
    (pcp_dir / "strategy" / "decomposition.md").write_text(
        f"# Module Decomposition\n\n"
        f"Auto-detected from import graph analysis. "
        f"Review cluster boundaries and update descriptions.\n\n"
        f"{cluster_lines}\n\n"
        f"---\n_Generated by `pcp import`. Human review required._\n"
    )

    # dependency_map.md
    dep_map_lines = []
    for (a, b), count in sorted(cross_edges.items(), key=lambda x: -x[1]):
        dep_map_lines.append(f"- {a} → {b}: {count} import edge(s) — **decouple in Wave 0**")
    (pcp_dir / "strategy" / "dependency_map.md").write_text(
        f"# Dependency Map\n\n"
        f"Cross-module dependencies detected by import analysis.\n"
        f"Coupling score: {coupling:.0%}\n\n"
        + ("\n".join(dep_map_lines) or "No cross-cluster dependencies detected.") + "\n\n"
        f"---\n_Generated by `pcp import`._\n"
    )

    # Module spec + acceptance files -- v2.0 schema, same logic_tier/
    # build_vs_buy enforcement `pcp kickoff` already gives a greenfield module.
    all_coercion_warnings: list[str] = []
    for cluster_name, cluster_files in clusters.items():
        module_dir = pcp_dir / "strategy" / "modules" / cluster_name
        module_dir.mkdir(exist_ok=True)

        if skip_specs:
            deps = sorted({b for (a, b), c in cross_edges.items() if a == cluster_name and c > 0}
                          | {a for (a, b), c in cross_edges.items() if b == cluster_name and c > 0})
            spec, acceptance = _default_module_shape(cluster_name, cluster_files, deps)
            spec["version"] = "2.0"
            acceptance["module"] = cluster_name
            acceptance["version"] = "2.0"
        else:
            spec, acceptance, warnings = _generate_module(
                cluster_name, cluster_files, cross_edges, description, pcp_dir=pcp_dir,
            )
            all_coercion_warnings += warnings

        (module_dir / "spec.yaml").write_text(
            "# DRAFT — review and remove _generated: true when correct\n"
            + yaml.dump(spec, default_flow_style=False, sort_keys=False)
        )
        (module_dir / "acceptance.yaml").write_text(
            "# DRAFT — add PM-validated acceptance criteria here\n"
            + yaml.dump(acceptance, default_flow_style=False, sort_keys=False)
        )

    if all_coercion_warnings:
        console.print(f"[yellow]⚠  {len(all_coercion_warnings)} generated field(s) didn't match the schema, coerced to a safe default:[/yellow]")
        for w in all_coercion_warnings:
            console.print(f"   {w}")

    # baseline_violations.yaml (empty — pcp check --baseline fills this)
    (pcp_dir / "baseline_violations.yaml").write_text(
        "# Pre-existing violations — run `pcp check --baseline` to populate.\n"
        "# These are excluded from hard gates until fixed in Wave 0.\n"
        "violations: []\n"
        "generated_at: null\n"
        "total: 0\n"
    )

    # SDLC_phase.yaml
    (pcp_dir / "SDLC_phase.yaml").write_text(
        yaml.dump({
            "phase": "brownfield-import",
            "imported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "stack": stack,
            "cluster_count": len(clusters),
            "coupling_score": coupling,
            "next_action": "Review .pcp/strategy/modules/*/spec.yaml. Remove _generated: true when correct.",
        }, default_flow_style=False)
    )

    # graphify export — cluster graph as JSON for /graphify consumption
    graphify_data = {
        "nodes": [
            {"id": name, "type": "module", "file_count": len(fs)}
            for name, fs in clusters.items()
        ],
        "edges": [
            {"source": a, "target": b, "weight": count, "type": "imports"}
            for (a, b), count in cross_edges.items()
        ],
        "metadata": {
            "project": root.name,
            "coupling_score": coupling,
            "stack": stack,
        }
    }
    (pcp_dir / "discovery_graph.json").write_text(json.dumps(graphify_data, indent=2))

    # ── 7. Summary ────────────────────────────────────────────────────────────
    console.print(f"\n[green]✓  .pcp/ scaffold written.[/green]")
    console.print(f"\n[bold]Next steps:[/bold]")
    console.print("  1. Review module specs:  [cyan].pcp/strategy/modules/*/spec.yaml[/cyan]")
    console.print("     Remove [yellow]_generated: true[/yellow] from each when description is correct.")
    console.print("  2. Run baseline scan:    [cyan]pcp check --baseline[/cyan]")
    console.print("     Catalogs pre-existing violations → [yellow].pcp/baseline_violations.yaml[/yellow]")
    console.print("  3. Run strategy check:   [cyan]pcp validate-strategy[/cyan]")
    console.print("     Coverage + coupling gaps visible immediately.")
    console.print("  4. Visualise clusters:   [cyan]/graphify .pcp/discovery_graph.json[/cyan]")
    console.print("     Pass the graph file to graphify for knowledge graph view.")
    console.print(f"\n  [dim]Coupling score {coupling:.0%} — "
                  + ("Wave 0 decouple recommended before new features." if coupling < 0.7
                     else "Coupling acceptable. Proceed to Wave 1 features.") + "[/dim]")
