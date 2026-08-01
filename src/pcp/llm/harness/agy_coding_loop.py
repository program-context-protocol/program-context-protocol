"""Antigravity CLI (agy) coding-agent-loop harness -- implements
CodingAgentHarness (llm/coding_agent_contract.py).

Provenance: the first draft of this file was written BY agy itself
(2026-08-01), given the contract and agy's own verified real CLI behavior
as input -- the same reasoning applied to "let Codex verify its own
contract instead of guessing" throughout the multi-harness conversation
(tag `pre-multi-harness-extension`) applies to agy here too. Reviewed and
lightly corrected before being accepted, same as any other AI-authored
change in this codebase -- not applied blind.

One correction from the original draft worth recording: it hardcoded
`--effort low` for every call, copying that choice from harness/agy.py's
verifier leg (a yes/no judgment call, where low effort is a deliberate,
measured cost control) without noticing that actual code generation is a
different kind of task -- forcing low reasoning effort on real coding work
isn't a cost optimization there, it's a quality regression. Fixed to leave
effort unset by default (agy's own default reasoning depth), configurable
via PCP_AGY_CODING_EFFORT for a caller that wants to override it either
direction.

Honest limitations, stated rather than papered over:
1. No pre-enforced budget ceiling. agy has no `--max-budget-usd`
   equivalent -- request.max_budget_usd cannot be pre-enforced at the CLI
   level, only checked after the fact against real reported tokens
   (see PCP_AGY_MAX_TOKENS_PER_CALL below, default 500,000/call).
2. cost_usd is always None. agy reports real token usage but no pricing
   data -- never fabricated here.
3. request.session_id (the caller-chosen id for a FRESH session) is
   advisory only, not actually usable. Unlike Claude Code's
   `--session-id <id>` (open a NEW session under a caller-picked name),
   agy always self-assigns its own conversation_id on a fresh call --
   there is no flag to open one under a name PCP picked. The REAL id
   PCP must track going forward is whatever comes back in the result,
   not what it requested. This is a genuine harness capability
   difference (see the multi-harness conversation's "accept divergence
   at the implementation layer" conclusion), not a bug to paper over.

Not wired into `pcp build` -- Stage 3/4 of the multi-harness plan still
require a deliberate decision to actually route criteria through this
harness instead of Claude Code. This file existing does not change
`pcp build`'s behavior.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from pcp.llm.coding_agent_contract import CodingAgentRequest, CodingAgentResult

DEFAULT_MAX_TOKENS_PER_CALL = 500_000


def _agy_coding_effort() -> str | None:
    """None by default -- let agy use its own default reasoning depth for
    real code-writing, unlike the verifier leg's deliberate --effort low.
    PCP_AGY_CODING_EFFORT overrides either direction."""
    return os.environ.get("PCP_AGY_CODING_EFFORT")


def _agy_coding_max_tokens_per_call() -> int:
    env_limit = os.environ.get("PCP_AGY_MAX_TOKENS_PER_CALL")
    return int(env_limit) if env_limit else DEFAULT_MAX_TOKENS_PER_CALL


class AgyCodingAgentHarness:
    """Coding agent harness implementation wrapping the Antigravity `agy`
    CLI. See module docstring for what's honestly not supported."""

    def __init__(self, max_tokens_limit: int | None = None) -> None:
        self.max_tokens_limit = max_tokens_limit if max_tokens_limit is not None else _agy_coding_max_tokens_per_call()

    def run(self, request: CodingAgentRequest) -> CodingAgentResult:
        """Run one coding-task attempt using the `agy` CLI in unattended
        file-editing mode against request.cwd."""
        cmd: list[str] = [
            "agy", "-p", request.prompt,
            "--output-format", "json",
            "--print-timeout", f"{request.timeout_sec}s",
            "--dangerously-skip-permissions",
            "--mode", "accept-edits",
            "--sandbox",
            "--add-dir", str(request.cwd),
        ]
        effort = _agy_coding_effort()
        if effort:
            cmd += ["--effort", effort]

        if request.resume_session_id:
            cmd += ["--conversation", request.resume_session_id]
        # No else branch: agy has no flag to open a fresh session under a
        # caller-chosen id (see module docstring, limitation 3) -- a fresh
        # call just starts a new conversation and self-assigns its own id.

        if request.model:
            cmd += ["--model", str(request.model)]

        start_time = time.monotonic()

        try:
            res = subprocess.run(
                cmd, cwd=request.cwd, capture_output=True, text=True, timeout=request.timeout_sec,
            )
            elapsed_ms = int((time.monotonic() - start_time) * 1000)
        except subprocess.TimeoutExpired:
            return CodingAgentResult(
                ok=False, error=f"Execution timed out after {request.timeout_sec} seconds",
                session_id=request.resume_session_id or request.session_id, model=request.model,
                duration_ms=int((time.monotonic() - start_time) * 1000),
            )
        except FileNotFoundError:
            return CodingAgentResult(
                ok=False, error="Executable 'agy' CLI was not found in PATH",
                session_id=request.resume_session_id or request.session_id, model=request.model,
                duration_ms=int((time.monotonic() - start_time) * 1000),
            )
        except Exception as e:
            return CodingAgentResult(
                ok=False, error=f"Subprocess execution failed: {e}",
                session_id=request.resume_session_id or request.session_id, model=request.model,
                duration_ms=int((time.monotonic() - start_time) * 1000),
            )

        stdout = res.stdout.strip()
        stderr = res.stderr.strip()

        if not stdout:
            err_msg = stderr or f"Process exited with code {res.returncode} and empty stdout"
            return CodingAgentResult(
                ok=False, error=err_msg,
                session_id=request.resume_session_id or request.session_id, model=request.model,
                duration_ms=elapsed_ms,
            )

        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            err_msg = f"Failed to parse JSON response envelope from agy. Raw stdout: {stdout[:500]}"
            if stderr:
                err_msg += f" | stderr: {stderr[:500]}"
            return CodingAgentResult(
                ok=False, error=err_msg,
                session_id=request.resume_session_id or request.session_id, model=request.model,
                duration_ms=elapsed_ms,
            )

        # The REAL session id going forward -- never what was requested,
        # see module docstring limitation 3.
        session_id = payload.get("conversation_id") or request.resume_session_id or request.session_id
        status = payload.get("status")
        usage = payload.get("usage") or {}
        duration_seconds = payload.get("duration_seconds")
        duration_ms = int(duration_seconds * 1000) if duration_seconds is not None else elapsed_ms

        if res.returncode != 0 or status != "SUCCESS":
            err_msg = payload.get("error") or payload.get("response") or stderr or f"agy reported status={status} (exit code {res.returncode})"
            return CodingAgentResult(
                ok=False, error=str(err_msg), session_id=session_id, model=request.model,
                usage=usage, cost_usd=None, duration_ms=duration_ms,
            )

        total_tokens = usage.get("total_tokens", 0)
        if total_tokens > self.max_tokens_limit:
            return CodingAgentResult(
                ok=False,
                error=(
                    f"Token usage limit exceeded: call used {total_tokens} total tokens "
                    f"(limit is {self.max_tokens_limit} tokens)"
                ),
                session_id=session_id, model=request.model, usage=usage, cost_usd=None, duration_ms=duration_ms,
            )

        return CodingAgentResult(
            ok=True, error=None, session_id=session_id, model=request.model,
            usage=usage, cost_usd=None, duration_ms=duration_ms,
        )
