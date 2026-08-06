"""token_ledger.yaml — usage/cost logging shared by every harness.

Split out of client.py 2026-07-31 (harness/common split, see llm/harness/
package docstring) -- this is PCP's own ledger format, not a harness
implementation detail, so it lives independent of both client.py's
dispatch layer and any individual harness/*.py file to avoid a circular
import between them (client.py -> harness.claude -> needs _log_usage ->
would need client.py back, without this module in between).
"""

import threading
from datetime import datetime, timezone
from pathlib import Path

import yaml

from pcp.llm.otel_trace import record_span

# Guards token_ledger.yaml's read-modify-write -- gate checks in build.py's
# per-criterion loop that make an LLM call (architect-review, gate,
# design-justification) run concurrently with each other (2026-07-18,
# Project O dogfood finding: gate stages were needlessly sequential).
# Without this lock, two concurrent calls reading the same ledger snapshot
# before either writes back would silently drop one call's usage record.
_LEDGER_LOCK = threading.Lock()


def _log_usage(pcp_dir: Path | None, command: str, model: str | None, session_id: str | None,
               usage: dict, cost_usd: float | None) -> None:
    if pcp_dir is None:
        return
    with _LEDGER_LOCK:
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
    record_span(command, model, session_id, usage, cost_usd)
