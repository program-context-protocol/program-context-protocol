"""agy (Antigravity CLI / Google Gemini) harness.

Split out of client.py 2026-07-31 -- see llm/harness/__init__.py's
docstring for the shared contract. Added 2026-07-31 for Loop 3's
cross-vendor architect-review verifier leg (proposed 2026-07-22, parked,
resumed -- see memory `project-cross-vendor-verifier-deferred-2026-07-22`).
Currently the ONLY caller is build.py's _verify_block_findings, scoped
narrowly to CTRL-005/CTRL-006 -- this file itself doesn't enforce that
scope, the caller does.

agy has no cost/usage JSON envelope the way `claude -p --output-format
json` does, so nothing here calls pcp.llm.ledger._log_usage -- a real,
stated gap (cross-vendor spend is untracked by PCP's own ledger), not a
silently dropped one.
"""

import os
import subprocess
from pathlib import Path


def _agy_bin() -> str:
    return os.environ.get("PCP_AGY_BIN", "agy")


def _agy_timeout() -> int:
    return int(os.environ.get("PCP_AGY_TIMEOUT", "240"))


def _call_agy(system: str, user: str, model: str | None = None, pcp_dir: Path | None = None,
              command: str = "llm.call", return_meta: bool = False) -> str | tuple[str, dict]:
    """Same contract as harness.claude._call_claude(). meta's usage/cost_usd/
    session_id fields are honest placeholders (empty/None), not fabricated
    numbers -- see the module docstring. `model`, if given, is passed
    through to agy's own `--model` flag."""
    prompt = f"{system}\n\n---\n\n{user}"
    cwd = Path(pcp_dir).parent if pcp_dir else None
    cmd = [_agy_bin(), "-p", prompt, "--print-timeout", f"{_agy_timeout()}s"]
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

    text = result.stdout.strip()
    if not return_meta:
        return text
    meta = {"model": model or "agy/default", "session_id": None, "usage": {}, "cost_usd": None, "duration_ms": None}
    return text, meta
