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
