"""Deterministic coupling analysis — replaces the LLM-judged coupling_score.

Coupling, unlike coverage ("does the plan cover the objective" — genuinely
semantic), is graph math: circular deps, dependency counts, and god modules
are all mechanically computable from each module's declared 'dependencies'
field. No LLM needed, no non-determinism, no cost, no flakiness.

The scoring formula here is the exact one PCP's own validate-strategy prompt
already specified — it was already fully deterministic, just being computed
by an LLM instead of code. Community detection (graphify, if installed) is
optional enrichment layered on top: it surfaces informal coupling — modules
that cluster together beyond their declared dependencies — it does not feed
the numeric coupling_score itself.
"""

import networkx as nx

DEP_PENALTY = 0.1
CYCLE_PENALTY = 0.2
GOD_MODULE_PENALTY = 0.15
GOD_MODULE_THRESHOLD = 3


def _hub_modules(G: nx.DiGraph) -> set[str]:
    """Modules more than half the other modules depend on — shared
    infrastructure (a 'core' or 'utils' module), not harmful coupling.
    Same reasoning graphify's own cluster() has an exclude_hubs_percentile
    for: a widely-depended-on utility module skews naive graph metrics."""
    n = G.number_of_nodes()
    if n < 3:
        return set()
    threshold = n / 2
    return {node for node, indeg in G.in_degree() if indeg > threshold}


def build_dependency_graph(modules: dict[str, dict]) -> nx.DiGraph:
    """modules: {module_name: spec_dict}. Edge A->B means A depends on B.
    Dependencies pointing outside this module set are ignored (external/
    already-built, not something this graph can score)."""
    G = nx.DiGraph()
    for name in modules:
        G.add_node(name)
    for name, spec in modules.items():
        for dep in (spec.get("dependencies") or []):
            if dep in modules and dep != name:
                G.add_edge(name, dep)
    return G


def compute_coupling(G: nx.DiGraph) -> dict:
    """Returns {coupling_score, coupling_violations, direct_dependencies,
    circular_dependencies, god_modules, hub_modules} — same shape
    validate-strategy's LLM prompt used to return, now computed directly.

    Edges into a hub module (shared infrastructure) don't count against the
    direct-dependency penalty — depending on a genuine shared kernel isn't
    the harmful coupling this score is meant to catch. Cycles and god-modules
    still count regardless of hub status: a cycle through 'core', or a module
    with too many outgoing deps, is still a real structural problem."""
    hubs = _hub_modules(G)
    scored_edges = [(a, b) for a, b in G.edges if b not in hubs]
    direct_deps = len(scored_edges)
    cycles = [c for c in nx.simple_cycles(G) if len(c) > 1]
    god_modules = [n for n in G.nodes if G.out_degree(n) > GOD_MODULE_THRESHOLD]

    score = 1.0 - DEP_PENALTY * direct_deps - CYCLE_PENALTY * len(cycles) - GOD_MODULE_PENALTY * len(god_modules)
    score = max(0.0, min(1.0, score))

    violations = []
    for cycle in cycles:
        path = " -> ".join(cycle + [cycle[0]])
        violations.append({
            "type": "circular", "modules": cycle,
            "description": f"Circular dependency: {path}",
            "fix": "Break the cycle — extract a shared interface or invert one dependency.",
        })
    for n in god_modules:
        deps = sorted(G.successors(n))
        violations.append({
            "type": "god_module", "modules": [n],
            "description": f"'{n}' depends on {len(deps)} other modules: {', '.join(deps)}",
            "fix": "Split into smaller modules or reduce dependencies.",
        })
    for a, b in sorted(scored_edges):
        violations.append({
            "type": "direct_dependency", "modules": [a, b],
            "description": f"'{a}' directly depends on '{b}'",
            "fix": "Confirm this is core infrastructure, not incidental coupling.",
        })

    return {
        "coupling_score": round(score, 2),
        "coupling_violations": violations,
        "direct_dependencies": direct_deps,
        "circular_dependencies": len(cycles),
        "god_modules": god_modules,
        "hub_modules": sorted(hubs),
    }


def compute_communities(G: nx.DiGraph) -> dict:
    """Optional enrichment via graphify's community detection (Leiden/Louvain).
    Advisory only — surfaces modules that cluster together, doesn't affect
    coupling_score. Returns {"available": False} if graphify isn't installed."""
    try:
        from graphify.cluster import cluster, score_all
    except ImportError:
        return {"available": False}
    if G.number_of_nodes() < 2:
        return {"available": True, "communities": {}, "cohesion": {}}
    communities = cluster(G)
    cohesion = score_all(G, communities)
    multi_node = {cid: nodes for cid, nodes in communities.items() if len(nodes) > 1}
    return {"available": True, "communities": multi_node, "cohesion": {cid: cohesion[cid] for cid in multi_node}}
