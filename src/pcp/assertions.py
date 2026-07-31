"""Deterministic objective-coverage scoring — replaces the LLM-judged
coverage_score with keyword-overlap graph reachability when possible,
mirroring how coupling.py already replaced the LLM-judged coupling_score
with networkx graph math (same pioneer-claim, same Logic-Tier Selection
philosophy: cheapest rung that correctly makes the decision).

coverage_score is the one LLM-judged number CLAUDE.md's own coverage_audit.py
section flags as a real Goodhart risk — a spec can be word-smithed to score
well without real coverage, and coverage_audit.py only ever detects the
resulting inconsistency/drift after the fact, it can't prevent it. This is a
genuinely different mitigation: score numbered assertions parsed straight
from objective.md's own text against each module's declared
objective_coverage via keyword overlap — zero LLM calls, zero flakiness,
immune to word-smithing the way a semantic judgment call isn't.

Deliberately not a replacement for the LLM's judgment across the board:
contradictions/overlaps/missing_modules stay genuinely semantic and keep
going through validate_strategy.py's LLM call unchanged — only
coverage_score/coverage_gaps get the deterministic treatment here, the same
narrow scope coupling.py's own history establishes (see CLAUDE.md's
"Updated 2026-06-30" coupling entry).

Honest limitation, stated rather than hidden: "does this module's free-text
objective_coverage actually, semantically cover this assertion" is still a
real judgment call that keyword overlap can get wrong in both directions —
false positive (shared vocabulary, no real coverage) and false negative
(real coverage, different words). Same class of approximation
docs.py's _brd_keywords() already accepts for BRD-item matching — a rung-1
heuristic, not ground truth, and it says so rather than pretending
precision it doesn't have.
"""

import re

_ASSERTION_PATTERN = re.compile(r"^[ \t]*(\d+)\.[ \t]+(.+)$", re.MULTILINE)
_KEYWORD_PATTERN = re.compile(r"[a-zA-Z]{5,}")
_OVERLAP_THRESHOLD = 0.25  # fraction of an assertion's own keywords that must appear in a module's coverage text


def parse_assertions(objective_text: str) -> list[dict]:
    """Numbered list items anywhere in objective.md — e.g. the numbered
    outcomes under a '## What Success Looks Like' heading, the exact shape
    `pcp kickoff`'s own SYSTEM_PROMPT already generates for every new
    project. Returns [] if the document has no numbered list at all (every
    pre-existing objective.md written before this convention existed) —
    callers MUST treat an empty list as "cannot score deterministically
    yet", not as "zero assertions found", and fall back to the LLM-judged
    score. Never hard-breaks on an old-format file."""
    assertions = []
    for i, m in enumerate(_ASSERTION_PATTERN.finditer(objective_text), 1):
        text = m.group(2).strip()
        if text:
            assertions.append({"id": f"A{i}", "text": text})
    return assertions


def _keywords(text: str) -> set[str]:
    return {w.lower() for w in _KEYWORD_PATTERN.findall(text)}


def _covers(assertion_text: str, coverage_text: str) -> bool:
    a_kw = _keywords(assertion_text)
    if not a_kw:
        return False
    c_kw = _keywords(coverage_text)
    return len(a_kw & c_kw) / len(a_kw) >= _OVERLAP_THRESHOLD


def compute_coverage(assertions: list[dict], modules: dict[str, dict]) -> dict:
    """Deterministic reachability: an assertion counts as covered if at
    least one module's objective_coverage list has keyword overlap with its
    text above _OVERLAP_THRESHOLD. Returns the same coverage_score/
    coverage_gaps shape the LLM-judged path already returns, so callers
    don't need to branch on which path produced the result — only the
    scoring_method field distinguishes them."""
    gaps = []
    covered_by: dict[str, list[str]] = {}
    for a in assertions:
        covering_modules = sorted(
            name for name, spec in modules.items()
            if any(_covers(a["text"], cov) for cov in (spec.get("objective_coverage") or []))
        )
        if covering_modules:
            covered_by[a["id"]] = covering_modules
        else:
            gaps.append({"area": a["text"], "quote": a["text"]})

    score = round(len(covered_by) / len(assertions), 2) if assertions else 0.0
    return {
        "coverage_score": score,
        "coverage_gaps": gaps,
        "assertions_total": len(assertions),
        "assertions_covered": len(covered_by),
        "assertion_coverage_map": covered_by,
    }


# Confidence floor above which the LLM's own coverage judgment is trusted
# enough that a much lower deterministic score reads as scorer disagreement
# rather than as real missing coverage.
LLM_CONFIDENCE_FLOOR = 0.85

# Below this, the deterministic scorer failed to match a MAJORITY of the
# objective's assertions, which indicates the keyword heuristic does not work
# against this objective's phrasing at all — not that coverage is genuinely
# absent. At or above it, individual misses are credible and still block.
SCORER_CREDIBILITY_FLOOR = 0.5


def scorers_disagree(result: dict, floor: float = LLM_CONFIDENCE_FLOOR,
                     credibility_floor: float = SCORER_CREDIBILITY_FLOOR) -> bool:
    """True when the deterministic score contradicts a confident LLM score.

    Keyword overlap is explicitly "not ground truth" (see this module's own
    docstring): it has systematic false negatives whenever an objective's
    numbered list is phrased in different vocabulary from the modules'
    `objective_coverage`. Measured 2026-07-27 across four real projects, the
    deterministic score ranged 0%-100% on healthy decompositions — Project P
    scored 0/5 with 11 real modules because its assertions are terse feature
    names, and Project S scored 1/4 because two of its assertions are
    non-functional outcomes ("the sender never has to explain it") that no
    module coverage text will ever share words with.

    Two scorers disagreeing is an uncertainty signal, not a verdict. This
    lives here, callable, because the rule previously existed ONLY inside
    build.py's wave gate: `pcp build` correctly treated the disagreement as
    advisory while standalone `pcp validate-strategy` exited 1 on the very
    same data. One rule, one definition, both callers.
    """
    llm_score = result.get("llm_coverage_score")
    det_score = result.get("coverage_score", 0)
    return (
        result.get("scoring_method") == "deterministic"
        and llm_score is not None
        and llm_score >= floor
        and det_score < llm_score
        # ...and the deterministic scorer must look BROKEN on this objective,
        # not merely stricter. Vocabulary mismatch is systematic: it fails to
        # match assertions uniformly and drives the score toward zero (Project P
        # 0/5, Project S 1/4). A genuine missing module is localised — most
        # assertions still match and one does not (9/10, or the 1/2 case in
        # test_deterministic_gap_shown_in_cli_output, a real uncovered
        # module the LLM claimed was fine).
        #
        # Without this clause the rule would have disarmed the very Goodhart
        # mitigation the deterministic scorer exists to provide: any gap the
        # LLM word-smithed past would have been waved through as
        # "disagreement". Below-majority means the scorer is not working here
        # and cannot be trusted to BLOCK; at or above majority its misses are
        # credible and still fail the check.
        and det_score < credibility_floor
    )
