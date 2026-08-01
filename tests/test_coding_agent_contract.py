"""Structural guard, not a spot fix -- same shape as
test_build_parallel.py::test_no_unregistered_pcp_runtime_writer.

llm/coding_agent_contract.py names, in prose + types, what build.py's own
coding-agent invocation (_build_one_criterion's per-attempt subprocess
call) currently does -- Stage 1 of the multi-harness plan (see git tag
`pre-multi-harness-extension`). This is Stage 2: a mechanical check that
build.py's ACTUAL implementation hasn't silently drifted from what Stage 1
declared, using inspect.getsource() over the real function rather than
trusting a comment to stay accurate. Every assertion here corresponds to
one of the three policy points in coding_agent_contract.py's module
docstring -- read that first if one of these fails.

Deliberately not an isinstance/Protocol check: build.py's implementation
is NOT wired to the CodingAgentHarness Protocol (Stage 3/4, not done, see
coding_agent_contract.py) -- there is nothing to isinstance() yet. This is
inspection of the one real implementation against the contract's prose."""

import inspect

from pcp.commands.build import _build_one_criterion


def _source() -> str:
    return inspect.getsource(_build_one_criterion)


def test_both_fresh_and_resume_session_flags_present():
    """Policy point 1: attempt 1/3 open fresh, attempt 2 resumes -- the
    implementation must support both, or the fresh-vs-resume POLICY
    (decided by the caller, not the harness) has nothing to select between."""
    src = _source()
    assert '"--session-id"' in src, "fresh-session flag missing -- how would attempt 1/3 open a new session?"
    assert '"--resume"' in src, "resume flag missing -- how would attempt 2 avoid re-exploring the repo?"


def test_escalation_attempt_does_not_resume():
    """Policy point 1, the specific part a naive 'just always resume for
    efficiency' simplification would get wrong: attempt 3 (escalation)
    must NOT resume -- contaminated retry context measurably hurts
    (CCRM, arXiv:2605.08563). Checked by finding the escalation branch's
    own session_flag assignment and confirming it uses --session-id, not
    --resume, textually adjacent to where escalation_session_id is set."""
    src = _source()
    assert "escalation_session_id = str(uuid.uuid4())" in src, \
        "escalation branch must mint its own fresh session id, not reuse the failed attempts'"
    # The escalation branch's session_flag must be the fresh-open flag.
    escalation_idx = src.index("escalation_session_id = str(uuid.uuid4())")
    nearby = src[escalation_idx:escalation_idx + 300]
    assert '"--session-id", escalation_session_id' in nearby, \
        "escalation attempt appears to resume instead of opening fresh -- this is the exact regression CCRM warned about"


def test_every_attempt_has_an_explicit_wall_clock_timeout():
    """Policy point 2: a stuck/looping agent must not run unbounded. The
    subprocess.run() call for the coding agent must pass timeout= from a
    function call (a live ceiling), not a bare literal that could silently
    be a stale/huge number nobody notices."""
    src = _source()
    assert "timeout=_build_agent_timeout_sec()" in src, \
        "coding-agent subprocess.run must pass a live timeout ceiling, not an omitted or hardcoded one"


def test_every_attempt_has_an_explicit_budget_ceiling():
    """Policy point 2, the dollar half: --max-budget-usd must be passed to
    every coding-agent invocation from a live function call, and the run's
    own project-level budget tracker must be updated from the real
    reported cost afterward (budget.add_cost) -- not just logged and
    forgotten, which would make PCP_PROJECT_BUDGET_USD a no-op."""
    src = _source()
    assert '"--max-budget-usd", _build_agent_max_budget_usd()' in src, \
        "coding-agent subprocess.run must pass a live per-attempt dollar ceiling"
    assert "budget.add_cost(" in src, \
        "reported cost must feed back into the run-level budget tracker, or the project spend ceiling is fiction"


def test_diff_is_read_from_git_not_from_the_agents_own_envelope():
    """Policy point 3: the coding-agent primitive's job ends at 'did it
    run, what did it cost' -- what changed is read back from git
    separately, never trusted as the agent's own self-report. If this
    regresses (someone starts trusting envelope.get('files_changed') or
    similar), PCP would be trusting exactly the kind of unverified
    self-report its own gates exist to catch everywhere else."""
    src = _source()
    assert "_get_changed_files_since(project_root, criterion_start_ref)" in src, \
        "changed files must be computed from git state, not from the agent's own report"
    assert "_get_working_diff(project_root, criterion_start_ref)" in src, \
        "the diff gates evaluate must come from git, not from the agent's own report"


def test_budget_circuit_breaker_checked_before_spawning_the_agent():
    """The run-level session-count circuit breaker (PCP_MAX_BUILD_SESSIONS)
    must be checked BEFORE the subprocess spawns, not after -- checking
    after would mean the breaker only prevents counting the (N+1)th
    session, not spending on it."""
    src = _source()
    take_session_idx = src.index("budget.take_session()")
    subprocess_idx = src.index("result = subprocess.run(")
    assert take_session_idx < subprocess_idx, \
        "budget.take_session() must be checked before the coding-agent subprocess spawns, not after"
