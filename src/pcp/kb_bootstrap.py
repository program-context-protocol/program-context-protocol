"""kb_bootstrap.py — New-project progressive-build hook (kb module, A017).

Lets the kb module's components 1-4 populate incrementally as `pcp build`
creates/modifies source files, instead of waiting for a big-bang kb pass at
the end of a project. Component 2 (topic finalization) is the deliberate
exception -- it derives topics.yaml from whole-project aggregates
(objective/target_state scope, build_vs_buy candidates across every module,
dependency manifests, per-criterion logic_tier rung 2/3 flags), so re-
deriving it per file during build buys nothing and re-does the same work
over and over; it is finalized once, at `pcp kickoff` time, after the spec
files it reads from actually exist. See kb module spec constraint: "no
fresh research call inside topic finalization itself" -- the same spirit
applies to *when* it runs, not just *how*.

Components:
  1. File-metadata cards (kb_file_metadata.py, A001) -- runs progressively,
     every call. Deterministic, sha-gated (unchanged files are a no-op).
  2. topics.yaml (kb_topics.py, A004) -- runs ONCE, at kickoff, via
     run_topic_finalization(), never via run_progressive_bootstrap().
  3. Catalog + index builders -- not yet built (a later criterion). Wired
     in defensively below: discovered by module/attribute at call time
     rather than a top-level `from ... import ...`, so this file loads and
     runs cleanly today and picks the real component up automatically the
     moment its module lands -- no edit to this file needed when it ships.
  4. Two-phase gap/ingestion engine -- not yet built either. Same posture
     as component 3.

Zero LLM calls (logic_tier 1) -- this is pure orchestration/dispatch over
already-deterministic component functions; it never itself decides
anything, it just calls what exists and reports what doesn't.
"""

import importlib
from pathlib import Path

# Each entry: (result_key, module_name, function_name). The function is
# expected to take (project_root, pcp_dir) and return a JSON-safe summary
# dict, the same calling convention kb_file_metadata.generate_file_metadata_cards
# already uses -- components 3/4 should follow it once built, for the same
# reason kb_file_metadata and this dispatcher agree on it now.
_PROGRESSIVE_COMPONENTS = [
    ("component_1_file_metadata", "pcp.kb_file_metadata", "generate_file_metadata_cards"),
    ("component_3_catalog_index", "pcp.kb_catalog", "build_catalog_and_index"),
    ("component_4_gap_ingestion", "pcp.kb_ingestion", "run_progressive_ingestion_step"),
]


def _load_component(module_name: str, func_name: str):
    """Best-effort dynamic import -- returns None (never raises) when a
    component's module doesn't exist yet, or doesn't define the expected
    function. This is what makes run_progressive_bootstrap forward-
    compatible with components 3/4 landing later without a code change
    here: the moment `import module_name` succeeds and `func_name` is a
    real attribute, this dispatcher starts calling it for real."""
    try:
        mod = importlib.import_module(module_name)
    except ImportError:
        return None
    return getattr(mod, func_name, None)


def run_progressive_bootstrap(project_root: Path, pcp_dir: Path) -> dict:
    """Runs every AVAILABLE progressive component (1, 3, 4) against the
    project's current file state. Intended to be called after each `pcp
    build` criterion completes and its files are committed -- so kb
    content is always current with what actually exists on disk, never
    stale until some later big-bang catch-up pass.

    Deliberately does NOT call topic finalization (component 2) -- see
    module docstring and run_topic_finalization() below.

    Never raises: a missing component is recorded as `not yet built`, and
    a component that raises has its error captured instead of propagated.
    This is advisory grounding infrastructure -- it must never itself
    block a build, the same posture `pcp capture`'s self-capture preflight
    and `pcp docs`'s per-wave refresh already take inside `pcp build`.
    """
    results: dict[str, dict] = {}
    for key, module_name, func_name in _PROGRESSIVE_COMPONENTS:
        fn = _load_component(module_name, func_name)
        if fn is None:
            results[key] = {"ran": False, "reason": "not yet built"}
            continue
        try:
            summary = fn(project_root, pcp_dir)
            results[key] = {"ran": True, "summary": summary}
        except Exception as exc:  # advisory only -- never blocks the build
            results[key] = {"ran": False, "reason": f"error: {exc}"}
    return results


def run_topic_finalization(project_root: Path, pcp_dir: Path) -> dict:
    """Component 2 -- topics.yaml. Deliberately separate from
    run_progressive_bootstrap: topic finalization derives from whole-
    project aggregates (objective/target_state scope, build_vs_buy
    candidates, dependency manifests, logic_tier flags), so it is
    finalized once, at `pcp kickoff` time -- after objective.md/
    target_state.md/module specs actually exist -- rather than re-run on
    every file `pcp build` creates. Never raises, for the same reason as
    run_progressive_bootstrap above: this is advisory grounding
    infrastructure, not a build gate.
    """
    from pcp.commands.kb_topics import write_kb_topics

    try:
        out_path = write_kb_topics(pcp_dir, project_root)
        return {"ran": True, "path": str(out_path)}
    except Exception as exc:  # advisory only -- never blocks kickoff
        return {"ran": False, "reason": f"error: {exc}"}
