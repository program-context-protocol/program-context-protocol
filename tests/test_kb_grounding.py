"""A015 — Tier-1 deterministic check: flags a diff touching a field/line an
existing spec.yaml constraint entry protects, with no accompanying
constraint-text update in the same diff. Zero LLM calls, pure regex + git diff.

A016 — Tier-6 LLM-judged check: compares a diff against loaded
kb/file_metadata content for non-mechanical contradictions and returns a
genuinely-blocking verdict, same posture as CTRL-015 (mocked judge calls
only, same convention as test_narrative_lint.py).

A013 — Grounding-gate context-package builder: assembles file metadata,
topic citations, known-issues hits, and blast radius (via graphify, when
installed) into one Build-Plan-style package per target file. Zero LLM
calls -- pure aggregation over data kb_file_metadata/kb_catalog/kb_index/
kb_ingest/coupling/impact already compute.

A014 — Disclosure-and-routing layer over A013's package: render_context_
package turns the package dict into agent-readable text without silently
dropping an empty (not-found) section; refresh_context_package writes that
text to a generated per-module projection routed through context_map.yaml's
own scenario table (kb_grounding_context, {module}-templated -- same
convention module_state already uses), and _build_agent_prompt (build.py)
wires it into a criterion's actual prompt when the criterion declares a
`target`.
"""

import subprocess
from unittest.mock import patch

import yaml

