"""pcp run-log — pre/post audit bracket for any development or test run,
manual or automated. Ganesh, 2026-07-23: "record entry pre and post any
development run, test run or anything ... so we get a full grip of where
the bluffing is happening." `pcp build` wires start_run/end_run
automatically with real usage data; this CLI is for bracketing manual /
interactive work, which is otherwise invisible to token_ledger.yaml and
telemetry.jsonl entirely (see project memory,
project_ontology_foundry_stale_snapshot_2026_07_23 and this session's own
token_ledger gap).
"""

import json
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from pcp.pcp_dir import find_pcp_dir, NoPCPDir
from pcp import run_log

console = Console()


def _resolve_pcp_dir(project_path: str | None) -> Path:
    try:
        return find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)


@click.group("run-log")
def run_log_cli():
    """Pre/post audit bracket around any development or test run."""


@run_log_cli.command("start")
@click.option("--module", required=True, help="Module this run is working on.")
@click.option("--feature", required=True, help="Feature/criterion this run addresses.")
@click.option("--type", "run_type", type=click.Choice(["dev", "test", "build", "gate", "manual"]), default="dev")
@click.option("--actor", default="human-interactive", help="Who is actually doing the work.")
@click.option("--model", default=None, help="Model driving this run, if known up front.")
@click.option("--path", "project_path", type=click.Path(), default=None)
def start(module: str, feature: str, run_type: str, actor: str, model: str | None, project_path: str | None):
    """Open a run — writes the PRE record, prints the run_id to close it with."""
    pcp_dir = _resolve_pcp_dir(project_path)
    run_id = run_log.start_run(
        pcp_dir, module=module, feature=feature, run_type=run_type, actor=actor, model=model,
    )
    console.print(f"[green]Run started:[/green] {run_id}")
    console.print(f"[dim]Close it: pcp run-log end --run-id {run_id} --result success ...[/dim]")


@run_log_cli.command("end")
@click.option("--run-id", required=True)
@click.option("--result", type=click.Choice(["success", "failure", "partial"]), required=True)
@click.option("--model", default=None)
@click.option("--tokens-in", "token_input", type=int, default=0)
@click.option("--tokens-out", "token_output", type=int, default=0)
@click.option("--cache-read", "token_cache_read", type=int, default=0)
@click.option("--cost", "cost_usd", type=float, default=None)
@click.option("--tests-passed/--tests-failed", "tests_passed", default=None,
              help="Omit if no test suite ran this run — the ledger records that gap explicitly, it does not guess.")
@click.option("--real-gate", "real_gates_passed", multiple=True,
              help="Name a deterministic check (subprocess/AST — not an LLM opinion) that actually ran. Repeatable.")
@click.option("--llm-gate", "llm_judged_gates_passed", multiple=True,
              help="Name an LLM-judged check that passed. Repeatable.")
@click.option("--note", default=None, help="Free-text proof-of-delivery note (what was actually verified).")
@click.option("--path", "project_path", type=click.Path(), default=None)
def end(run_id: str, result: str, model: str | None, token_input: int, token_output: int,
        token_cache_read: int, cost_usd: float | None, tests_passed: bool | None,
        real_gates_passed: tuple, llm_judged_gates_passed: tuple, note: str | None, project_path: str | None):
    """Close a run — writes the POST record, prints any anomaly flags."""
    pcp_dir = _resolve_pcp_dir(project_path)
    entry = run_log.end_run(
        pcp_dir, run_id, result=result, model=model,
        token_input=token_input, token_output=token_output, token_cache_read=token_cache_read,
        cost_usd=cost_usd, tests_ran=tests_passed is not None, tests_passed=tests_passed,
        real_gates_passed=list(real_gates_passed), llm_judged_gates_passed=list(llm_judged_gates_passed),
        note=note, self_reported_usage=True,
    )
    console.print(f"[green]Run closed:[/green] {run_id} — result={result}")
    if entry["anomaly_flags"]:
        console.print("[yellow bold]Anomalies flagged:[/yellow bold]")
        for a in entry["anomaly_flags"]:
            console.print(f"  ⚠ {a}")
    else:
        console.print("[dim]No anomalies.[/dim]")


@run_log_cli.command("list")
@click.option("--path", "project_path", type=click.Path(), default=None)
@click.option("--json", "as_json", is_flag=True)
@click.option("--limit", type=int, default=30)
def list_runs(project_path: str | None, as_json: bool, limit: int):
    """Show recent runs, paired pre+post, with any anomaly flags."""
    pcp_dir = _resolve_pcp_dir(project_path)
    records = run_log.load(pcp_dir)
    pairs = run_log.pair_runs(records)
    open_ = run_log.open_runs(records)

    if as_json:
        console.print(json.dumps({"runs": pairs, "open_runs": open_}, indent=2))
        return

    table = Table(title="Run Ledger")
    for col in ["run_id", "module", "feature", "actor", "type", "result", "committed", "anomalies"]:
        table.add_column(col)
    for p in pairs[-limit:]:
        proof = p.get("proof_of_delivery", {})
        table.add_row(
            str(p.get("run_id", ""))[:8], str(p.get("module", "")), str(p.get("feature", ""))[:30],
            str(p.get("actor", "")), str(p.get("run_type", "")), str(p.get("result", "")),
            str(proof.get("committed")), ", ".join(p.get("anomaly_flags", [])) or "-",
        )
    console.print(table)
    if open_:
        console.print(f"\n[yellow]{len(open_)} open run(s) never closed:[/yellow]")
        for r in open_:
            console.print(f"  {r.get('run_id', '')[:8]} — {r.get('module')}/{r.get('feature')} started {r.get('start_time')}")
