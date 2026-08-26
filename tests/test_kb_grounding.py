"""A015 — Tier-1 deterministic check: flags a diff touching a field/line an
existing spec.yaml constraint entry protects, with no accompanying
constraint-text update in the same diff. Zero LLM calls, pure regex + git diff.
"""

import subprocess

from pcp.kb_grounding import (
    check_constraint_protection,
    extract_protected_identifiers,
    load_constraint_index,
)


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(tmp_path):
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "t@t.com"], tmp_path)
    _git(["config", "user.name", "T"], tmp_path)


def _make_module(tmp_path, name, constraints, extra_source_text="pass\n"):
    """Creates .pcp/strategy/modules/<name>/spec.yaml with the given
    constraints list, plus a source file src/<name>_thing.py, commits both
    as the baseline (unstaged working tree change comes later per test)."""
    modules_dir = tmp_path / ".pcp" / "strategy" / "modules" / name
    modules_dir.mkdir(parents=True)
    lines = "\n".join(f"- {c}" for c in constraints)
    (modules_dir / "spec.yaml").write_text(f"module: {name}\nconstraints:\n{lines}\n")

    src_dir = tmp_path / "src"
    src_dir.mkdir(exist_ok=True)
    src_file = src_dir / f"{name}_thing.py"
    src_file.write_text(extra_source_text)
    return modules_dir / "spec.yaml", src_file


# ── extract_protected_identifiers ──────────────────────────────────────────

def test_extract_identifiers_requires_underscore_dot_or_colon():
    """Plain English words never count as protected identifiers -- only a
    token shaped like real code (underscore, dot, or colon inside it)."""
    text = "A card's tier:cited claim requires a literal quote; a filename alone is never sufficient"
    idents = extract_protected_identifiers(text)
    assert "tier:cited" in idents
    assert "filename" not in idents
    assert "alone" not in idents


def test_extract_identifiers_dotted_filename():
    idents = extract_protected_identifiers("topics.yaml is derived only from four structured inputs")
    assert "topics.yaml" in idents


def test_extract_identifiers_underscore_field_name():
    idents = extract_protected_identifiers(
        "Card schema enforces card_version and requires a delta_summary field when superseded"
    )
    assert "card_version" in idents
    assert "delta_summary" in idents


def test_extract_identifiers_empty_for_plain_prose():
    assert extract_protected_identifiers("Coverage is mandatory for every file") == set()


# ── load_constraint_index ───────────────────────────────────────────────────

def test_load_constraint_index_maps_identifier_to_module_and_text(tmp_path):
    _init_repo(tmp_path)
    _make_module(
        tmp_path, "kb",
        ["Card schema enforces card_version and requires delta_summary on supersede"],
    )
    index = load_constraint_index(tmp_path / ".pcp")
    assert "card_version" in index
    module_name, text = index["card_version"][0]
    assert module_name == "kb"
    assert "card_version" in text


def test_load_constraint_index_empty_when_no_modules(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / ".pcp" / "strategy" / "modules").mkdir(parents=True)
    assert load_constraint_index(tmp_path / ".pcp") == {}


def test_load_constraint_index_skips_malformed_spec_yaml(tmp_path):
    _init_repo(tmp_path)
    modules_dir = tmp_path / ".pcp" / "strategy" / "modules" / "broken"
    modules_dir.mkdir(parents=True)
    (modules_dir / "spec.yaml").write_text("not: valid: yaml: [")
    assert load_constraint_index(tmp_path / ".pcp") == {}


# ── check_constraint_protection ────────────────────────────────────────────

def test_flags_diff_touching_protected_identifier_with_no_spec_update(tmp_path):
    _init_repo(tmp_path)
    _spec_path, src_path = _make_module(
        tmp_path, "kb",
        ["Card schema enforces card_version and never overwrites an existing card in place"],
    )
    _git(["add", "."], tmp_path)
    _git(["commit", "-q", "-m", "init"], tmp_path)

    # Change the source file to touch the protected identifier -- but do NOT
    # touch spec.yaml at all in this diff.
    src_path.write_text("card_version = 2\n")
    _git(["add", str(src_path.relative_to(tmp_path))], tmp_path)

    staged = [str(src_path.relative_to(tmp_path))]
    violations = check_constraint_protection(staged, tmp_path, tmp_path / ".pcp")

    assert len(violations) == 1
    assert "card_version" in violations[0]
    assert "kb" in violations[0]


