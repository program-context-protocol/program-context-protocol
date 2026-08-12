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

    # Output per dollar over time. Nothing reported this, so a real ~6x
    # degradation on Project O went unseen for a week while commits/day
    # rose — the flattering metric was the only visible one.
    weeks = telemetry_lib.productivity_by_week(records)
    if len(weeks) > 1:
        # Written vs landed, side by side. Telemetry alone reads healthy — on
        # Project O it called 2026-W31 the most productive week of the run
        # (+12,342 lines, $0.018/line) while git says non-test code grew by 599.
        # The ratio between them is the signal; neither number is, alone.
        repo = telemetry_lib.repo_net_lines_by_week(pcp_dir.parent)
        landed = repo["by_week"]
        wt = Table(title="Output per dollar — by week")
        wt.add_column("Week")
        wt.add_column("Attempts", justify="right")
        wt.add_column("Spend", justify="right")
        wt.add_column("Lines written", justify="right")
        if landed:
            wt.add_column("Landed (non-test)", justify="right")
            wt.add_column("Survived", justify="right")
            wt.add_column("$/landed line", justify="right")
        else:
            wt.add_column("$/written line", justify="right")

        for w in weeks:
            row = [w["week"], str(w["attempts"]), f"${w['cost_usd']:.2f}",
                   f"{w['net_lines']:+,}"]
            if landed:
                net = landed.get(w["week"])
                if net is None:
                    row += ["—", "—", "—"]
                else:
                    row.append(f"{net:+,}")
                    written = w["net_lines"]
                    if net > written:
                        # More landed than the build loop recorded writing, so code
                        # arrived by some other path — an ad-hoc agent, a hand edit,
                        # a bulk merge. That is CTRL-037's story showing up in the
                        # numbers, and printing it as "5185% survived" would bury it.
                        row.append("[yellow]outside loop[/yellow]")
                    elif written > 0:
                        row.append(f"{net / written:.0%}")
                    else:
                        row.append("—")
                    # A week that spent money and landed nothing is the single most
                    # important row to see — never render it as a tidy $0.00.
                    row.append(f"${w['cost_usd'] / net:.2f}" if net > 0
                               else "[red]nothing landed[/red]")
            else:
                per = w["usd_per_net_line"]
                row.append(f"${per:.3f}" if per is not None
                           else ("[red]no net output[/red]" if w["cost_usd"] > 0 else "—"))
            wt.add_row(*row)
        console.print(wt)

        if landed:
            console.print(
                "[dim]`Lines written` is every attempt's diff, so it includes superseded "
                "attempts, reverts and test code. `Landed` is net authored non-test source "
                "that survived in git. `Survived` is the ratio — the number actually worth "
                "watching, since a high written count with a low survival rate is churn, "
                "not velocity. `outside loop` means more landed than the build loop recorded "
                "writing, i.e. code arrived by some other path.[/dim]"
            )
            # No silent caps: say what was excluded and why.
            skipped = repo["bulk_commits_skipped"]
            if skipped:
                detail = ", ".join(f"{w} ({n})" for w, n in sorted(skipped.items()))
                console.print(
                    f"[dim]Excluded {sum(skipped.values())} bulk commit(s) over "
                    f"{repo['bulk_threshold']:,} changed source lines — {detail}. Those are "
                    "vendor imports, bulk moves or generated dumps, not one criterion's "
                    "work; counting them made survival read 107372% on a real project.[/dim]"
                )
        else:
            console.print(
                "[dim]git unavailable — showing written lines only. These count every "
                "attempt's diff including superseded work, so treat $/written line as a "
                "trend, not an accounting figure.[/dim]"
            )

    # Issues found after a criterion's first "pass" -- a real proxy for "did
    # verification actually dig" vs "did it pass", not just pass/fail on the
    # last attempt. See telemetry.issues_after_first_green's own docstring.
    post_green = telemetry_lib.issues_after_first_green(records)
    if post_green["criteria_with_a_pass"]:
        pt = Table(title="Issues found after first green")
        pt.add_column("Module")
        pt.add_column("Criterion")
        pt.add_column("First pass at")
        pt.add_column("Issues found after", justify="right")
        pt.add_column("Checks")
        for f in post_green["flagged"]:
            pt.add_row(
                f["module"], f["criterion_id"], f["first_pass_at"] or "—",
                str(f["issues_found_after"]), ", ".join(filter(None, f["issue_checks"])) or "—",
            )
        if post_green["flagged"]:
            console.print(pt)
        console.print(
            f"[dim]{post_green['criteria_with_post_green_issues']}/{post_green['criteria_with_a_pass']} "
            "criteria that passed at least once later had a real issue found against them -- "
            "a high count here means that layer's gate marked work done too early, not that "
            "the metric is noisy.[/dim]"
        )

    console.print(
        f"\n[dim]{len(records)} total records — {len(agg['build_records'])} build, "
        f"{len(agg['qa_records'])} qa — total cost ~${total_cost:.2f}[/dim]"
    )
    console.print("[dim]Raw data: .pcp/telemetry.jsonl (one JSON object per line — load into pandas/duckdb for deeper analysis)[/dim]")
