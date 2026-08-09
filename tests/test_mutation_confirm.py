"""Targeted mutation-testing confirmation (2026-08-08) -- the empirical
follow-up to test_composition.py's static grep-shaped flag. Deliberately
scoped to ONE flagged function at a time, not a whole-module sweep: see
mutation_confirm.py's module docstring for why a broad sweep was rejected
as low-signal-per-cost."""

import shutil
import textwrap
from unittest.mock import patch, MagicMock

import pytest

from pcp.mutation_confirm import (
    resolve_definition_file, resolve_definition_file_all,
    run_targeted_mutation_test, cosmic_ray_available,
)

HAS_COSMIC_RAY = shutil.which("cosmic-ray") is not None


# ── resolve_definition_file: pure AST, no subprocess ──

def test_resolves_a_simple_function(tmp_path):
    (tmp_path / "thing.py").write_text("def compute_score(a, b):\n    return a + b\n")
    result = resolve_definition_file(tmp_path, "compute_score")
    assert result == tmp_path / "thing.py"


def test_resolves_a_class(tmp_path):
    (tmp_path / "thing.py").write_text("class Scorer:\n    pass\n")
    result = resolve_definition_file(tmp_path, "Scorer")
    assert result == tmp_path / "thing.py"


def test_returns_none_when_not_found(tmp_path):
    (tmp_path / "thing.py").write_text("def other_function():\n    pass\n")
    assert resolve_definition_file(tmp_path, "compute_score") is None


def test_skips_test_files_as_definition_sources(tmp_path):
    """The definition must come from SOURCE, not another test file that
    happens to define a same-named helper."""
    (tmp_path / "test_thing.py").write_text("def compute_score():\n    pass\n")  # a test helper, not the real def
    (tmp_path / "thing.py").write_text("def compute_score(a, b):\n    return a + b\n")
    result = resolve_definition_file(tmp_path, "compute_score")
    assert result == tmp_path / "thing.py"


def test_finds_all_ambiguous_matches(tmp_path):
    (tmp_path / "a.py").write_text("def compute_score():\n    pass\n")
    (tmp_path / "b.py").write_text("def compute_score():\n    pass\n")
    matches = resolve_definition_file_all(tmp_path, "compute_score")
    assert len(matches) == 2


def test_skips_syntax_error_files_gracefully(tmp_path):
    (tmp_path / "broken.py").write_text("def compute_score(:\n   invalid\n")
    (tmp_path / "thing.py").write_text("def compute_score():\n    pass\n")
    result = resolve_definition_file(tmp_path, "compute_score")
    assert result == tmp_path / "thing.py"


# ── run_targeted_mutation_test: mocked subprocess ──

def test_unavailable_when_cosmic_ray_not_installed(tmp_path):
    (tmp_path / "thing.py").write_text("def f():\n    pass\n")
    (tmp_path / "test_thing.py").write_text("def test_f():\n    pass\n")
    with patch("pcp.mutation_confirm.shutil.which", return_value=None):
        result = run_targeted_mutation_test(
            tmp_path, "f", tmp_path / "thing.py", tmp_path / "test_thing.py",
        )
    assert result == {"available": False}


