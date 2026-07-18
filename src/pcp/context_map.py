"""Deterministic context routing — scenario → files an agent should read.

2026-07-18, from the context-contamination review. PCP already routed agent
context in code (prompt builders pointing at specific files per scenario),
but the routing was scattered and unauditable. `.pcp/context_map.yaml` makes
it one declarative, reviewable, drift-checkable table.

Principles (the review's three amendments, enforced by design):
- Scenario detection stays rung-1: scenarios are keyed off schema fields
  (logic_tier present, UI keyword match, module name) — never an LLM call.
- Intent files are routed WHOLE (objective/spec) — fragmenting spec content
  measurably collapses agent faithfulness (SLUMP, arXiv:2603.17104).
- Sliced state is always a GENERATED projection of a canonical source
  (module docs/built.md is regenerated from acceptance.yaml), never a
  hand-maintained copy — divergence is structurally impossible.

A missing context_map.yaml falls back to the built-in defaults below, so
projects scaffolded before this feature keep working unchanged.
"""

from pathlib import Path

import yaml

CONTEXT_MAP_FILE = "context_map.yaml"

# Built-in defaults — mirror the routing the prompt builders used before the
# table existed. `{module}` is substituted at resolve time. `fallback` is
# used only when none of the primary files exist.
DEFAULT_ROUTES: dict[str, dict] = {
    "always": {
        "files": [".pcp/objective.md", ".pcp/architecture.md", ".pcp/architect_persona.md"],
    },
    "module_state": {
        # The module's own generated state slice — NOT program-wide
        # current_state.md, which on a many-module project is mostly other
        # modules' context (the contamination the review named).
        "files": [".pcp/strategy/modules/{module}/docs/built.md"],
        "fallback": [".pcp/current_state.md"],
    },
    "ui_facing": {
        "files": [".pcp/design_system.md"],
    },
    "logic_tier_declared": {
        "files": [".pcp/logic_tier_guide.md"],
    },
}


def load(pcp_dir: Path) -> dict[str, dict]:
    path = pcp_dir / CONTEXT_MAP_FILE
    if not path.exists():
        return DEFAULT_ROUTES
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return DEFAULT_ROUTES
    routes = data.get("routes")
    if not isinstance(routes, dict) or not routes:
        return DEFAULT_ROUTES
    return routes


def resolve(pcp_dir: Path, scenario: str, module: str | None = None) -> list[str]:
    """Project-root-relative paths that exist for this scenario. Primary
    files first; fallback only when NO primary file exists."""
    routes = load(pcp_dir)
    route = routes.get(scenario)
    if not route:
        return []
    project_root = pcp_dir.parent

    def _expand(paths: list) -> list[str]:
        out = []
        for p in paths or []:
            p = str(p)
            if "{module}" in p:
                if not module:
                    continue
                p = p.replace("{module}", module)
            if (project_root / p).exists():
                out.append(p)
        return out

    primary = _expand(route.get("files", []))
    if primary:
        return primary
    return _expand(route.get("fallback", []))


def validate(pcp_dir: Path, known_modules: list[str] | None = None) -> list[str]:
    """Staleness check (CTRL-021): every routed path must resolve to at least
    one existing file (for {module} templates: for at least one known module,
    or via fallback). A route pointing at nothing silently starves agents of
    context — worse and less visible than over-feeding."""
    findings = []
    routes = load(pcp_dir)
    project_root = pcp_dir.parent
    modules = known_modules or [p.name for p in (pcp_dir / "strategy" / "modules").glob("*") if p.is_dir()]

    for scenario, route in routes.items():
        if not isinstance(route, dict):
            findings.append(f"context_map: route '{scenario}' is not a mapping")
            continue
        all_paths = list(route.get("files", [])) + list(route.get("fallback", []))
        if not all_paths:
            findings.append(f"context_map: route '{scenario}' lists no files")
            continue
        any_exists = False
        for p in all_paths:
            p = str(p)
            if "{module}" in p:
                if any((project_root / p.replace("{module}", m)).exists() for m in modules):
                    any_exists = True
                    break
            elif (project_root / p).exists():
                any_exists = True
                break
        if not any_exists:
            findings.append(
                f"context_map: route '{scenario}' resolves to zero existing files "
                f"({', '.join(str(p) for p in all_paths[:3])}) — agents routed here get nothing"
            )
    return findings
