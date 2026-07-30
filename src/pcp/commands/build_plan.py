"""pcp build-plan — deterministic execution plan, no agents spawned.

Root of tonight's redesign (2026-07-30): `pcp build`'s Python execution engine
(worktree-per-criterion, ThreadPoolExecutor, merge/retry-on-conflict) is one of
TWO independent orchestration implementations in this codebase -- the `/pcp`
skill's own SKILL.md already documents a second one, built on the Workflow
tool's `pipeline()`/`parallel()`, and CLAUDE.md describes both as if they were
the same mechanism. They aren't. The Python engine reinvents coordination the
harness already provides natively, and pays for it: measured on ontology-foundry,
5 of 5 completed `query-eval-harness` criteria hit a merge conflict in one run,
99% of that run's cost sat on the conflicted criteria, and two criteria were
still stuck mid-retry two hours in.

The concrete root cause, read directly out of the colliding file: every
criterion in a module has to add its own method to one shared facade class in
`__init__.py` and remove its own entry from a shared `_PENDING` dict -- small,
non-overlapping in meaning, but landing in the same file at the same insertion
anchor, so git's diff sees a conflict where nothing semantically conflicts.

This command is the new split: Python stays the planner (deterministic, rung 1,
reuses the exact wave-computation this project already had -- nothing here is
new logic, it's `gather_modules_to_build`/`compute_waves`/
`_compute_criterion_waves` from build.py, called and serialized rather than
immediately acted on). Python stops being the executor. Emits a plan; spawns
nothing. Execution -- actually running agents against that plan -- happens
through the Workflow tool from an orchestrating session (the `/pcp` skill),
using real harness primitives: `parallel()` for criteria that provably touch
disjoint files, `pipeline()`/a single-writer step for the ones that don't.

**Shared-surface files are the reason a per-criterion `target` isn't enough to
schedule safely.** A criterion's own new file (`metrics/plan_correctness.py`)
is genuinely disjoint from another criterion's new file -- true parallelism is
safe there. But BOTH criteria still touch the module's `__init__.py` to
register, and no criterion ever declares that as its `target` (it's an
implicit side effect of the registration convention every `pcp init --module`
scaffold uses, not something anyone writes down). So `shared_surface_files`
here is not inferred from declared targets at all -- it's the module's own
known facade files: `src/modules/<module>/__init__.py` and any file a
MOD_A00x criterion in that module declares as `target` (MOD_A002's app-registry
target, MOD_A004's interface-file target). Every criterion in the module is
marked as touching these implicitly, conservatively, whether or not its own
declared target says so -- the same asymmetry `_partition_wave_by_file_scope`
already reasons from: reopening/serializing too much costs parallelism,
missing a real collision costs a broken merge, and the second is worse.
"""

import json
import sys
from pathlib import Path

import click
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, NoPCPDir
from pcp.commands.build import gather_modules_to_build, compute_waves, _compute_criterion_waves

console = Console()


def _module_shared_surface(pcp_dir: Path, module_name: str) -> list[str]:
    """The facade files every criterion in this module implicitly touches to
    register, regardless of what each criterion's own `target` says.

    `src/modules/<module>/__init__.py` is the scaffold's own convention
    (see init.py's MODULE_ACCEPTANCE_TEMPLATE / MOD_A002-A004) -- it exists
    whether or not it's on disk yet, because the first criterion to land
    creates it. MOD_A002 (registers through the application interface) and
    MOD_A004 (interface file) declare their OWN targets explicitly; those are
    pulled in here too, since registering ANY new criterion touches both the
    facade and, if the module has one, the interface file.
    """
    mod_slug = module_name.replace("-", "_")
    surface = {f"src/modules/{mod_slug}/__init__.py"}
    acc_path = pcp_dir / "strategy" / "modules" / module_name / "acceptance.yaml"
    if acc_path.exists():
        from pcp.schema.validator import load_yaml
        try:
            data = load_yaml(acc_path) or {}
        except Exception:
            data = {}
        for c in data.get("criteria", []) or []:
            cid = str(c.get("id") or "")
            if cid.startswith("MOD_") and c.get("target"):
                surface.add(c["target"])
    return sorted(surface)


def build_plan(pcp_dir: Path, module_name: str | None = None) -> dict:
    """{modules: [{name, wave, shared_surface_files, dependencies,
    criterion_waves: [[{id, description, check, target, depends_on,
    touches_shared_surface}, ...], ...]}], total_criteria: int}.

    Pure aggregation over data build.py already computes -- no new scheduling
    logic, no LLM, nothing spawned. `criterion_waves` is a list of lists: each
    inner list is one dependency wave (from `_compute_criterion_waves`), so
    the consumer knows both "these can run together" and "in what order
    relative to each other" without recomputing anything.
    """
    modules = gather_modules_to_build(pcp_dir, module_name)
    module_waves = compute_waves(modules)

    out_modules = []
    for mod in modules:
        surface = _module_shared_surface(pcp_dir, mod["name"])
        crit_wave_of = _compute_criterion_waves(mod)
        max_wave = max(crit_wave_of.values()) if crit_wave_of else 0

        criterion_waves = []
        for w in range(max_wave + 1):
            wave_criteria = [c for c in mod["pending_criteria"] if crit_wave_of.get(c["id"], 0) == w]
            criterion_waves.append([
                {
                    "id": c["id"],
                    "description": c.get("description", ""),
                    "check": c.get("check", "manual"),
                    "target": c.get("target"),
                    "depends_on": c.get("depends_on") or [],
                    "touches_shared_surface": True,  # see _module_shared_surface docstring
                }
                for c in wave_criteria
            ])

        out_modules.append({
            "name": mod["name"],
            "wave": module_waves.get(mod["name"], 0),
            "dependencies": mod["spec"].get("dependencies") or [],
            "shared_surface_files": surface,
            "criterion_waves": criterion_waves,
        })

    out_modules.sort(key=lambda m: (m["wave"], m["name"]))
    total = sum(len(w) for m in out_modules for w in m["criterion_waves"])
    return {"modules": out_modules, "total_criteria": total}


@click.command(name="build-plan")
@click.option("--module", "module_name", default=None, help="Limit the plan to one module.")
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root override.")
def build_plan_cmd(module_name: str | None, project_path: str | None):
    """Emit the deterministic build plan as JSON. Spawns nothing.

    Consumed by the `/pcp` skill's Workflow-tool execution -- see this
    module's own docstring for why Python stopped being the executor.
    """
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    plan = build_plan(pcp_dir, module_name)
    click.echo(json.dumps(plan, indent=2))
