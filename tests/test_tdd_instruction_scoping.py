"""A criterion PCP already verifies must not also get a pytest asserting it.

Measured on ontology-foundry 2026-07-30: `test_interface_file_exists` and
`test_feature_flag_file_exists` each appear 20 times — once per module — asserting
only that a path exists, which acceptance.yaml already declares as a machine-checked
`file_exists`. One of the generated files says so in its own docstring: "Mirrors the
file_exists check declared in .pcp/strategy/modules/.../acceptance.yaml."

Not free: core-data-model's blast radius is 99% of the suite, so these run on
essentially every scoped test run — on a suite that hit the 900s timeout 3x that day.
"""
from pcp.commands.build import _tdd_instruction


def test_manual_criterion_still_gets_plain_tdd():
    out = _tdd_instruction({"id": "A001", "check": "manual"})
    assert "write a failing test for this criterion first" in out
    assert "Do NOT write a test" not in out


def test_test_passes_criterion_still_gets_plain_tdd():
    """A criterion whose check IS a test obviously still wants the test."""
    out = _tdd_instruction({"id": "A002", "check": "test_passes"})
    assert "write a failing test for this criterion first" in out


def test_file_exists_criterion_is_told_not_to_duplicate_the_check():
    out = _tdd_instruction({"id": "MOD_A003", "check": "file_exists"})
    assert "Do NOT write a test that asserts the file exists" in out
    assert "deterministically" in out


def test_ast_pattern_criterion_is_told_not_to_duplicate_the_check():
    out = _tdd_instruction({"id": "MOD_A002", "check": "ast_pattern"})
    assert "Do NOT write a test that asserts the pattern is present" in out


def test_behaviour_testing_is_still_demanded_not_waived():
    """The point is to drop existence assertions, never to drop testing."""
    out = _tdd_instruction({"id": "MOD_A003", "check": "file_exists"})
    assert "Write tests only for BEHAVIOUR" in out
    assert "what the file" in out and "must contain" in out
    assert "Follow TDD for whatever behavioural tests you do write" in out


def test_every_variant_keeps_the_qa_gate_warning():
    for check in ("manual", "file_exists", "ast_pattern", "test_passes", ""):
        out = _tdd_instruction({"id": "X", "check": check})
        assert "SAST/secret scan" in out, check


def test_missing_or_none_check_falls_back_to_plain_tdd():
    for crit in ({"id": "X"}, {"id": "X", "check": None}, {"id": "X", "check": "  "}):
        assert "write a failing test for this criterion first" in _tdd_instruction(crit)


def test_instruction_is_reachable_from_the_real_prompt(tmp_path):
    """Guards the wiring, not just the helper."""
    import yaml
    from pcp.commands.build import _build_agent_prompt
    pcp_dir = tmp_path / ".pcp"
    (pcp_dir / "strategy" / "modules" / "billing").mkdir(parents=True)
    (pcp_dir / "objective.md").write_text("# obj")
    prompt = _build_agent_prompt(
        pcp_dir, "billing",
        {"id": "MOD_A003", "description": "feature flag exists", "check": "file_exists",
         "target": "src/modules/billing/feature_flag.env"},
        {"module": "billing", "description": "d"},
    )
    assert "Do NOT write a test that asserts the file exists" in prompt