def test_init_failure_reported_not_raised(tmp_path):
    (tmp_path / "thing.py").write_text("def f():\n    pass\n")
    (tmp_path / "test_thing.py").write_text("def test_f():\n    pass\n")
    with patch("pcp.mutation_confirm.shutil.which", return_value="/usr/bin/cosmic-ray"), \
         patch("pcp.mutation_confirm.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stderr="bad config", stdout="")
        result = run_targeted_mutation_test(
            tmp_path, "f", tmp_path / "thing.py", tmp_path / "test_thing.py",
        )
    assert result["available"] is True
    assert "init failed" in result["error"]


def test_baseline_failure_distinguished_from_other_errors(tmp_path):
    """A failing baseline means the UNMUTATED code doesn't pass its own
    tests -- a different, real finding from 'the test is weak', surfaced
    with distinct wording so it isn't misread as a mutation-score result."""
    (tmp_path / "thing.py").write_text("def f():\n    pass\n")
    (tmp_path / "test_thing.py").write_text("def test_f():\n    pass\n")
    with patch("pcp.mutation_confirm.shutil.which", return_value="/usr/bin/cosmic-ray"), \
         patch("pcp.mutation_confirm.subprocess.run") as mock_run:
        def side_effect(cmd, **kwargs):
            if cmd[1] == "init":
                return MagicMock(returncode=0, stderr="", stdout="")
            if cmd[1] == "baseline":
                return MagicMock(returncode=1, stderr="tests failed on unmutated code", stdout="")
            return MagicMock(returncode=0, stderr="", stdout="")
        mock_run.side_effect = side_effect
        result = run_targeted_mutation_test(
            tmp_path, "f", tmp_path / "thing.py", tmp_path / "test_thing.py",
        )
    assert result["available"] is True
    assert "baseline failed" in result["error"]


def test_filters_dump_output_to_only_the_target_function(tmp_path):
    """A file with TWO functions must not let mutations of the OTHER
    function pollute the target's score."""
    import json
    (tmp_path / "thing.py").write_text("def f():\n    pass\ndef g():\n    pass\n")
    (tmp_path / "test_thing.py").write_text("def test_f():\n    pass\n")

    dump_lines = [
        json.dumps([{"mutations": [{"definition_name": "f"}]}, {"test_outcome": "killed"}]),
        json.dumps([{"mutations": [{"definition_name": "f"}]}, {"test_outcome": "survived"}]),
        json.dumps([{"mutations": [{"definition_name": "g"}]}, {"test_outcome": "survived"}]),  # NOT the target
        json.dumps([{"mutations": [{"definition_name": "g"}]}, {"test_outcome": "survived"}]),  # NOT the target
    ]

    with patch("pcp.mutation_confirm.shutil.which", return_value="/usr/bin/cosmic-ray"), \
         patch("pcp.mutation_confirm.subprocess.run") as mock_run:
        def side_effect(cmd, **kwargs):
            if cmd[1] == "dump":
                return MagicMock(returncode=0, stdout="\n".join(dump_lines), stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = side_effect
        result = run_targeted_mutation_test(
            tmp_path, "f", tmp_path / "thing.py", tmp_path / "test_thing.py",
        )

    assert result["killed"] == 1
    assert result["survived"] == 1
    assert result["mutation_score"] == 0.5
    assert result["confirms_grep_shaped"] is False  # not ALL survived, so not a confirmed pure grep-shape


def test_all_survived_confirms_grep_shaped(tmp_path):
    import json
    (tmp_path / "thing.py").write_text("def f():\n    pass\n")
    (tmp_path / "test_thing.py").write_text("def test_f():\n    pass\n")
    dump_lines = [
        json.dumps([{"mutations": [{"definition_name": "f"}]}, {"test_outcome": "survived"}]),
        json.dumps([{"mutations": [{"definition_name": "f"}]}, {"test_outcome": "survived"}]),
    ]
    with patch("pcp.mutation_confirm.shutil.which", return_value="/usr/bin/cosmic-ray"), \
         patch("pcp.mutation_confirm.subprocess.run") as mock_run:
        def side_effect(cmd, **kwargs):
            if cmd[1] == "dump":
                return MagicMock(returncode=0, stdout="\n".join(dump_lines), stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = side_effect
        result = run_targeted_mutation_test(
            tmp_path, "f", tmp_path / "thing.py", tmp_path / "test_thing.py",
        )
    assert result["mutation_score"] == 0.0
    assert result["confirms_grep_shaped"] is True


def test_zero_mutations_is_none_not_false(tmp_path):
    """No mutants for this definition at all (e.g. a class with nothing
    mutable) is a different, honest 'we could not judge this' -- not the
    same as 'confirmed grep-shaped' (all survived) or 'refuted' (some killed)."""
    (tmp_path / "thing.py").write_text("def f():\n    pass\n")
    (tmp_path / "test_thing.py").write_text("def test_f():\n    pass\n")
    with patch("pcp.mutation_confirm.shutil.which", return_value="/usr/bin/cosmic-ray"), \
         patch("pcp.mutation_confirm.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        result = run_targeted_mutation_test(
            tmp_path, "f", tmp_path / "thing.py", tmp_path / "test_thing.py",
        )
    assert result["mutation_score"] is None
    assert result["confirms_grep_shaped"] is None


# ── real end-to-end, only when cosmic-ray is actually installed ──

@pytest.mark.skipif(not HAS_COSMIC_RAY, reason="cosmic-ray not installed")
def test_real_cosmic_ray_confirms_a_genuinely_grep_shaped_test(tmp_path):
    """Reproduces the actual spike: a real-execution test kills every
    mutation, a grep-shaped test kills none."""
    (tmp_path / "thing.py").write_text("def compute_score(a, b):\n    return a + b\n")
    (tmp_path / "test_grep.py").write_text(textwrap.dedent("""
        def test_function_name_present():
            content = open("thing.py").read()
            assert "compute_score" in content
    """))
    defn = resolve_definition_file(tmp_path, "compute_score")
    assert defn == tmp_path / "thing.py"

    result = run_targeted_mutation_test(
        tmp_path, "compute_score", defn, tmp_path / "test_grep.py", timeout_sec=10.0,
    )
    assert result["available"] is True
    assert "error" not in result
    assert result["mutation_score"] == 0.0
    assert result["confirms_grep_shaped"] is True


def test_cosmic_ray_available_reflects_which(tmp_path):
    with patch("pcp.mutation_confirm.shutil.which", return_value=None):
        assert cosmic_ray_available() is False
    with patch("pcp.mutation_confirm.shutil.which", return_value="/usr/bin/cosmic-ray"):
        assert cosmic_ray_available() is True