from pcp import context_map
from pcp.commands.build import _build_agent_prompt
from pcp.kb_grounding import (
    build_blast_radius_section,
    build_context_package,
    build_file_metadata_section,
    build_known_issues_section,
    build_topic_citations_section,
    check_constraint_protection,
    check_kb_contradiction,
    context_package_path,
    extract_protected_identifiers,
    load_constraint_index,
    load_kb_claims_for_files,
    refresh_context_package,
    render_context_package,
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


# ── A013 — grounding-gate context-package builder ───────────────────────────

def _write_module_dep(pcp_dir, name, deps=None, target=None):
    """Minimal module scaffold matching impact.py's own expectations:
    spec.yaml with dependencies, acceptance.yaml with one criterion
    declaring `target` (the declared-target attribution signal
    changed_files_to_modules reads)."""
    mod_dir = pcp_dir / "strategy" / "modules" / name
    mod_dir.mkdir(parents=True)
    (mod_dir / "spec.yaml").write_text(yaml.safe_dump({"dependencies": deps or []}))
    criteria = [{"id": "A001", "target": target}] if target else []
    (mod_dir / "acceptance.yaml").write_text(yaml.safe_dump({"criteria": criteria}))


# --- build_file_metadata_section ---------------------------------------------

def test_file_metadata_section_found_returns_card(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _write_card(tmp_path, "src/thing.py", ["A real claim"])

    section = build_file_metadata_section(pcp_dir, tmp_path, "src/thing.py")

    assert section["found"] is True
    assert section["card"]["card_version"] == 1
    assert section["card"]["claims"] == ["A real claim"]


def test_file_metadata_section_not_found_when_no_card(tmp_path):
    pcp_dir = tmp_path / ".pcp"

    section = build_file_metadata_section(pcp_dir, tmp_path, "src/missing.py")

    assert section == {"found": False, "card": None}


def test_file_metadata_section_disclosed_on_malformed_card(tmp_path):
    from pcp.kb_file_metadata import card_path_for

    pcp_dir = tmp_path / ".pcp"
    card_path = card_path_for(pcp_dir, tmp_path, tmp_path / "src/thing.py")
    card_path.parent.mkdir(parents=True, exist_ok=True)
    card_path.write_text("not: valid: yaml: [")

    section = build_file_metadata_section(pcp_dir, tmp_path, "src/thing.py")

    assert section["found"] is False
    assert section["card"] is None


# --- build_topic_citations_section -------------------------------------------

def test_topic_citations_section_found_extracts_headings(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    adr_dir = pcp_dir / "kb" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "ADR-001.md").write_text(
        "# ADR-001: thing.py rationale\n\n## Context\n\nSee src/thing.py.\n"
    )

    section = build_topic_citations_section(pcp_dir, tmp_path, "src/thing.py")

    assert section["found"] is True
    assert len(section["citations"]) == 1
    citation = section["citations"][0]
    assert citation["doc"].endswith("ADR-001.md")
    assert {"line": 1, "level": 1, "text": "ADR-001: thing.py rationale"} in citation["headings"]


def test_topic_citations_section_not_found_when_no_reference(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    domain_dir = pcp_dir / "kb" / "domain"
    domain_dir.mkdir(parents=True)
    (domain_dir / "general.md").write_text("# General\n\nNo filenames here.\n")

    section = build_topic_citations_section(pcp_dir, tmp_path, "src/thing.py")

    assert section == {"found": False, "citations": []}


# --- build_known_issues_section -----------------------------------------------

def test_known_issues_section_found_when_domain_doc_mentions_file(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    domain_dir = pcp_dir / "kb" / "domain"
    domain_dir.mkdir(parents=True)
    (domain_dir / "gotchas.md").write_text(
        "# Gotchas\n\nsrc/thing.py has a known race condition on init.\nUnrelated line.\n"
    )

    section = build_known_issues_section(pcp_dir, "src/thing.py")

    assert section["found"] is True
    assert len(section["hits"]) == 1
    assert section["hits"][0]["doc"] == "gotchas.md"
    assert any("race condition" in m for m in section["hits"][0]["matches"])


def test_known_issues_section_not_found_when_no_mention(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    domain_dir = pcp_dir / "kb" / "domain"
    domain_dir.mkdir(parents=True)
    (domain_dir / "gotchas.md").write_text("# Gotchas\n\nNothing about this file.\n")

    section = build_known_issues_section(pcp_dir, "src/thing.py")

    assert section == {"found": False, "hits": []}


def test_known_issues_section_no_kb_dir_returns_not_found(tmp_path):
    pcp_dir = tmp_path / ".pcp"

    section = build_known_issues_section(pcp_dir, "src/thing.py")

    assert section == {"found": False, "hits": []}


# --- build_blast_radius_section -----------------------------------------------

def test_blast_radius_section_reports_affected_modules(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _write_module_dep(pcp_dir, "auth", deps=[], target="src/auth/login.py")
    _write_module_dep(pcp_dir, "api", deps=["auth"], target="src/api/routes.py")

    section = build_blast_radius_section(pcp_dir, tmp_path, "src/auth/login.py")

    assert section["found"] is True
    assert section["owning_modules"] == ["auth"]
    assert section["affected_modules"] == ["api"]


def test_blast_radius_section_not_found_when_file_unattributed(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _write_module_dep(pcp_dir, "auth", deps=[], target="src/auth/login.py")

    section = build_blast_radius_section(pcp_dir, tmp_path, "src/unrelated/file.py")

    assert section["found"] is False
    assert section["owning_modules"] == []
    assert section["affected_modules"] == []


def test_blast_radius_section_not_found_when_no_modules(tmp_path):
    pcp_dir = tmp_path / ".pcp"

    section = build_blast_radius_section(pcp_dir, tmp_path, "src/thing.py")

    assert section["found"] is False


def test_blast_radius_graphify_communities_unavailable_without_graphify(tmp_path):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("graphify"):
            raise ImportError("no graphify")
        return real_import(name, *args, **kwargs)

    pcp_dir = tmp_path / ".pcp"
    _write_module_dep(pcp_dir, "auth", deps=[], target="src/auth/login.py")

    builtins.__import__ = fake_import
    try:
        section = build_blast_radius_section(pcp_dir, tmp_path, "src/auth/login.py")
    finally:
        builtins.__import__ = real_import

    assert section["graphify_communities"] == {"available": False}


# --- build_context_package -----------------------------------------------------

def test_build_context_package_assembles_all_four_sections(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _write_card(tmp_path, "src/thing.py", ["A real claim"])
    adr_dir = pcp_dir / "kb" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "ADR-001.md").write_text("# thing.py notes\n\nAbout src/thing.py.\n")
    domain_dir = pcp_dir / "kb" / "domain"
    domain_dir.mkdir(parents=True)
    (domain_dir / "gotchas.md").write_text("thing.py has a known issue.\n")
    _write_module_dep(pcp_dir, "core", deps=[], target="src/thing.py")

    package = build_context_package(tmp_path, pcp_dir, ["src/thing.py"])

    assert package["target_files"] == ["src/thing.py"]
    sections = package["files"]["src/thing.py"]
    assert set(sections) == {"file_metadata", "topic_citations", "known_issues", "blast_radius"}
    assert sections["file_metadata"]["found"] is True
    assert sections["topic_citations"]["found"] is True
    assert sections["known_issues"]["found"] is True
    assert sections["blast_radius"]["found"] is True
    assert "generated_at" in package


def test_build_context_package_discloses_not_found_independently(tmp_path):
    """A target file with none of the four kinds of grounding evidence gets
    an honest not-found disclosure in every section, not a silently empty
    package."""
    pcp_dir = tmp_path / ".pcp"

    package = build_context_package(tmp_path, pcp_dir, ["src/ungrounded.py"])

    sections = package["files"]["src/ungrounded.py"]
    assert sections["file_metadata"]["found"] is False
    assert sections["topic_citations"]["found"] is False
    assert sections["known_issues"]["found"] is False
    assert sections["blast_radius"]["found"] is False


def test_build_context_package_multiple_target_files_independent(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _write_card(tmp_path, "src/a.py", ["claim about a"])

    package = build_context_package(tmp_path, pcp_dir, ["src/a.py", "src/b.py"])

    assert package["target_files"] == ["src/a.py", "src/b.py"]
    assert package["files"]["src/a.py"]["file_metadata"]["found"] is True
    assert package["files"]["src/b.py"]["file_metadata"]["found"] is False


# ── A014 — render_context_package: disclosure must survive rendering ───────

def test_render_discloses_all_four_sections_even_when_nothing_found(tmp_path):
    """The actual failure mode A014 guards against: a naive renderer that
    only prints sections with real content would throw away A013's own
    not-found disclosure the moment it reaches an agent's prompt, even
    though build_context_package's own dict preserved it."""
    pcp_dir = tmp_path / ".pcp"
    package = build_context_package(tmp_path, pcp_dir, ["src/ungrounded.py"])

    rendered = render_context_package(package)

    for label in ("File metadata", "Topic citations", "Known issues", "Blast radius"):
        assert label in rendered
    assert rendered.count("NOT FOUND") == 4


def test_render_marks_found_sections_with_real_detail(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _write_card(tmp_path, "src/thing.py", ["A real claim"])

    package = build_context_package(tmp_path, pcp_dir, ["src/thing.py"])
    rendered = render_context_package(package)

    assert "File metadata" in rendered
    assert "FOUND" in rendered
    assert "A real claim" in rendered  # actual evidence surfaced, not just a flag


def test_render_multiple_target_files_each_get_their_own_disclosure(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _write_card(tmp_path, "src/a.py", ["claim about a"])

    package = build_context_package(tmp_path, pcp_dir, ["src/a.py", "src/b.py"])
    rendered = render_context_package(package)

    assert "src/a.py" in rendered
    assert "src/b.py" in rendered
    assert "claim about a" in rendered


def test_render_no_target_files_discloses_explicitly():
    package = {"target_files": [], "files": {}, "generated_at": "x"}
    rendered = render_context_package(package)
    assert "No target files" in rendered


# ── A014 — context_package_path / refresh_context_package ──────────────────

def test_context_package_path_matches_module_state_style_convention(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    path = context_package_path(pcp_dir, "auth")
    assert path == pcp_dir / "kb" / "context_packages" / "auth.md"


def test_refresh_context_package_writes_generated_file(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _write_card(tmp_path, "src/thing.py", ["A real claim"])

    written = refresh_context_package(tmp_path, pcp_dir, "auth", ["src/thing.py"])

    assert written == context_package_path(pcp_dir, "auth")
    assert written.exists()
    text = written.read_text()
    assert "src/thing.py" in text
    assert "A real claim" in text


def test_refresh_context_package_returns_none_without_target_files(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    written = refresh_context_package(tmp_path, pcp_dir, "auth", [])
    assert written is None
    assert not context_package_path(pcp_dir, "auth").exists()


def test_refresh_context_package_regenerates_fresh_not_append(tmp_path):
    """A GENERATED projection, same posture as module docs/built.md --
    regenerated fresh on every call, never a hand-maintained/accumulating
    log."""
    pcp_dir = tmp_path / ".pcp"
    refresh_context_package(tmp_path, pcp_dir, "auth", ["src/first.py"])
    written = refresh_context_package(tmp_path, pcp_dir, "auth", ["src/second.py"])

    text = written.read_text()
    assert "src/second.py" in text
    assert "src/first.py" not in text


# ── A014 — context_map.yaml-style routing ───────────────────────────────────

def test_kb_grounding_context_route_registered_in_default_routes():
    assert "kb_grounding_context" in context_map.DEFAULT_ROUTES
    route = context_map.DEFAULT_ROUTES["kb_grounding_context"]
    assert "{module}" in route["files"][0]


def test_context_map_resolves_generated_grounding_file(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    refresh_context_package(tmp_path, pcp_dir, "auth", ["src/thing.py"])

    files = context_map.resolve(pcp_dir, "kb_grounding_context", module="auth")
    assert files == [".pcp/kb/context_packages/auth.md"]


def test_agent_prompt_includes_grounding_context_when_target_declared(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _write_card(tmp_path, "src/thing.py", ["A real claim"])

    prompt = _build_agent_prompt(
        pcp_dir, "auth",
        {"id": "A1", "description": "x", "target": "src/thing.py"},
        {"name": "auth"},
    )

    assert "kb/context_packages/auth.md" in prompt
    written = context_package_path(pcp_dir, "auth")
    assert written.exists()
    assert "src/thing.py" in written.read_text()


def test_agent_prompt_omits_grounding_route_without_declared_target(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    prompt = _build_agent_prompt(pcp_dir, "auth", {"id": "A1", "description": "x"}, {"name": "auth"})
    assert "kb/context_packages" not in prompt
    assert not context_package_path(pcp_dir, "auth").exists()


def test_agent_prompt_grounding_injection_off_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("PCP_BUILD_INJECT_KB_GROUNDING", "0")
    pcp_dir = tmp_path / ".pcp"
    _write_card(tmp_path, "src/thing.py", ["A real claim"])

    prompt = _build_agent_prompt(
        pcp_dir, "auth",
        {"id": "A1", "description": "x", "target": "src/thing.py"},
        {"name": "auth"},
    )

    assert "kb/context_packages" not in prompt
    assert not context_package_path(pcp_dir, "auth").exists()
