"""agy (Antigravity CLI / Google Gemini) harness.

Split out of client.py 2026-07-31 -- see llm/harness/__init__.py's
docstring for the shared contract. Added 2026-07-31 for Loop 3's
cross-vendor architect-review verifier leg (proposed 2026-07-22, parked,
resumed -- see memory `project-cross-vendor-verifier-deferred-2026-07-22`).
Currently the ONLY caller is build.py's _verify_block_findings, scoped
narrowly to CTRL-005/CTRL-006 -- this file itself doesn't enforce that
scope, the caller does.

Real usage data, corrected same day: `agy -p ... --output-format json`
(not the bare text mode this file originally used) returns a genuine
envelope with `conversation_id` (real session-resume support, verified
live against `--conversation <id>`) and a `usage` block with
input/output/thinking/cache_read/total token counts. There is still no
`cost_usd` -- agy exposes no pricing -- so that field stays an honest
None, not a fabricated number. But "agy has no usage data" was wrong; it
just wasn't being asked for it. Real token counts now feed
pcp.llm.ledger._log_usage like every other harness, and
PCP_AGY_MAX_TOKENS_PER_CALL is a real circuit breaker on top of real
numbers, not a guess.
"""

import json
import os
import subprocess
from pathlib import Path

from pcp.llm.ledger import _log_usage


def _agy_bin() -> str:
    return os.environ.get("PCP_AGY_BIN", "agy")


def _agy_timeout() -> int:
    return int(os.environ.get("PCP_AGY_TIMEOUT", "240"))


def _agy_effort() -> str:
    """Default "low" -- Loop 3's only caller today is a verifier check, not
    a task that benefits from deep reasoning, and effort is real cost
    control agy exposes (confirmed: --effort low measurably drops
    thinking_tokens). PCP_AGY_EFFORT overrides for a caller that needs more."""
    return os.environ.get("PCP_AGY_EFFORT", "low")


def _agy_max_tokens_per_call() -> int:
    """Per-call ceiling, same role --max-budget-usd plays for claude (a
    live cap passed/checked per call, not a cumulative tracker) -- except
    agy has no self-enforcing budget flag, so this is checked AFTER the
    call returns, against real reported usage. Can't prevent the tokens
    already spent on a call that blew past it, but does turn that call
    into a raised, non-silent failure (fails the same way a timeout or
    missing binary does) rather than a quietly-accepted result -- and
    stops a caller from treating an oversized response as normal."""
    return int(os.environ.get("PCP_AGY_MAX_TOKENS_PER_CALL", "200000"))


def _call_agy(system: str, user: str, model: str | None = None, pcp_dir: Path | None = None,
              command: str = "llm.call", return_meta: bool = False) -> str | tuple[str, dict]:
    """Same contract as harness.claude._call_claude(). `model`, if given, is
    passed through to agy's own `--model` flag. `--effort` is always passed
    (see _agy_effort()) -- real cost control, not optional."""
    prompt = f"{system}\n\n---\n\n{user}"
    cwd = Path(pcp_dir).parent if pcp_dir else None
    cmd = [
        _agy_bin(), "-p", prompt, "--output-format", "json",
        "--print-timeout", f"{_agy_timeout()}s", "--effort", _agy_effort(),
    ]
    if model:
        cmd += ["--model", model]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_agy_timeout() + 30, cwd=cwd,
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"agy CLI not found at '{_agy_bin()}'. Install Antigravity CLI, or unset "
            "PCP_LLM_HARNESS/PCP_VERIFIER_CROSS_VENDOR to skip it."
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"agy CLI timed out after {_agy_timeout()}s")

    if result.returncode != 0:
        raise RuntimeError(f"agy CLI exited {result.returncode}: {result.stderr.strip()}")

    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError:
        # Fallback: unexpected/older CLI output -- treat stdout as raw text,
        # same posture harness.claude._call_claude() takes on the same failure.
        text = result.stdout.strip()
        return (text, {"model": model or "agy/default", "session_id": None, "usage": {}, "cost_usd": None, "duration_ms": None}) if return_meta else text

    if envelope.get("status") and envelope.get("status") != "SUCCESS":
        raise RuntimeError(f"agy CLI reported status={envelope.get('status')}: {envelope.get('response', '')}")

    usage = envelope.get("usage", {}) or {}
    total_tokens = usage.get("total_tokens", 0)
    if total_tokens > _agy_max_tokens_per_call():
        raise RuntimeError(
            f"agy call used {total_tokens} tokens, over the {_agy_max_tokens_per_call()} "
            "PCP_AGY_MAX_TOKENS_PER_CALL ceiling -- treated as a failed call, same as a "
            "timeout, rather than silently accepted."
        )

    duration_ms = int(envelope.get("duration_seconds", 0) * 1000) if envelope.get("duration_seconds") else None
    conversation_id = envelope.get("conversation_id")

    _log_usage(pcp_dir, command, model or "agy/default", conversation_id, usage, None)

    text = (envelope.get("response") or "").strip()
    if not return_meta:
        return text
    meta = {
        "model": model or "agy/default", "session_id": conversation_id,
        "usage": usage, "cost_usd": None, "duration_ms": duration_ms,
    }
    return text, meta
