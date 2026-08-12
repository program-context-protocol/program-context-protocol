"""Auto-triggered cross-model adversarial review for native/IPC-bridging
criteria (CTRL-042, 2026-08-09).

Real gap this closes, named directly in the win2mac/pisco-sour-wre
dogfood session (see memory `project_pcp_learnings_from_win2mac_debug_2026_08_09`):
a self-testing build agent verifies its own code against its own test
harness using its own deployment/execution assumptions. If that assumption
is wrong (wrong socket lookup, wrong file resolution, wrong search order —
schannel's case: the deployment method never reached the code it claimed
to test, confirmed in the build agent's own words), every result downstream
looks internally consistent, because nothing INSIDE that loop can reveal
the loop itself is the problem. It took a genuinely independently-derived
test path to expose it. CTRL-041 (`_run_adversarial_review`, build.py)
already runs a separate, adversarially-framed, tool-using agent session --
but only on a criterion's own explicit `adversarial_review: true` opt-in.
This module adds the missing piece: a DETERMINISTIC auto-trigger for the
exact class of criterion where a self-verification loop is most likely to
be silently wrong (native/IPC/ABI boundary code), plus a genuinely
cross-vendor (not just cross-model-same-vendor) second opinion via the
existing agy harness -- CTRL-041's reviewer is pinned to Claude
(ESCALATION_MODEL), so it shares Claude's blind spots; this doesn't.

Scope, stated honestly: this is a TEXT-based cross-vendor review (agy has
no agentic tool-use loop in this codebase's harness -- see
llm/harness/agy.py, a single prompt/response call), not a rebuild-and-run
re-execution of the artifact. It catches design/logic-level red flags an
independent reader would raise, not runtime-execution bugs that only
surface by actually running the code (the kind found manually this session
-- those still require a real tool-using verification pass, which is
CTRL-041's job when opted in, or a human debugging session like this one).
Not a silent gap: said here, in the CTRL-042 catalog entry, and in
`run_cross_model_review`'s own docstring.
"""

import os

from pcp.llm import client as llm

NATIVE_BRIDGE_MARKERS = (
    "WINE_UNIX_CALL",
    "__wine_unix_call",
    "dlopen(",
    "#pragma makedep unix",
    "AF_UNIX",
    "AF_INET",
)


def has_native_bridge_pattern(content: str) -> bool:
    """Deterministic, rung 1 -- a fixed marker list, not a judgment call.
    Same posture as `ast_grep_swallowed_exceptions`/`lazy_marker` elsewhere
    in this codebase: a structural pattern check on the target file's own
    text, zero LLM cost."""
    return any(marker in content for marker in NATIVE_BRIDGE_MARKERS)


def should_auto_verify(criterion: dict, target_content: str) -> bool:
    """The trigger condition: `logic_tier: 1` (this criterion claims to be
    pure deterministic code, no judgment involved) AND the target file
    actually crosses a native/IPC/ABI boundary. That combination is exactly
    where a self-verification loop's own deployment/execution assumptions
    are both load-bearing AND invisible to the loop itself -- a rung-1
    criterion is never expected to need adversarial judgment for its LOGIC,
    so if it also touches a native bridge, the risk isn't logic correctness,
    it's "does my own test harness actually reach the code it claims to.\""""
    return criterion.get("logic_tier") == 1 and has_native_bridge_pattern(target_content)


CROSS_MODEL_REVIEW_SYSTEM_PROMPT = """You are an independent, adversarial verifier reviewing a \
criterion completed by a DIFFERENT AI system. Your only job is to find reasons this does NOT \
actually work -- not to confirm it does. Default to suspicion.

You do not have access to the original implementer's test harness, evidence, or deployment \
setup -- only the criterion description and the diff. Reason from first principles about \
whether the described mechanism (a native/IPC/ABI boundary: raw sockets, dlopen, a cross-ABI \
call, or similar) could plausibly work as described, or whether the diff shows a deployment \
assumption, resolution order, or boundary-crossing detail that looks likely to be wrong \
even though the code "looks" complete.

Respond with JSON only (no markdown fences):
{"is_real": true or false, "confidence": 0.0 to 1.0, "red_flags": ["specific, evidence-based concern citing the diff"], "reasoning": "2-4 sentences"}
"""


def _cross_model_confidence_floor() -> float:
    return float(os.environ.get("PCP_NATIVE_BRIDGE_CONFIDENCE_FLOOR", "0.5"))


def run_cross_model_review(pcp_dir, mod_name: str, criterion: dict, diff: str) -> list[str]:
    """One cross-vendor (agy, not Claude) text-based adversarial pass over a
    native-bridging criterion's diff. Fails OPEN on any error (timeout, bad
    JSON, agy not installed) -- same asymmetry as `_verify_block_findings`:
    a missed real problem is worse than an occasional wasted look, but this
    check existing at all is worthless if it routinely blocks builds when
    the cross-vendor tool simply isn't available. Returns red_flags (empty
    list = no dispute) rather than raising, so a caller can treat this
    exactly like any other advisory finding source.

    Text-based only -- see this module's docstring for why that's a stated
    scope limit, not silently implied to be equivalent to CTRL-041's
    tool-using, execution-based review."""
    prompt = (
        f"## Criterion under review ({mod_name}/{criterion.get('id', '?')})\n"
        f"{criterion.get('description', '')}\n\n"
        f"## Diff\n```diff\n{diff[:14000]}\n```\n"
    )
    try:
        res = llm.call_json_agy(
            CROSS_MODEL_REVIEW_SYSTEM_PROMPT, prompt, pcp_dir=pcp_dir,
            command="build-native-bridge-cross-model-review",
        )
    except Exception:
        return []

    if not isinstance(res, dict):
        return []
    if res.get("is_real") is True and float(res.get("confidence") or 0) >= _cross_model_confidence_floor():
        return []
    flags = res.get("red_flags")
    return [f for f in flags if isinstance(f, str)] if isinstance(flags, list) else []
