"""Impacted-module test selection (2026-07-21) -- see impact.py's module
docstring. Reuses coupling.py's dependency graph rather than a bespoke
AST import analyzer: module-level blast radius (changed module + everyone
who transitively depends on it), then a file-naming heuristic within that
radius to find candidate test files. Every step degrades to None (caller
falls back to the full suite) rather than guessing."""

import yaml

from pcp.impact import (
    blast_radius_modules, blast_radius_test_paths, changed_files_to_modules,
)


def _write_module(pcp_dir, name, deps=None, criteria=None):
    mod_dir = pcp_dir / "strategy" / "modules" / name
    mod_dir.mkdir(parents=True)
    (mod_dir / "spec.yaml").write_text(yaml.dump({"dependencies": deps or []}))
    (mod_dir / "acceptance.yaml").write_text(yaml.dump({"criteria": criteria or []}))


def test_blast_radius_includes_dependents_not_dependencies():
    # auth <- api <- web  (api depends on auth, web depends on api)
    modules = {
        "auth": {"dependencies": []},
        "api": {"dependencies": ["auth"]},
        "web": {"dependencies": ["api"]},
    }
    radius = blast_radius_modules(modules, {"auth"})
    # everything that (transitively) depends on auth is in the blast radius
    assert radius == {"auth", "api", "web"}


def test_blast_radius_does_not_pull_in_unrelated_modules():
    modules = {
        "auth": {"dependencies": []},
        "billing": {"dependencies": []},  # unrelated, no edge to/from auth
    }
    radius = blast_radius_modules(modules, {"auth"})
    assert radius == {"auth"}


