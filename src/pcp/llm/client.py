"""LLM client — uses `claude -p` CLI subprocess. No API key required.

Token discipline is a hard constraint, same tier as modularity (see CLAUDE.md).
Every call site must pass an explicit `model` — judge/advisory calls route to
Haiku by default; PCP_MODEL env always wins if a human sets it. Usage/cost is
captured from --output-format json and logged to .pcp/token_ledger.yaml so
spend is visible the same way coverage_score and coupling_score are.
"""

import base64
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

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


def _claude_bin() -> str:
    return os.environ.get("PCP_CLAUDE_BIN", "claude")


def _timeout() -> int:
    return int(os.environ.get("PCP_LLM_TIMEOUT", "300"))


def _log_usage(pcp_dir: Path | None, command: str, model: str | None, session_id: str | None,
               usage: dict, cost_usd: float | None) -> None:
    if pcp_dir is None:
        return
    ledger_path = Path(pcp_dir) / "token_ledger.yaml"
    entries = []
    if ledger_path.exists():
        data = yaml.safe_load(ledger_path.read_text()) or {}
        entries = data.get("calls", [])
    entries.append({
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "command": command,
        "model": model or "default",
        "session_id": session_id,
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
        "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
        "cost_usd": cost_usd,
    })
    ledger_path.write_text(yaml.dump({"calls": entries}, default_flow_style=False))


def call(system: str, user: str, model: str | None = None, pcp_dir: Path | None = None,
          command: str = "llm.call", return_meta: bool = False) -> str | tuple[str, dict]:
    """Run prompt through claude CLI (one-shot, no session reuse). Returns text output.

    model: explicit model for this call site (e.g. JUDGE_MODEL). PCP_MODEL env overrides
    everything if set, so a human can force a model for debugging.
    pcp_dir/command: if given, usage+cost is appended to .pcp/token_ledger.yaml.
    return_meta: if True, returns (text, meta) where meta has model/session_id/usage/
    cost_usd/duration_ms — for callers building richer per-event telemetry (see telemetry.py).
    """
    prompt = f"{system}\n\n---\n\n{user}"

    resolved_model = os.environ.get("PCP_MODEL") or model
    cmd = [_claude_bin(), "-p", "--output-format", "json"]
    if resolved_model:
        cmd += ["--model", resolved_model]

    # Real bug, found 2026-07-08: this call never passed cwd, so it always
    # ran in whatever the calling PROCESS's actual OS cwd happened to be --
    # not necessarily the target project. Harmless when the CLI is invoked
    # from the project root (the common case), actively wrong otherwise: a
    # test process (or any caller) with a different cwd would silently run
    # the agent against the wrong directory. pcp_dir is already passed by
    # every call site for token-ledger logging, so it doubles as the correct
    # anchor here — project_root = pcp_dir.parent.
    cwd = Path(pcp_dir).parent if pcp_dir else None

    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=_timeout(),
            cwd=cwd,
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"claude CLI not found at '{_claude_bin()}'. "
            "Install Claude Code: https://claude.ai/download"
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"claude CLI timed out after {_timeout()}s")

    if result.returncode != 0:
        raise RuntimeError(
            f"claude CLI exited {result.returncode}: {result.stderr.strip()}"
        )

    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError:
        # Fallback: older CLI or unexpected output — treat stdout as raw text.
        text = result.stdout.strip()
        return (text, {}) if return_meta else text

    if envelope.get("is_error"):
        raise RuntimeError(f"claude CLI returned an error: {envelope.get('result', '')}")

    _log_usage(
        pcp_dir, command, resolved_model, envelope.get("session_id"),
        envelope.get("usage", {}), envelope.get("total_cost_usd"),
    )

    text = (envelope.get("result") or "").strip()
    if not return_meta:
        return text

    meta = {
        "model": resolved_model or "default",
        "session_id": envelope.get("session_id"),
        "usage": envelope.get("usage", {}),
        "cost_usd": envelope.get("total_cost_usd"),
        "duration_ms": envelope.get("duration_ms"),
    }
    return text, meta


def call_json(system: str, user: str, model: str | None = None, pcp_dir: Path | None = None,
              command: str = "llm.call_json", return_meta: bool = False) -> Any:
    """Call claude CLI, parse response as JSON."""
    out = call(
        system, user + "\n\nRespond with valid JSON only. No markdown fences.",
        model=model, pcp_dir=pcp_dir, command=command, return_meta=return_meta,
    )
    text, meta = out if return_meta else (out, None)
    text = text.strip()
    # Strip markdown fences if model adds them anyway
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1])
    parsed = json.loads(text)
    return (parsed, meta) if return_meta else parsed


_MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def call_with_images(system: str, user: str, image_paths: list[Path], model: str | None = None,
                     pcp_dir: Path | None = None, command: str = "llm.call_with_images",
                     return_meta: bool = False) -> str | tuple[str, dict]:
    """Same contract as call(), but attaches one or more images as inline
    multimodal content blocks. Plain text stdin (what call() uses) has no
    image channel -- `claude -p` only accepts an image via `--input-format
    stream-json` (a single JSON message with text + image content blocks),
    which in turn requires `--output-format stream-json` (confirmed by the
    CLI's own error: "requires --verbose" once stream-json output is set).
    The final envelope is the last `type: "result"` line on stdout -- same
    fields (result/session_id/usage/total_cost_usd) as --output-format json.

    Enables VLM-based checks (uat.check_visual_quality) that judge a
    rendered screenshot -- optionally against a second reference image --
    without requiring the coding agent itself to have filesystem access to
    the image at judge-call time. Verified live 2026-07-18: the model
    sometimes first attempts a Read tool call on a hallucinated file path
    for an inline image before falling back to inspecting it directly --
    the explicit instruction below cuts that wasted turn most of the time
    but not always; harmless either way, just an extra attempt."""
    content = [{
        "type": "text",
        "text": (
            f"{system}\n\n---\n\n{user}\n\n"
            f"{'The image is' if len(image_paths) == 1 else 'The images are'} attached "
            "inline in this message's content as image blocks -- not files on disk. "
            "Do not attempt to Read a file path for them; analyze the attached "
            "image(s) directly."
        ),
    }]
    for image_path in image_paths:
        media_type = _MEDIA_TYPES.get(image_path.suffix.lower(), "image/png")
        data = base64.b64encode(image_path.read_bytes()).decode()
        content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})
    message = {"type": "user", "message": {"role": "user", "content": content}}

    resolved_model = os.environ.get("PCP_MODEL") or model
    cmd = [_claude_bin(), "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
    if resolved_model:
        cmd += ["--model", resolved_model]
    cwd = Path(pcp_dir).parent if pcp_dir else None

    try:
        result = subprocess.run(
            cmd, input=json.dumps(message), capture_output=True, text=True,
            timeout=_timeout(), cwd=cwd,
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"claude CLI not found at '{_claude_bin()}'. "
            "Install Claude Code: https://claude.ai/download"
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"claude CLI timed out after {_timeout()}s")

    if result.returncode != 0:
        raise RuntimeError(
            f"claude CLI exited {result.returncode}: {result.stderr.strip()}"
        )

    envelope = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "result":
            envelope = obj
    if envelope is None:
        raise RuntimeError("claude CLI stream-json output had no result event")

    if envelope.get("is_error"):
        raise RuntimeError(f"claude CLI returned an error: {envelope.get('result', '')}")

    _log_usage(
        pcp_dir, command, resolved_model, envelope.get("session_id"),
        envelope.get("usage", {}), envelope.get("total_cost_usd"),
    )

    text = (envelope.get("result") or "").strip()
    if not return_meta:
        return text

    meta = {
        "model": resolved_model or "default",
        "session_id": envelope.get("session_id"),
        "usage": envelope.get("usage", {}),
        "cost_usd": envelope.get("total_cost_usd"),
        "duration_ms": envelope.get("duration_ms"),
    }
    return text, meta


def call_with_image(system: str, user: str, image_path: Path, model: str | None = None,
                     pcp_dir: Path | None = None, command: str = "llm.call_with_image",
                     return_meta: bool = False) -> str | tuple[str, dict]:
    """Single-image convenience wrapper over call_with_images()."""
    return call_with_images(
        system, user, [image_path], model=model, pcp_dir=pcp_dir, command=command, return_meta=return_meta,
    )


def call_json_with_images(system: str, user: str, image_paths: list[Path], model: str | None = None,
                           pcp_dir: Path | None = None, command: str = "llm.call_json_with_images",
                           return_meta: bool = False) -> Any:
    """call_with_images + JSON parsing, same contract as call_json()."""
    out = call_with_images(
        system, user + "\n\nRespond with valid JSON only. No markdown fences.",
        image_paths, model=model, pcp_dir=pcp_dir, command=command, return_meta=return_meta,
    )
    text, meta = out if return_meta else (out, None)
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1])
    parsed = json.loads(text)
    return (parsed, meta) if return_meta else parsed


def call_json_with_image(system: str, user: str, image_path: Path, model: str | None = None,
                          pcp_dir: Path | None = None, command: str = "llm.call_json_with_image",
                          return_meta: bool = False) -> Any:
    """Single-image convenience wrapper over call_json_with_images()."""
    return call_json_with_images(
        system, user, [image_path], model=model, pcp_dir=pcp_dir, command=command, return_meta=return_meta,
    )
