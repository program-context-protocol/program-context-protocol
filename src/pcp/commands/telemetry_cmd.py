"""pcp telemetry — summarize .pcp/telemetry.jsonl for analysis.

Closes the loop on "captured to analyse and learn": telemetry.jsonl has the raw
per-attempt/per-qa-check records, this command rolls them up so the data is
actually useful without hand-writing a notebook every time.
"""

import json
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from pcp.pcp_dir import find_pcp_dir, NoPCPDir
from pcp import telemetry as telemetry_lib

console = Console()


@click.command(name="telemetry")
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root (default: cwd, walks up to find .pcp/).")
@click.option("--json", "output_json", is_flag=True, help="Print raw per-module aggregates as JSON.")
def telemetry_cmd(project_path: str | None, output_json: bool):
    """Summarize .pcp/telemetry.jsonl — per-module retries, QA error rate, languages, cost."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    records = telemetry_lib.load(pcp_dir)
    if not records:
        console.print("[dim]No .pcp/telemetry.jsonl found — run `pcp build` first.[/dim]")
        sys.exit(0)

    agg = telemetry_lib.aggregate(records)
    by_module = agg["by_module"]

    if output_json:
        click.echo(json.dumps({
            k: {**v, "criteria": sorted(filter(None, v["criteria"])), "languages": sorted(v["languages"])}
            for k, v in by_module.items()
        }, indent=2))
        return

    table = Table(title="PCP Build Telemetry — per module")
    table.add_column("Module")
    table.add_column("Criteria")
    table.add_column("Attempts")
    table.add_column("Avg attempts/criterion")
    table.add_column("QA error rate")
    table.add_column("Tokens in/cache/out")
    table.add_column("Cost")
    table.add_column("Languages")

    total_cost = 0.0
    for m_name, v in sorted(by_module.items()):
        n_crit = len(v["criteria"]) or 1
        avg_attempts = v["attempts"] / n_crit
        qa_rate = f"{v['qa_blocks']}/{v['qa_total']}" if v["qa_total"] else "—"
        table.add_row(
            m_name, str(len(v["criteria"])), str(v["attempts"]), f"{avg_attempts:.1f}",
            qa_rate, f"{v['tokens_in']:,}/{v['tokens_cache_read']:,}/{v['tokens_out']:,}",
            f"${v['cost']:.2f}", ", ".join(sorted(v["languages"])) or "—",
        )
        total_cost += v["cost"]

    console.print(table)

    # Worktree merge conflict rate — comparable against AgenticFlict's
    # (arXiv:2604.03551) 27.67% agent-authored-PR conflict baseline.
    merges = [r for r in records if r.get("check") == "worktree-merge"]
    if merges:
        conflicts = sum(1 for r in merges if r.get("result") == "block")
        console.print(
            f"[dim]Worktree merges: {len(merges)}, conflicts: {conflicts} "
            f"({conflicts / len(merges):.0%}) — agentic-PR literature baseline 27.67% (AgenticFlict)[/dim]"
        )

    console.print(
        f"\n[dim]{len(records)} total records — {len(agg['build_records'])} build, "
        f"{len(agg['qa_records'])} qa — total cost ~${total_cost:.2f}[/dim]"
    )
    console.print("[dim]Raw data: .pcp/telemetry.jsonl (one JSON object per line — load into pandas/duckdb for deeper analysis)[/dim]")
