from pcp import coupling


def _graph(modules):
    return coupling.build_dependency_graph(modules)


def test_no_dependencies_scores_perfect():
    modules = {"a": {"dependencies": []}, "b": {"dependencies": []}}
    result = coupling.compute_coupling(_graph(modules))
    assert result["coupling_score"] == 1.0
    assert result["direct_dependencies"] == 0
    assert result["circular_dependencies"] == 0
    assert result["god_modules"] == []


def test_direct_dependency_penalized():
    modules = {"a": {"dependencies": ["b"]}, "b": {"dependencies": []}}
    result = coupling.compute_coupling(_graph(modules))
    assert result["direct_dependencies"] == 1
    assert result["coupling_score"] == 0.9
    assert result["coupling_violations"][0]["type"] == "direct_dependency"


def test_circular_dependency_detected_and_penalized_more_than_direct():
    modules = {"a": {"dependencies": ["b"]}, "b": {"dependencies": ["a"]}}
    result = coupling.compute_coupling(_graph(modules))
    assert result["circular_dependencies"] == 1
    types = {v["type"] for v in result["coupling_violations"]}
    assert "circular" in types
    # 2 direct deps (0.2) + 1 cycle (0.2) = 1.0 - 0.4 = 0.6
    assert result["coupling_score"] == 0.6


def test_god_module_detected_when_out_degree_exceeds_threshold():
    modules = {
        "hub": {"dependencies": ["a", "b", "c", "d"]},
        "a": {"dependencies": []}, "b": {"dependencies": []},
        "c": {"dependencies": []}, "d": {"dependencies": []},
    }
    result = coupling.compute_coupling(_graph(modules))
    assert "hub" in result["god_modules"]
    assert any(v["type"] == "god_module" for v in result["coupling_violations"])


# ── aggregator exemption (win2mac dogfood, 2026-08-08) ──
# The symmetric case to hub_modules: a launcher/deployment-orchestrator
# module's high OUT-degree is the design, not accidental coupling.

def test_declared_aggregator_exempt_from_god_module():
    modules = {
        "deployer": {"dependencies": ["a", "b", "c", "d"], "aggregator": True},
        "a": {"dependencies": []}, "b": {"dependencies": []},
        "c": {"dependencies": []}, "d": {"dependencies": []},
    }
    result = coupling.compute_coupling(_graph(modules))
    assert "deployer" not in result["god_modules"]
    assert not any(v["type"] == "god_module" for v in result["coupling_violations"])
    assert result["aggregator_modules"] == ["deployer"]


def test_undeclared_module_with_many_deps_still_flagged():
    """The exemption is opt-in -- a module that just happens to have many
    deps without declaring aggregator: true is unaffected, unchanged
    behavior from test_god_module_detected_when_out_degree_exceeds_threshold."""
    modules = {
        "hub": {"dependencies": ["a", "b", "c", "d"]},
        "a": {"dependencies": []}, "b": {"dependencies": []},
        "c": {"dependencies": []}, "d": {"dependencies": []},
    }
    result = coupling.compute_coupling(_graph(modules))
    assert "hub" in result["god_modules"]
    assert result["aggregator_modules"] == []


def test_aggregators_outgoing_edges_exempt_from_direct_dependency_penalty():
    modules = {
        "deployer": {"dependencies": ["a", "b"], "aggregator": True},
        "a": {"dependencies": []}, "b": {"dependencies": []},
    }
    result = coupling.compute_coupling(_graph(modules))
    assert result["direct_dependencies"] == 0
    assert result["coupling_score"] == 1.0


def test_cycle_through_an_aggregator_still_counts():
    """Same rule as hub-through-cycle -- self-declaration never waives a
    real circular dependency."""
    modules = {
        "deployer": {"dependencies": ["a"], "aggregator": True},
        "a": {"dependencies": ["deployer"]},
    }
    result = coupling.compute_coupling(_graph(modules))
    assert result["circular_dependencies"] == 1


def test_hub_module_excluded_from_direct_dependency_penalty():
    """A widely-depended-on 'core' module (more than half the others depend on
    it) is shared infrastructure, not harmful coupling -- shouldn't penalize
    each module that legitimately depends on it."""
    modules = {
        "core": {"dependencies": []},
        "a": {"dependencies": ["core"]},
        "b": {"dependencies": ["core"]},
        "c": {"dependencies": ["core"]},
    }
    result = coupling.compute_coupling(_graph(modules))
    assert "core" in result["hub_modules"]
    assert result["direct_dependencies"] == 0
    assert result["coupling_score"] == 1.0


def test_cycle_through_hub_still_counts():
    """Hub-exclusion only waives the direct-dependency penalty -- a cycle
    through a hub is still a real structural problem."""
    modules = {
        "core": {"dependencies": ["a"]},
        "a": {"dependencies": ["core"]},
        "b": {"dependencies": ["core"]},
        "c": {"dependencies": ["core"]},
    }
    result = coupling.compute_coupling(_graph(modules))
    assert "core" in result["hub_modules"]
    assert result["circular_dependencies"] == 1


def test_self_dependency_ignored():
    modules = {"a": {"dependencies": ["a"]}}
    graph = _graph(modules)
    assert graph.number_of_edges() == 0


def test_dependency_outside_module_set_ignored():
    modules = {"a": {"dependencies": ["external_not_a_module"]}}
    graph = _graph(modules)
    assert graph.number_of_edges() == 0


def test_score_never_goes_below_zero():
    modules = {
        "a": {"dependencies": ["b", "c", "d", "e"]},
        "b": {"dependencies": ["a", "c", "d", "e"]},
        "c": {"dependencies": ["a", "b", "d", "e"]},
        "d": {"dependencies": ["a", "b", "c", "e"]},
        "e": {"dependencies": ["a", "b", "c", "d"]},
    }
    result = coupling.compute_coupling(_graph(modules))
    assert result["coupling_score"] >= 0.0


def test_compute_communities_reports_unavailable_without_graphify():
    import sys
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("graphify"):
            raise ImportError("no graphify")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = fake_import
    try:
        result = coupling.compute_communities(_graph({"a": {}, "b": {}}))
    finally:
        builtins.__import__ = real_import
    assert result == {"available": False}
