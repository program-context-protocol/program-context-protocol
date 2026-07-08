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

console = Console()

GENERATE_SPEC_PROMPT = """\
You are a software architect generating draft module specs for an existing codebase.

Given:
- A PM description of what the project does
- A detected cluster of files (natural module boundary from import analysis)
- The cluster name and its files

Generate a concise spec.yaml for this module.

Output ONLY valid YAML, no prose, no code fences. Format:
module: <cluster_name>
description: "<one sentence: what this module does>"
_generated: true
dependencies: [<other_module_names_this_cluster_imports_from>]
constraints:
  - "<key constraint visible from the code>"
"""


def _generate_module_spec(
    cluster_name: str,
    files: list[str],
    cross_edges: dict[tuple[str, str], int],
    pm_description: str,
    pcp_dir: Path | None = None,
) -> dict:
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
        raw = llm.call(GENERATE_SPEC_PROMPT, user_prompt, pcp_dir=pcp_dir, command="import-generate-spec")
        spec = yaml.safe_load(raw)
        if not isinstance(spec, dict):
            raise ValueError("not a dict")
        spec["module"] = cluster_name
        spec["_generated"] = True
        return spec
    except Exception:
        return {
            "module": cluster_name,
            "description": f"Auto-detected cluster ({len(files)} files)",
            "_generated": True,
            "dependencies": deps,
            "constraints": [],
        }


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

    # Module spec files
    for cluster_name, cluster_files in clusters.items():
        module_dir = pcp_dir / "strategy" / "modules" / cluster_name
        module_dir.mkdir(exist_ok=True)

        if skip_specs:
            spec = {
                "module": cluster_name,
                "description": f"(add description — {len(cluster_files)} files)",
                "_generated": True,
                "dependencies": [],
                "constraints": [],
            }
        else:
            spec = _generate_module_spec(cluster_name, cluster_files, cross_edges, description, pcp_dir=pcp_dir)

        (module_dir / "spec.yaml").write_text(
            "# DRAFT — review and remove _generated: true when correct\n"
            + yaml.dump(spec, default_flow_style=False, sort_keys=False)
        )

        # Empty acceptance.yaml
        acceptance = {
            "module": cluster_name,
            "version": "1",
            "criteria": [
                {
                    "id": "BF_001",
                    "description": f"Write characterization tests for {cluster_name} (golden master — no code changes)",
                    "check": "test_passes",
                    "test": f"tests/characterization/test_{cluster_name.replace('-', '_')}_baseline.py",
                    "status": "pending",
                }
            ],
        }
        # Add decouple criterion if cross-cluster violations exist
        outbound = [(b, count) for (a, b), count in cross_edges.items() if a == cluster_name]
        if outbound:
            for i, (target, count) in enumerate(outbound[:3], 2):
                acceptance["criteria"].append({
                    "id": f"BF_00{i}",
                    "description": f"Remove direct {cluster_name}→{target} coupling ({count} imports) — route through interface",
                    "check": "test_passes",
                    "test": f"tests/modularity/test_drop_{cluster_name.replace('-', '_')}.sh",
                    "status": "pending",
                })

        (module_dir / "acceptance.yaml").write_text(
            "# DRAFT — add PM-validated acceptance criteria here\n"
            + yaml.dump(acceptance, default_flow_style=False, sort_keys=False)
        )

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
