"""A rule must never be evaluated against PCP's own generated output.

Project O, 2026-07-30, from bypass_log.yaml:

    reason: R008 matched its own rule text quoted inside generated
            telemetry.jsonl, not a real property_hints persistence

telemetry records the findings of the rules, so a rule's pattern text is written
into the very file the next commit stages and scans. And a [pcp-bypass] is
all-or-nothing across rules, so that one self-match bypassed R001-R010 together,
unattended, with nobody reading it.
"""
from pcp.operational import filter_operational, is_operational


def test_the_file_that_caused_the_incident_is_operational():
    assert is_operational(".pcp/telemetry.jsonl") is True


def test_every_generated_record_pcp_writes_during_a_run_is_operational():
    for p in (".pcp/token_ledger.yaml", ".pcp/decision_log.jsonl", ".pcp/current_state.md",
              ".pcp/diff.md", ".pcp/build_progress.yaml", ".pcp/bypass_log.yaml",
              ".pcp/symbol_fingerprints.json", ".pcp/brd.md", ".pcp/brd_items.yaml",
              ".pcp/escalations.yaml", ".pcp/audit.md", ".pcp/provenance.md", "pcp.md"):
        assert is_operational(p) is True, p


def test_evidence_and_transcript_trees_are_operational():
    assert is_operational(".pcp/evidence/query-eval-harness/A001/attempt_1/gate.txt") is True
    assert is_operational(".pcp/transcripts/whatever.gz") is True


def test_authored_content_is_never_operational():
    """Specs and source are gated as before — this must not become a hole."""
    for p in ("src/modules/query_eval_harness/__init__.py",
              "tests/query_eval_harness/test_a001.py",
              ".pcp/objective.md", ".pcp/ci_rules.yaml", ".pcp/controls.yaml",
              ".pcp/strategy/modules/core/spec.yaml",
              ".pcp/strategy/modules/core/acceptance.yaml"):
        assert is_operational(p) is False, p


def test_leading_dot_slash_and_backslashes_normalise():
    assert is_operational("./.pcp/telemetry.jsonl") is True
    assert is_operational(".pcp\\telemetry.jsonl") is True


def test_filter_returns_both_halves_so_nothing_is_dropped_silently():
    keep, skipped = filter_operational([
        "src/a.py", ".pcp/telemetry.jsonl", ".pcp/objective.md",
        ".pcp/evidence/m/A1/attempt_1/lint.txt",
    ])
    assert keep == ["src/a.py", ".pcp/objective.md"]
    assert skipped == [".pcp/telemetry.jsonl", ".pcp/evidence/m/A1/attempt_1/lint.txt"]


def test_the_incident_commit_shape_leaves_only_real_content_to_gate():
    """The actual bypassed commit was overwhelmingly PCP's own output."""
    staged = [
        ".pcp/bypass_log.yaml", ".pcp/current_state.md", ".pcp/diff.md",
        ".pcp/symbol_fingerprints.json", ".pcp/telemetry.jsonl", "pcp.md",
        "src/modules/query_eval_harness/__init__.py",
    ]
    keep, skipped = filter_operational(staged)
    assert keep == ["src/modules/query_eval_harness/__init__.py"]
    assert len(skipped) == 6
