"""A015 — Tier-1 deterministic check: flags a diff touching a field/line an
existing spec.yaml constraint entry protects, with no accompanying
constraint-text update in the same diff. Zero LLM calls, pure regex + git diff.

A016 — Tier-6 LLM-judged check: compares a diff against loaded
kb/file_metadata content for non-mechanical contradictions and returns a
genuinely-blocking verdict, same posture as CTRL-015 (mocked judge calls
only, same convention as test_narrative_lint.py).
"""

import subprocess
from unittest.mock import patch

import yaml

from pcp.kb_grounding import (
    check_constraint_protection,
    check_kb_contradiction,
    extract_protected_identifiers,
    load_constraint_index,
    load_kb_claims_for_files,
)


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(tmp_path):
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "t@t.com"], tmp_path)
    _git(["config", "user.name", "T"], tmp_path)


def _write_card(tmp_path, rel_source_path, claims):
    """Writes a file-metadata card at the mirrored path
    .pcp/kb/file_metadata/<rel_source_path>.yaml -- same layout
    kb_file_metadata.card_path_for produces (A001)."""
    from pcp.kb_file_metadata import card_path_for

    pcp_dir = tmp_path / ".pcp"
    card_path = card_path_for(pcp_dir, tmp_path, tmp_path / rel_source_path)
    card_path.parent.mkdir(parents=True, exist_ok=True)
    card_path.write_text(yaml.safe_dump({"card_version": 1, "claims": claims}, sort_keys=False))
    return card_path


def _stage_diff(tmp_path, rel_path, old_text, new_text, committed=True):
    """Commits old_text (if committed), writes new_text, stages it. Returns
    the staged relative path."""
    p = tmp_path / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(old_text)
    if committed:
        _git(["add", rel_path], tmp_path)
        _git(["commit", "-q", "-m", "init"], tmp_path)
    p.write_text(new_text)
    _git(["add", rel_path], tmp_path)
    return rel_path


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


# ── A016: load_kb_claims_for_files ──────────────────────────────────────────

def test_load_kb_claims_no_card_returns_empty(tmp_path):
    _init_repo(tmp_path)
    claims = load_kb_claims_for_files(tmp_path, tmp_path / ".pcp", ["src/nope.py"])
    assert claims == []


def test_load_kb_claims_string_and_dict_shapes(tmp_path):
    _init_repo(tmp_path)
    _write_card(
        tmp_path, "src/thing.py",
        ["Plain string claim", {"text": "Dict-with-text claim"}, {"claim": "Dict-with-claim claim"}],
    )
    claims = load_kb_claims_for_files(tmp_path, tmp_path / ".pcp", ["src/thing.py"])
    texts = [t for _p, t in claims]
    assert "Plain string claim" in texts
    assert "Dict-with-text claim" in texts
    assert "Dict-with-claim claim" in texts
    assert all(p == "src/thing.py" for p, _t in claims)


def test_load_kb_claims_empty_claims_list_contributes_nothing(tmp_path):
    """A card exists (A001 guarantees every file gets one) but claims are
    still empty -- A003 hasn't authored any yet. Not an error."""
    _init_repo(tmp_path)
    _write_card(tmp_path, "src/thing.py", [])
    claims = load_kb_claims_for_files(tmp_path, tmp_path / ".pcp", ["src/thing.py"])
    assert claims == []


def test_load_kb_claims_skips_malformed_card_yaml(tmp_path):
    _init_repo(tmp_path)
    from pcp.kb_file_metadata import card_path_for

    pcp_dir = tmp_path / ".pcp"
    card_path = card_path_for(pcp_dir, tmp_path, tmp_path / "src" / "thing.py")
    card_path.parent.mkdir(parents=True, exist_ok=True)
    card_path.write_text("not: valid: yaml: [")
    claims = load_kb_claims_for_files(tmp_path, pcp_dir, ["src/thing.py"])
    assert claims == []


def test_load_kb_claims_ignores_files_with_no_matching_card(tmp_path):
    _init_repo(tmp_path)
    _write_card(tmp_path, "src/thing.py", ["A real claim"])
    claims = load_kb_claims_for_files(tmp_path, tmp_path / ".pcp", ["src/other.py"])
    assert claims == []


# ── A016: check_kb_contradiction ─────────────────────────────────────────────

def test_no_claims_skips_llm_call_entirely(tmp_path):
    """Token Discipline: zero LLM calls when there's nothing to compare a
    diff against -- no card, or a card with an empty claims list."""
    _init_repo(tmp_path)
    rel = _stage_diff(tmp_path, "src/thing.py", "pass\n", "x = 1\n")
    with patch("pcp.llm.client.call_json") as mock_call:
        findings = check_kb_contradiction([rel], tmp_path, tmp_path / ".pcp")
    mock_call.assert_not_called()
    assert findings == []


