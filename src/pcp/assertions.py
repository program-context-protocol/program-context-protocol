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
