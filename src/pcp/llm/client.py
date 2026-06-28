"""LLM client — uses `claude -p` CLI subprocess. No API key required."""

import json
import os
import subprocess
from typing import Any


def _claude_bin() -> str:
    return os.environ.get("PCP_CLAUDE_BIN", "claude")


def call(system: str, user: str) -> str:
    """Run prompt through claude CLI. Returns text output."""
    prompt = f"{system}\n\n---\n\n{user}"

    cmd = [_claude_bin(), "-p"]

    model = os.environ.get("PCP_MODEL")
    if model:
        cmd += ["--model", model]

    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"claude CLI not found at '{_claude_bin()}'. "
            "Install Claude Code: https://claude.ai/download"
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("claude CLI timed out after 120s")

    if result.returncode != 0:
        raise RuntimeError(
            f"claude CLI exited {result.returncode}: {result.stderr.strip()}"
        )

    return result.stdout.strip()


def call_json(system: str, user: str) -> Any:
    """Call claude CLI, parse response as JSON."""
    text = call(system, user + "\n\nRespond with valid JSON only. No markdown fences.")
    text = text.strip()
    # Strip markdown fences if model adds them anyway
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1])
    return json.loads(text)
