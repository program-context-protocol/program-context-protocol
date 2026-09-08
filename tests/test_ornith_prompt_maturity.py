"""Real coverage for the 2026-09-07 "mature opencode prompting" work:
standing Ornith conventions move to a project's own AGENTS.md (opencode
reads it natively, once per session) instead of being re-appended into
every `--prompt` argument, and the per-criterion task message states an
explicit goal + definition of done (opencode's own documented prompting
guidance) instead of leaving what determines "done" implicit.

Zero existing tests covered `_run_local_llm_attempt`/`local_llm_build`-
related code before this session (confirmed by grep) -- this closes that
gap for the three pieces that landed today, rather than leaving them
untested plumbing."""

import inspect

from pcp.commands.build import (
    _build_agent_prompt,
    _build_one_criterion,
    _definition_of_done_line,
    _ornith_standing_conventions_available,
)
from pcp.commands.init import (
    PCP_AGENTS_BLOCK_START,
    PCP_AGENTS_BLOCK_END,
    upsert_pcp_agents_md_block,
)


# ── _definition_of_done_line ────────────────────────────────────────────

def test_file_exists_names_the_real_target():
    line = _definition_of_done_line({"check": "file_exists", "target": "src/x.py"})
    assert line == "Definition of done: `src/x.py` must exist."


def test_ast_pattern_names_target_and_pattern():
    line = _definition_of_done_line({"check": "ast_pattern", "target": "src/x.py", "pattern": "def foo"})
    assert "src/x.py" in line and "def foo" in line


def test_test_passes_states_real_suite_requirement():
    line = _definition_of_done_line({"check": "test_passes"})
    assert "test suite" in line and "pass" in line


def test_dom_contains_includes_selector_when_present():
    line = _definition_of_done_line({"check": "dom_contains", "selector": ".foo"})
    assert ".foo" in line


def test_url_responds_includes_url_when_present():
    line = _definition_of_done_line({"check": "url_responds", "url": "/health"})
    assert "/health" in line


def test_visual_check_is_named():
    line = _definition_of_done_line({"check": "visual"})
    assert "screenshot" in line


def test_manual_or_missing_check_falls_back_honestly():
    """Never claim a deterministic check exists when none was declared."""
    for criterion in [{"check": "manual"}, {}]:
        line = _definition_of_done_line(criterion)
        assert "no deterministic check is declared" in line


def test_every_case_starts_with_the_same_prefix():
    """A consistent prefix is what makes this scannable as a distinct
    'definition of done' statement rather than blending into surrounding text."""
    for check in ["file_exists", "ast_pattern", "test_passes", "dom_contains", "url_responds", "visual", "manual"]:
        assert _definition_of_done_line({"check": check}).startswith("Definition of done:")


def test_definition_of_done_line_is_wired_into_the_real_prompt(tmp_path):
    """Not just a standalone function -- must actually appear in what
    _build_agent_prompt produces for both Claude and Ornith attempts."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    criterion = {"id": "A001", "description": "does a thing", "check": "file_exists", "target": "src/x.py"}
    prompt = _build_agent_prompt(pcp_dir, "billing", criterion, {"module": "billing"})
    assert "Definition of done: `src/x.py` must exist." in prompt


# ── _ornith_standing_conventions_available ──────────────────────────────

def test_unavailable_when_agents_md_missing(tmp_path):
    assert _ornith_standing_conventions_available(tmp_path) is False


def test_unavailable_when_agents_md_present_but_lacks_pcp_marker(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Some other tool's file\nNo PCP section here.\n")
    assert _ornith_standing_conventions_available(tmp_path) is False


def test_available_once_pcp_marker_present(tmp_path):
    upsert_pcp_agents_md_block(tmp_path / "AGENTS.md")
    assert _ornith_standing_conventions_available(tmp_path) is True


# ── init.py's upsert_pcp_agents_md_block ────────────────────────────────

def test_creates_agents_md_with_marker_pair(tmp_path):
    path = tmp_path / "AGENTS.md"
    changed = upsert_pcp_agents_md_block(path)
    assert changed is True
    content = path.read_text()
    assert PCP_AGENTS_BLOCK_START in content and PCP_AGENTS_BLOCK_END in content


def test_idempotent_second_call_reports_no_change(tmp_path):
    path = tmp_path / "AGENTS.md"
    upsert_pcp_agents_md_block(path)
    changed_again = upsert_pcp_agents_md_block(path)
    assert changed_again is False


def test_preserves_human_authored_content_outside_the_marker(tmp_path):
    path = tmp_path / "AGENTS.md"
    upsert_pcp_agents_md_block(path)
    with open(path, "a") as f:
        f.write("\n\n# Human notes\nDo not remove this.\n")
    upsert_pcp_agents_md_block(path)  # re-run, e.g. `pcp init --force`
    content = path.read_text()
    assert "Human notes" in content
    assert content.count(PCP_AGENTS_BLOCK_START) == 1, "marker must not duplicate on re-run"


def test_real_content_covers_run_shell_module_label_and_shadcn_notes(tmp_path):
    """The whole point of the migration -- the standing conventions that used
    to live inline in build.py's per-call prompt must actually be present
    here, not just an empty/placeholder block."""
    path = tmp_path / "AGENTS.md"
    upsert_pcp_agents_md_block(path)
    content = path.read_text()
    assert "run_shell" in content
    assert "not a Python package name" in content
    assert "shadcn" in content


# ── Structural guard: the two note-injection sites are actually gated ───

def test_local_environment_notes_are_gated_behind_the_availability_check():
    """Regression guard, same style as test_coding_agent_contract.py's
    inspect.getsource() checks -- if a future edit removes the gate and goes
    back to unconditionally injecting the standing notes, this fails loudly
    instead of silently reintroducing the per-call cost this work removed."""
    src = inspect.getsource(_build_one_criterion)
    assert src.count("_ornith_standing_conventions_available(project_root)") == 2, (
        "expected exactly 2 call sites: the general local-environment note and the "
        "UI-facing shadcn note -- both must be gated"
    )
