"""check_prior_art_evidence -- closes the gap where CLAUDE.md's Prior-Art
Check rule (/priorart before building a non-trivial module) lived only in
doctrine, with no code path ever checking it happened. Same tier and
posture as check_capability_coverage/check_module_logic_breakdown_coverage:
deterministic, no LLM, advisory (flags, never blocks)."""

from pcp.commands.kickoff import check_prior_art_evidence


def test_flags_non_trivial_module_with_empty_candidates_considered():
    specs = {
        "sender-auth": {
            "description": "handles OAuth login for outbound senders",
            "build_vs_buy": {"decision": "build_fresh", "rationale": "x", "candidates_considered": []},
        }
    }
    findings = check_prior_art_evidence(specs)
    assert any("sender-auth" in f and "build_fresh" in f for f in findings)


def test_silent_when_candidates_considered_is_populated():
    specs = {
        "sender-auth": {
            "description": "handles OAuth login for outbound senders",
            "build_vs_buy": {
                "decision": "reuse_whole",
                "rationale": "x",
                "candidates_considered": ["authlib", "python-social-auth"],
            },
        }
    }
    assert check_prior_art_evidence(specs) == []


def test_silent_for_trivial_module_regardless_of_candidates_considered():
    specs = {
        "display-formatter": {
            "description": "formats currency values for display",
            "build_vs_buy": {"decision": "build_fresh", "rationale": "x", "candidates_considered": []},
        }
    }
    assert check_prior_art_evidence(specs) == []


def test_silent_when_module_level_build_vs_buy_is_not_applicable():
    specs = {
        "payment-summary": {
            "description": "pure business-logic summary of payment records",
            "build_vs_buy": {"decision": "not_applicable", "rationale": "x", "candidates_considered": []},
        }
    }
    assert check_prior_art_evidence(specs) == []


def test_silent_when_build_vs_buy_missing_entirely():
    specs = {"payment-gateway": {"description": "handles payment processing"}}
    assert check_prior_art_evidence(specs) == []


def test_matches_ui_subsystem_keywords():
    specs = {
        "flow-builder": {
            "description": "a drag-drop canvas for building automation flows",
            "build_vs_buy": {"decision": "build_fresh", "rationale": "x", "candidates_considered": []},
        }
    }
    findings = check_prior_art_evidence(specs)
    assert any("flow-builder" in f for f in findings)