def test_claims_exist_but_no_staged_diff_skips_llm_call(tmp_path):
    """A claim exists for the file, but nothing actually changed in the
    staged diff (e.g. re-added identical content) -- nothing to judge."""
    _init_repo(tmp_path)
    _write_card(tmp_path, "src/thing.py", ["Endpoint validates input server-side before use"])
    rel = _stage_diff(tmp_path, "src/thing.py", "pass\n", "pass\n")
    with patch("pcp.llm.client.call_json") as mock_call:
        findings = check_kb_contradiction([rel], tmp_path, tmp_path / ".pcp")
    mock_call.assert_not_called()
    assert findings == []


def test_judge_finding_survives_verifier_and_blocks(tmp_path):
    """The genuinely-blocking case: judge flags a real contradiction,
    adversarial verifier does NOT refute it -- the finding is returned,
    non-empty, meant to fail the criterion (same posture as CTRL-015)."""
    _init_repo(tmp_path)
    _write_card(tmp_path, "src/thing.py", ["Endpoint validates input server-side before use"])
    rel = _stage_diff(
        tmp_path, "src/thing.py",
        "def handle(req):\n    validate(req)\n    return req\n",
        "def handle(req):\n    return req\n",
    )
    judge_res = {"contradictions": [{"index": 0, "reason": "server-side validate() call was removed"}]}
    verify_res = {"verdicts": [{"index": 0, "refuted": False, "reason": "confirmed, validate() is gone"}]}
    with patch("pcp.llm.client.call_json", side_effect=[judge_res, verify_res]) as mock_call:
        findings = check_kb_contradiction([rel], tmp_path, tmp_path / ".pcp")
    assert mock_call.call_count == 2
    assert len(findings) == 1
    assert "validates input server-side" in findings[0]
    assert "validate() call was removed" in findings[0]


def test_verifier_refutes_finding_and_drops_it(tmp_path):
    """The adversarial second pass is real, not decorative: a refuted
    finding does not survive to the returned (blocking) result."""
    _init_repo(tmp_path)
    _write_card(tmp_path, "src/thing.py", ["Value is append-only"])
    rel = _stage_diff(tmp_path, "src/thing.py", "x = 1\n", "x = 2\n")
    judge_res = {"contradictions": [{"index": 0, "reason": "looks like a rename, not a real change"}]}
    verify_res = {"verdicts": [{"index": 0, "refuted": True, "reason": "purely cosmetic, no real contradiction"}]}
    with patch("pcp.llm.client.call_json", side_effect=[judge_res, verify_res]):
        findings = check_kb_contradiction([rel], tmp_path, tmp_path / ".pcp")
    assert findings == []


def test_judge_call_failure_fails_open(tmp_path):
    """An unreachable judge is an infra problem, not evidence of a real
    contradiction -- the one place this check is forgiving."""
    _init_repo(tmp_path)
    _write_card(tmp_path, "src/thing.py", ["A real claim"])
    rel = _stage_diff(tmp_path, "src/thing.py", "pass\n", "x = 1\n")
    with patch("pcp.llm.client.call_json", side_effect=RuntimeError("down")):
        findings = check_kb_contradiction([rel], tmp_path, tmp_path / ".pcp")
    assert findings == []


def test_verifier_call_failure_keeps_finding_unverified_and_still_blocking(tmp_path):
    """Fails OPEN on a verifier error: the raw judge finding is kept rather
    than silently dropped because the verifier itself broke -- a real
    contradiction slipping through a broken verifier would ship a defect,
    the same asymmetry build.py's own _verify_block_findings documents."""
    _init_repo(tmp_path)
    _write_card(tmp_path, "src/thing.py", ["A real claim"])
    rel = _stage_diff(tmp_path, "src/thing.py", "pass\n", "x = 1\n")
    judge_res = {"contradictions": [{"index": 0, "reason": "real contradiction"}]}
    with patch("pcp.llm.client.call_json", side_effect=[judge_res, RuntimeError("verifier down")]):
        findings = check_kb_contradiction([rel], tmp_path, tmp_path / ".pcp")
    assert len(findings) == 1
    assert "real contradiction" in findings[0]


def test_judge_returns_no_contradictions_skips_verifier_call(tmp_path):
    """No point running the adversarial verifier over zero findings --
    Token Discipline, same as _verify_block_findings' own early return."""
    _init_repo(tmp_path)
    _write_card(tmp_path, "src/thing.py", ["A real claim"])
    rel = _stage_diff(tmp_path, "src/thing.py", "pass\n", "x = 1\n")
    with patch("pcp.llm.client.call_json", return_value={"contradictions": []}) as mock_call:
        findings = check_kb_contradiction([rel], tmp_path, tmp_path / ".pcp")
    assert mock_call.call_count == 1
    assert findings == []
