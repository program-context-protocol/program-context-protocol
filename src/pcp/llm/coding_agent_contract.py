"""The coding-agent-loop contract -- Stage 1 of the multi-harness plan
(see git tag `pre-multi-harness-extension` for the state this plan started
from, and the conversation it came out of for the full reasoning).

This is documentation given a type, not a running abstraction. `pcp
build`'s actual coding loop (commands/build.py's per-attempt subprocess
call, inside _build_one_criterion) still calls `claude -p` directly --
nothing here is wired into it yet, and nothing here changes its behavior.
What this file does is name, precisely, what that call currently does, so:

1. A second harness (Codex, or agy promoted beyond its current verifier-
   only role) has a concrete target to implement against, once someone
   actually builds it -- instead of reverse-engineering build.py's
   ~150-line inline subprocess dance from scratch.
2. tests/test_coding_agent_contract.py can mechanically check build.py's
   own implementation still satisfies what's declared here -- a structural
   guard against silent drift between "what the contract says" and "what
   the one real implementation actually does", the same shape as
   test_build_parallel.py's test_no_unregistered_pcp_runtime_writer.

Deliberately NOT done here (see the conversation for why): extracting
build.py's inline call into a _run_coding_agent_claude() that implements
this contract, the way llm/harness/claude.py's _call_claude() implements
client.py's judge-call contract. That extraction is Stage 3/4 of the plan,
and it waits for a second real implementation to validate the boundary
against -- building it speculatively, alone, against a guess is a bigger
risk than the drift this file exists to catch in the meantime.

Three policy points this contract makes explicit because they were each a
real, deliberate design decision (see build.py's own comments at the call
site) and are exactly the kind of thing a naive re-implementation would
get wrong by "simplifying":

  - Attempt 1 opens a FRESH session. Attempt 2 RESUMES it (Token
    Discipline -- avoid re-exploring the repo). Attempt 3 (escalation)
    does NOT resume -- deliberately fresh, because contaminated retry
    context measurably hurts (CCRM, arXiv:2605.08563, 7.1x baseline error
    rate). This is a POLICY the caller decides (which attempt, therefore
    fresh vs resume) -- the harness implementation just needs to support
    both "open fresh under session_id" and "resume session_id", not decide
    between them itself.
  - Every attempt has an explicit wall-clock timeout AND an explicit
    dollar budget ceiling, both passed in per-call, not read from the
    harness's own defaults -- a stuck/looping agent must not run
    unbounded just because it hasn't returned yet, and the ceiling is
    PCP's own circuit breaker (PCP_MAX_BUILD_SESSIONS /
    PCP_BUILD_AGENT_MAX_BUDGET_USD), not the harness's.
  - The primitive's job ends at "did it run, what did it cost, what
    session id came out of it." It does NOT report what changed --
    build.py reads that back from git (_get_changed_files_since,
    _get_working_diff) as a separate, already-harness-agnostic step. A
    harness implementation that tried to self-report its own diff would
    be trusting the agent's own claim about its own work, exactly the
    kind of unverified self-report PCP's own gates exist to not trust
    anywhere else.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class CodingAgentRequest:
    """One attempt. `resume_session_id` is None for a fresh session (attempt
    1 and attempt 3/escalation), set for a resumed one (attempt 2) -- the
    CALLER decides fresh vs resume (see module docstring), this is just the
    resulting instruction to the harness."""
    prompt: str
    cwd: Path
    session_id: str
    resume_session_id: str | None
    model: str | None
    timeout_sec: int
    max_budget_usd: str | float


@dataclass
class CodingAgentResult:
    """`ok=False` covers every failure the current build.py code already
    distinguishes as a retryable attempt failure (timeout, non-zero exit,
    is_error envelope) -- see _build_one_criterion's attempt loop, which
    treats all three the same way (record feedback, continue to next
    attempt). `changed_files`/`diff` are deliberately NOT fields here --
    see module docstring's third policy point."""
    ok: bool
    error: str | None
    session_id: str | None
    model: str | None
    usage: dict = field(default_factory=dict)
    cost_usd: float | None = None
    duration_ms: int | None = None


class CodingAgentHarness(Protocol):
    """Contract any coding-agent-loop implementation satisfies. See module
    docstring -- build.py's own inline implementation (commands/build.py,
    _build_one_criterion's attempt loop) is the one real implementation
    today; it is NOT wired to this Protocol (no `class ClaudeCodingAgent`
    exists yet), so this is checked by structural inspection
    (tests/test_coding_agent_contract.py), not by isinstance/typing."""

    def run(self, request: CodingAgentRequest) -> CodingAgentResult:
        ...