def test_changed_files_to_modules_matches_declared_targets(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _write_module(pcp_dir, "auth", criteria=[{"id": "A001", "target": "src/auth/login.py"}])
    _write_module(pcp_dir, "billing", criteria=[{"id": "B001", "target": "src/billing/charge.py"}])
    modules = {"auth": {}, "billing": {}}

    matched = changed_files_to_modules(pcp_dir, modules, ["src/auth/login.py"])
    assert matched == {"auth"}


def test_blast_radius_test_paths_finds_convention_test_file(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _write_module(pcp_dir, "auth", criteria=[{"id": "A001", "target": "src/auth/login.py"}])
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_login.py").write_text("def test_x(): pass")

    paths = blast_radius_test_paths(pcp_dir, tmp_path, ["src/auth/login.py"])
    assert paths == ["tests/test_login.py"]


def test_blast_radius_test_paths_includes_dependent_modules_tests(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _write_module(pcp_dir, "auth", criteria=[{"id": "A001", "target": "src/auth/login.py"}])
    _write_module(pcp_dir, "api", deps=["auth"], criteria=[{"id": "P001", "target": "src/api/routes.py"}])
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_login.py").write_text("x")
    (tmp_path / "tests" / "test_routes.py").write_text("x")  # api's own test

    paths = blast_radius_test_paths(pcp_dir, tmp_path, ["src/auth/login.py"])
    # api depends on auth, so a change to auth pulls api's tests in too
    assert set(paths) == {"tests/test_login.py", "tests/test_routes.py"}


def test_blast_radius_test_paths_none_when_change_unattributable(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _write_module(pcp_dir, "auth", criteria=[{"id": "A001", "target": "src/auth/login.py"}])
    # changed file matches no module's declared target -- don't guess
    assert blast_radius_test_paths(pcp_dir, tmp_path, ["src/unrelated/thing.py"]) is None


def test_blast_radius_test_paths_none_when_no_matching_test_file_found(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _write_module(pcp_dir, "auth", criteria=[{"id": "A001", "target": "src/auth/login.py"}])
    # module attributed correctly, but no test_login.py exists anywhere -- fall back, don't run zero
    assert blast_radius_test_paths(pcp_dir, tmp_path, ["src/auth/login.py"]) is None


def test_blast_radius_test_paths_none_when_no_modules_dir(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    assert blast_radius_test_paths(pcp_dir, tmp_path, ["src/x.py"]) is None


# ── Scoping must actually resolve on real layouts (2026-07-27) ──

def _project(tmp_path, modules, tests_dirs=(), extra_files=()):
    import yaml
    pcp = tmp_path / ".pcp" / "strategy" / "modules"
    for name, deps in modules.items():
        d = pcp / name
        d.mkdir(parents=True)
        (d / "spec.yaml").write_text(yaml.dump(
            {"version": "1.0", "module": name, "description": f"{name} does things.",
             "objective_coverage": ["x"], "dependencies": list(deps), "constraints": []}))
        (d / "acceptance.yaml").write_text(yaml.dump(
            {"version": "1.0", "module": name,
             "criteria": [{"id": "A001", "description": "d", "check": "manual", "status": "pending"}]}))
    for t in tests_dirs:
        (tmp_path / "tests" / t).mkdir(parents=True, exist_ok=True)
        (tmp_path / "tests" / t / "test_x.py").write_text("def test_x(): pass\n")
    for f in extra_files:
        p = tmp_path / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x = 1\n")
    return tmp_path / ".pcp", tmp_path


def test_changed_file_attributed_by_path_convention_without_declared_target(tmp_path):
    """Attribution used to depend solely on criteria declaring `target`. Only
    51 of 382 Project O criteria do, so it returned an empty set for
    real files and every gate fell back to the full 1,098-test suite."""
    from pcp.impact import changed_files_to_modules, _load_modules_for_impact
    pcp_dir, root = _project(tmp_path, {"web-server": [], "accounts": []})
    modules = _load_modules_for_impact(pcp_dir)
    hit = changed_files_to_modules(pcp_dir, modules, ["src/web_server/routes/review.py"])
    assert hit == {"web-server"}, "underscored directory segment must attribute to the module"


def test_path_attribution_requires_a_whole_segment_not_a_prefix(tmp_path):
    from pcp.impact import changed_files_to_modules, _load_modules_for_impact
    pcp_dir, root = _project(tmp_path, {"web-server": []})
    modules = _load_modules_for_impact(pcp_dir)
    assert changed_files_to_modules(pcp_dir, modules, ["src/web_server_helpers.py"]) == set()


def test_per_module_test_directories_are_discovered(tmp_path):
    """`tests/web_server/` is how real projects lay this out; the old file-only
    patterns found nothing there and fell back to the full suite."""
    from pcp.impact import blast_radius_test_paths
    pcp_dir, root = _project(
        tmp_path, {"web-server": [], "accounts": []},
        tests_dirs=("web_server", "accounts"))
    paths = blast_radius_test_paths(pcp_dir, root, ["src/web_server/app.py"])
    assert paths is not None
    assert "tests/web_server" in paths
    assert "tests/accounts" not in paths, "unrelated module must not be pulled in"


def test_modularity_drop_tests_always_included(tmp_path):
    """The module-boundary guarantee. A change that quietly couples two modules
    must fail at the criterion, not survive to the wave merge."""
    from pcp.impact import blast_radius_test_paths
    pcp_dir, root = _project(
        tmp_path, {"web-server": []}, tests_dirs=("web_server", "modularity"))
    paths = blast_radius_test_paths(pcp_dir, root, ["src/web_server/app.py"])
    assert "tests/modularity" in paths


def test_dependents_are_included_in_the_radius(tmp_path):
    """A module that depends on the changed one can break from it, so its tests
    belong in scope — this is why scoping is ~2.3x, not the naive 3x."""
    from pcp.impact import blast_radius_test_paths
    pcp_dir, root = _project(
        tmp_path, {"web-server": [], "web-ui": ["web-server"], "unrelated": []},
        tests_dirs=("web_server", "web_ui", "unrelated"))
    paths = blast_radius_test_paths(pcp_dir, root, ["src/web_server/app.py"])
    assert "tests/web_ui" in paths, "dependents must be tested"
    assert "tests/unrelated" not in paths


def test_unattributable_change_falls_back_to_full_suite(tmp_path):
    """Never silently run zero tests."""
    from pcp.impact import blast_radius_test_paths
    pcp_dir, root = _project(tmp_path, {"web-server": []}, tests_dirs=("web_server",))
    assert blast_radius_test_paths(pcp_dir, root, ["README.md"]) is None