def test_no_violation_when_spec_text_updated_in_same_diff(tmp_path):
    _init_repo(tmp_path)
    spec_path, src_path = _make_module(
        tmp_path, "kb",
        ["Card schema enforces card_version and never overwrites an existing card in place"],
    )
    _git(["add", "."], tmp_path)
    _git(["commit", "-q", "-m", "init"], tmp_path)

    src_path.write_text("card_version = 2\n")
    spec_path.write_text(
        "module: kb\nconstraints:\n"
        "- Card schema enforces card_version (now integer, was string) and never "
        "overwrites an existing card in place\n"
    )
    rel_src = str(src_path.relative_to(tmp_path))
    rel_spec = str(spec_path.relative_to(tmp_path))
    _git(["add", rel_src, rel_spec], tmp_path)

    staged = [rel_src, rel_spec]
    violations = check_constraint_protection(staged, tmp_path, tmp_path / ".pcp")
    assert violations == []


def test_no_violation_when_identifier_not_touched(tmp_path):
    _init_repo(tmp_path)
    _spec_path, src_path = _make_module(
        tmp_path, "kb",
        ["Card schema enforces card_version and never overwrites an existing card in place"],
    )
    _git(["add", "."], tmp_path)
    _git(["commit", "-q", "-m", "init"], tmp_path)

    src_path.write_text("pass\nx = 1\n")  # unrelated change, no protected identifier
    rel_src = str(src_path.relative_to(tmp_path))
    _git(["add", rel_src], tmp_path)

    violations = check_constraint_protection([rel_src], tmp_path, tmp_path / ".pcp")
    assert violations == []


def test_no_violation_when_no_constraints_declared(tmp_path):
    _init_repo(tmp_path)
    modules_dir = tmp_path / ".pcp" / "strategy" / "modules" / "kb"
    modules_dir.mkdir(parents=True)
    (modules_dir / "spec.yaml").write_text("module: kb\nconstraints: []\n")
    src_file = tmp_path / "src" / "kb_thing.py"
    src_file.parent.mkdir(parents=True)
    src_file.write_text("pass\n")
    _git(["add", "."], tmp_path)
    _git(["commit", "-q", "-m", "init"], tmp_path)

    src_file.write_text("card_version = 2\n")
    rel_src = str(src_file.relative_to(tmp_path))
    _git(["add", rel_src], tmp_path)

    violations = check_constraint_protection([rel_src], tmp_path, tmp_path / ".pcp")
    assert violations == []


def test_spec_yaml_itself_never_flagged_as_the_violating_file(tmp_path):
    """spec.yaml diffs are the constraint-text side of the check, not the
    protected side -- editing spec.yaml alone (e.g. adding a brand-new
    constraint) must never itself be flagged as an unprotected change."""
    _init_repo(tmp_path)
    spec_path, _src_path = _make_module(
        tmp_path, "kb",
        ["Card schema enforces card_version and never overwrites an existing card in place"],
    )
    _git(["add", "."], tmp_path)
    _git(["commit", "-q", "-m", "init"], tmp_path)

    spec_path.write_text(
        "module: kb\nconstraints:\n"
        "- Card schema enforces card_version and never overwrites an existing card in place\n"
        "- topics.yaml is derived only from structured inputs\n"
    )
    rel_spec = str(spec_path.relative_to(tmp_path))
    _git(["add", rel_spec], tmp_path)

    violations = check_constraint_protection([rel_spec], tmp_path, tmp_path / ".pcp")
    assert violations == []


def test_multiple_modules_only_matching_module_spec_satisfies(tmp_path):
    """A protected identifier from module A's constraints is not satisfied by
    touching module B's spec.yaml, even if B's spec.yaml is also staged."""
    _init_repo(tmp_path)
    _spec_a, src_a = _make_module(
        tmp_path, "kb",
        ["Card schema enforces card_version and never overwrites an existing card in place"],
    )
    spec_b, _src_b = _make_module(tmp_path, "core", ["Registry lookup stays O(1) via a dict index"])
    _git(["add", "."], tmp_path)
    _git(["commit", "-q", "-m", "init"], tmp_path)

    src_a.write_text("card_version = 2\n")
    spec_b.write_text("module: core\nconstraints:\n- Registry lookup stays O(1) via a dict index, now cached\n")
    rel_src_a = str(src_a.relative_to(tmp_path))
    rel_spec_b = str(spec_b.relative_to(tmp_path))
    _git(["add", rel_src_a, rel_spec_b], tmp_path)

    violations = check_constraint_protection([rel_src_a, rel_spec_b], tmp_path, tmp_path / ".pcp")
    assert len(violations) == 1
    assert "card_version" in violations[0]
