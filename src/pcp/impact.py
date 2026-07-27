"""Impacted-module test selection.

2026-07-21 finding: qa.py's test-suite gate ran the FULL project-wide suite
on every build attempt (up to 3x/criterion), deliberately never scoped --
real cost as a project's suite grows, and the thing that turned a real
2026-07-21 ontology-foundry incident (a squatted DB port) into an
undiagnosable "timed out" for hours. Ganesh's direction: full-suite runs
should be rare, and scoping should reuse PCP's own module dependency graph
(coupling.py's build_dependency_graph, the same graph validate-strategy
already trusts for coupling_score) rather than a bespoke file-level AST
import analyzer -- one graph, one source of truth, not two competing ones.

Two-stage, both deterministic, no ML:
1. Module-level blast radius: which module(s) does the changed file belong
   to (via each module's acceptance.yaml `target` fields), then every
   module that transitively DEPENDS ON those modules (nx.ancestors on the
   same graph coupling.py builds -- edge A->B means "A depends on B", so a
   change to B can only break A's callers if A is broken, not the reverse;
   ancestors of B are exactly "everything that could be affected if B
   changes").
2. File-level: within the blast-radius modules, resolve their declared
   `target` files to candidate test files via common naming conventions
   (test_<stem>.py, near the target or under tests/). This layer is
   inherently a heuristic -- PCP doesn't mandate a test-file-location
   convention -- so it degrades safely: any step that can't confidently
   narrow returns None, and the caller MUST fall back to the full suite
   rather than silently running zero tests. Never a hard requirement.
"""

from pathlib import Path

import networkx as nx

from pcp.coupling import build_dependency_graph
from pcp.schema.validator import load_yaml


def _load_modules_for_impact(pcp_dir: Path) -> dict[str, dict]:
    modules_dir = pcp_dir / "strategy" / "modules"
    modules: dict[str, dict] = {}
    if not modules_dir.exists():
        return modules
    for spec_path in sorted(modules_dir.glob("*/spec.yaml")):
        spec = load_yaml(spec_path) or {}
        if not spec.get("deprecated"):
            modules[spec_path.parent.name] = spec
    return modules


def _module_target_files(pcp_dir: Path, module_name: str) -> set[str]:
    """Files this module's acceptance criteria declare as `target`."""
    acc_path = pcp_dir / "strategy" / "modules" / module_name / "acceptance.yaml"
    if not acc_path.exists():
        return set()
    data = load_yaml(acc_path) or {}
    return {c["target"] for c in data.get("criteria", []) if c.get("target")}


def _name_variants(module_name: str) -> set[str]:
    """`web-server` also appears on disk as `web_server`."""
    return {module_name, module_name.replace("-", "_")}


def _owns_by_path_convention(module_name: str, changed_files: list[str]) -> bool:
    """Does a changed file sit under a directory named for this module?

    Attribution used to rely solely on criteria declaring `target`. Measured
    2026-07-27 on ontology-foundry: only 51 of 382 criteria declare one, so
    attribution returned an empty set for real changed files, scoping resolved
    to None, and the gate fell back to the full 1,098-test suite on EVERY
    criterion attempt -- roughly 7m46s each, about 13 hours of pure test time
    across the remaining work, re-running the same tests every time.

    Path convention covers what declarations do not: 18 of that project's 27
    modules have a matching directory. Deterministic and documented, not a
    guess -- a path SEGMENT must equal the module name (or its underscored
    form), so `web-server` matches `src/app/web_server/routes.py` and never
    matches `src/web_server_helpers.py`.
    """
    variants = _name_variants(module_name)
    for f in changed_files:
        if variants & set(Path(f).parts):
            return True
    return False


def changed_files_to_modules(pcp_dir: Path, modules: dict[str, dict], changed_files: list[str]) -> set[str]:
    """Which module(s) own at least one of changed_files.

    Two signals, both deterministic: a criterion's declared `target`, and the
    directory convention above. Declared targets alone left this inert on real
    projects (see _owns_by_path_convention)."""
    changed = set(changed_files)
    return {
        name for name in modules
        if (_module_target_files(pcp_dir, name) & changed)
        or _owns_by_path_convention(name, changed_files)
    }


def blast_radius_modules(modules: dict[str, dict], changed_module_names: set[str]) -> set[str]:
    """changed_module_names plus every module that transitively depends on
    any of them -- reuses coupling.py's own graph, not a second one."""
    G = build_dependency_graph(modules)
    radius = set(changed_module_names)
    for m in changed_module_names:
        if m in G:
            radius |= nx.ancestors(G, m)
    return radius


def blast_radius_test_paths(pcp_dir: Path, project_root: Path, changed_files: list[str]) -> list[str] | None:
    """Returns test file paths (relative to project_root) scoped to the
    modules impacted by changed_files, or None if scoping can't be
    determined with confidence -- caller must fall back to the full suite,
    never silently run zero tests."""
    modules = _load_modules_for_impact(pcp_dir)
    if not modules:
        return None

    changed_modules = changed_files_to_modules(pcp_dir, modules, changed_files)
    if not changed_modules:
        return None  # can't attribute the change to a declared module -- don't guess

    radius = blast_radius_modules(modules, changed_modules)

    candidates: set[Path] = set()
    for name in radius:
        for target in _module_target_files(pcp_dir, name):
            p = Path(target)
            stem = p.stem
            parent = p.parent
            for cand in (
                project_root / "tests" / f"test_{stem}.py",
                project_root / parent / f"test_{stem}.py",
                project_root / parent / "tests" / f"test_{stem}.py",
            ):
                if cand.exists():
                    candidates.add(cand)
            if stem.startswith("test_") and (project_root / target).exists():
                candidates.add(project_root / target)  # the agent wrote the test as the "target" itself

    # Per-module test DIRECTORIES (`tests/web_server/`) are how real projects
    # actually lay this out; the file patterns above only find
    # `tests/test_<stem>.py`. Without this the function returned None on
    # ontology-foundry despite 18 of 27 modules having exactly such a directory.
    for name in radius:
        for variant in _name_variants(name):
            d = project_root / "tests" / variant
            if d.is_dir():
                candidates.add(d)

    if not candidates:
        return None  # narrowed to real modules but found no matching test files -- fall back, don't run zero

    # Modularity drop-tests always run, whatever the radius. They are the
    # module-boundary guarantee -- "every module is a guest, it can leave
    # without drama" -- so a change that quietly couples two modules must fail
    # HERE, at the criterion, not survive until the wave merge.
    for d in (project_root / "tests" / "modularity", project_root / "tests" / "test_modularity"):
        if d.is_dir():
            candidates.add(d)

    return sorted(str(c.relative_to(project_root)) for c in candidates)
