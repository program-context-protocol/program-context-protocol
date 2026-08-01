"""LLM client — common dispatch layer over per-vendor harness implementations.

Token discipline is a hard constraint, same tier as modularity (see CLAUDE.md).
Every call site must pass an explicit `model` — judge/advisory calls route to
Haiku by default; PCP_MODEL env always wins if a human sets it. Usage/cost is
captured (where the harness exposes it) and logged to .pcp/token_ledger.yaml
so spend is visible the same way coverage_score and coupling_score are.

Repo split (2026-07-31): this file is the COMMON half — dispatch, retry-on-
bad-JSON, model routing constants, none of it caring which vendor answered.
The actual per-vendor implementations live in llm/harness/ (claude.py,
agy.py, ...) — see that package's docstring for the contract a new harness
implements. `_log_usage`/token-ledger writing lives in llm/ledger.py,
independent of both this file and any harness/*.py, specifically to avoid a
circular import between the dispatcher and the harnesses it dispatches to.

Everything here re-exports what harness/claude.py and harness/agy.py define,
so existing `from pcp.llm.client import _claude_bin` / `llm.call_with_images`
/ patch("pcp.llm.client.call_json_with_images") call sites and tests are
unaffected by the split — only tests that patch subprocess.run directly
needed updating, to the module that actually owns the subprocess call now
(llm.harness.claude / llm.harness.agy).

Scope, stated honestly: this file (and the harness/ split under it) covers
PCP's JUDGE/GENERATION calls only. It does NOT cover `pcp build`'s own
coding-agent loop -- the part that actually writes code (commands/build.py,
worktree-per-criterion, `--resume`-based retry) is a separate, deeper
subprocess integration this seam does not touch. Porting THAT to a
different harness (Codex, or promoting agy beyond its current verifier-
only role) is real, additional work, and for agy specifically also runs
past this repo's own CLAUDE.md scoping of agy to research/QA/analysis, not
code-writing -- a policy question, not just a technical one, worth a human
decision before extending it there.
"""

import json
import os
from pathlib import Path
from typing import Any

from pcp.llm.harness.claude import (
    _claude_bin, _timeout, _call_claude,
    call_with_images, call_with_image, call_json_with_images, call_json_with_image,
    _MEDIA_TYPES,
)
from pcp.llm.harness.agy import _agy_bin, _agy_timeout, _call_agy
from pcp.llm.ledger import _LEDGER_LOCK, _log_usage

JUDGE_MODEL = "haiku"
# Model-selection strategy (reviewed and approved 2026-07-17) -- same
# cheapest-tool-that-correctly-does-the-job philosophy as the Logic-Tier
# Selection ladder (CLAUDE.md), applied to PCP's own LLM call sites instead
# of to the projects PCP builds:
#   Haiku    -- bounded, structured judge calls (JUDGE_MODEL, unchanged)
#   Sonnet   -- pcp build's coding agent + kickoff/pm generation (BUILD_MODEL)
#   Opus     -- escalation only: 3rd/final build-criterion attempt, and
#               wave-level architect-review (ESCALATION_MODEL) -- both have
#               a materially higher blast radius than a per-criterion Haiku
#               check, worth paying up for
#   Fable 5  -- never a default anywhere in this file; PCP_BUILD_MODEL is
#               the only path to it, a PM's explicit, deliberate override.
#               Its always-on-thinking/minutes-long-turn profile conflicts
#               with Token Discipline as a default for anything here.
BUILD_MODEL = "sonnet"
ESCALATION_MODEL = "opus"


# ── Harness seam ────────────────────────────────────────────────────────
# call()/call_json() below route through a per-vendor implementation chosen
# by `harness` (default "claude", PCP_LLM_HARNESS env overrides). This is
# the plug point for a future harness: implement _call_<name>() in a new
# llm/harness/<name>.py with the same contract as _call_claude()/_call_agy()
# (returns text, or (text, meta) when return_meta=True; raises RuntimeError
# on a CLI-level failure), add it to SUPPORTED_HARNESSES and
# _HARNESS_IMPLS, done -- call_json()'s retry-on-bad-JSON logic and every
# judge/generation call site (kickoff/pm/gate/architect-review/build's
# _verify_block_findings) work unchanged, they never see which harness
# actually answered.

SUPPORTED_HARNESSES = ("claude", "agy")
_HARNESS_IMPLS = {"claude": _call_claude, "agy": _call_agy}


def _resolve_harness(harness: str | None) -> str:
    """PCP_LLM_HARNESS env always wins, same override precedence PCP_MODEL
    already has for models -- a human forcing a harness for debugging
    shouldn't need to edit every call site."""
    h = os.environ.get("PCP_LLM_HARNESS") or harness or "claude"
    if h not in SUPPORTED_HARNESSES:
        raise ValueError(f"Unknown harness '{h}' -- one of {SUPPORTED_HARNESSES}.")
    return h


def call(system: str, user: str, model: str | None = None, pcp_dir: Path | None = None,
          command: str = "llm.call", return_meta: bool = False,
          harness: str | None = None) -> str | tuple[str, dict]:
    """Dispatches to the resolved harness's implementation. See the module
    docstring for the seam's scope and contract. Existing call sites that
    never pass `harness` are unaffected -- this resolves to "claude",
    exactly the prior hardcoded behavior."""
    impl = _HARNESS_IMPLS[_resolve_harness(harness)]
    return impl(system, user, model=model, pcp_dir=pcp_dir, command=command, return_meta=return_meta)


def _json_retries() -> int:
    return max(0, int(os.environ.get("PCP_LLM_JSON_RETRIES", "2")))


def call_json(system: str, user: str, model: str | None = None, pcp_dir: Path | None = None,
              command: str = "llm.call_json", return_meta: bool = False,
              harness: str | None = None) -> Any:
    """Call the resolved harness, parse response as JSON, retrying a
    malformed response. Harness-agnostic by construction -- it only calls
    call() and parses text, so this same retry logic now covers every
    harness in SUPPORTED_HARNESSES without duplicating it per-vendor (agy's
    JSON-retry loop used to be a separate near-identical copy of this one;
    unified 2026-07-31).

    A response that is not parseable JSON is the single most retryable failure
    an LLM call has: the model answered, it just answered in the wrong shape.
    Asking again is the rung-1 response.

    Without a retry, one transient `Extra data: line 15 column 1` propagated as
    an exception, became a blocking gate finding, consumed a criterion attempt,
    and after three of them the remedy PCP offered was
    PCP_ALLOW_UNVERIFIED_GATES=1 -- turn the gate off. Reported from
    Project O 2026-07-27, where it cost three attempts on one criterion
    and where the same architect review had caught a real path-traversal
    vulnerability an hour earlier. Offering "skip the check" as the cure for a
    flaky check points at exactly the wrong lever.

    Retries ONLY on a JSON parse failure. A RuntimeError from the CLI (rate
    limit, timeout, not authenticated) is a different condition with its own
    handling -- retrying that here would just multiply the wait.
    """
    prompt = user + "\n\nRespond with valid JSON only. No markdown fences."
    attempts = _json_retries() + 1
    last_exc: Exception | None = None

    for attempt in range(attempts):
        out = call(
            system, prompt, model=model, pcp_dir=pcp_dir, command=command,
            return_meta=return_meta, harness=harness,
        )
        text, meta = out if return_meta else (out, None)
        text = text.strip()
        # Strip markdown fences if model adds them anyway
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1])
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            last_exc = exc
            if attempt + 1 >= attempts:
                break
            # Say what went wrong -- a blind re-ask tends to reproduce the same
            # malformed shape.
            prompt = (
                user
                + "\n\nYour previous response could not be parsed as JSON: "
                + f"{exc}. Respond with ONE valid JSON object and nothing else -- "
                + "no prose before or after it, no markdown fences."
            )
            continue
        return (parsed, meta) if return_meta else parsed

    raise ValueError(
        f"{command}: response was not valid JSON after {attempts} attempt(s): {last_exc}"
    )


def call_json_agy(system: str, user: str, pcp_dir: Path | None = None,
                   command: str = "llm.call_json_agy") -> Any:
    """Backward-compatible convenience wrapper -- call_json(..., harness="agy").
    Kept as a named function since Loop 3's cross-vendor verifier leg
    (build.py's _verify_block_findings, proposed 2026-07-22, resumed
    2026-07-31 -- see memory `project-cross-vendor-verifier-deferred-2026-07-22`)
    already calls it by this name; existing callers/tests don't need to
    change. Scope note: callers must keep cross-vendor use narrow
    (CTRL-005/CTRL-006 BLOCK findings only, per the original proposal) --
    this function does not enforce that scope itself, the caller does."""
    return call_json(system, user, pcp_dir=pcp_dir, command=command, harness="agy")
