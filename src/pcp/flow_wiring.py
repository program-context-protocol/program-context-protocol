"""Cross-module user-flow wiring — CTRL-040.

Every existing wave-merge check validates CODE-level integration: declared
dependencies are complete (CTRL-007), the merged test suite passes, imports
resolve, a diff review doesn't object. None of them prove that a real
end-to-end USER JOURNEY spanning several modules still works once those
modules are built in different waves by different agents. A dashboard
module and an export module can each pass every one of their own criteria
while the button the dashboard added never actually calls the export
module's endpoint — nothing above catches that, because nothing above
executes the product as a user would.

`.pcp/strategy/user_flows.yaml` (human-authorized via `pcp amend
user_flows`, same propose/diff/approve mechanic as decomposition.md/
dependency_map.md — see amend.py) declares these journeys as an ordered list
of UI steps. This module walks them for real, in a headless browser, once
every module a flow spans has completed.

Same could-not-check-vs-failed posture as uat.check_visual: `run_flow`
returns (None, detail) — not (False, detail) — when Playwright isn't
installed. Absence of the optional `[visual]` extra is not a wiring
failure, and callers must not downgrade a flow to "broken" over a missing
dependency.
"""

from __future__ import annotations

from pathlib import Path

import yaml

TIMEOUT_MS = 10_000

_VALID_ACTIONS = {"navigate", "click", "fill", "submit", "assert_visible", "assert_text"}


def load_flows(pcp_dir: Path) -> list[dict]:
    """Empty list if the file is absent or unparseable — same "not
    determinable" honesty nav_graph.analyse uses, never treated as a
    failure by callers."""
    path = pcp_dir / "strategy" / "user_flows.yaml"
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return []
    return data.get("flows") or []


def flow_is_runnable(flow: dict, wave_modules: list[dict], all_modules_complete: set[str]) -> bool:
    """A flow runs once every module it spans is complete, AND this wave
    completed at least one of them — otherwise an already-satisfied flow
    would re-run identically (and re-record telemetry) at every later wave
    forever, for no new signal."""
    spanned = set(flow.get("modules_spanned") or [])
    if not spanned or not spanned <= all_modules_complete:
        return False
    wave_names = {m["name"] for m in wave_modules}
    return bool(spanned & wave_names)


def _resolve_url(base_url: str, target: str) -> str:
    if target.startswith(("http://", "https://")):
        return target
    return base_url.rstrip("/") + "/" + target.lstrip("/")


def run_flow(flow: dict, base_url: str | None) -> tuple[bool | None, str]:
    """Walk a flow's declared steps for real. Returns:

    - (None, detail) — could not check (Playwright not installed). Never a
      finding against the product.
    - (False, detail) — a step broke; detail names the exact flow, step
      index, action, and selector, because "flow 3 failed" is useless and
      "the Export button's route doesn't exist" is what CTRL-040 exists to
      surface.
    - (True, detail) — every step completed.
    """
    flow_id = flow.get("id", "?")
    if not base_url:
        return False, f"flow '{flow_id}' has no base_url configured (set one in user_flows.yaml)"
    steps = flow.get("steps") or []
    if not steps:
        return False, f"flow '{flow_id}' declares no steps"

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, (
            f"flow '{flow_id}': playwright not installed — flow-wiring check skipped, not failed. "
            "Install with: pip install program-context-protocol[visual] && playwright install chromium"
        )

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            for i, step in enumerate(steps):
                action = step.get("action")
                target = step.get("target", "")
                if action not in _VALID_ACTIONS:
                    browser.close()
                    return False, f"flow '{flow_id}' step {i} declares unknown action '{action}'"
                try:
                    if action == "navigate":
                        page.goto(_resolve_url(base_url, target), timeout=TIMEOUT_MS)
                        page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
                    elif action == "click":
                        page.click(target, timeout=TIMEOUT_MS)
                    elif action == "fill":
                        page.fill(target, step.get("value", ""), timeout=TIMEOUT_MS)
                    elif action == "submit":
                        page.click(target, timeout=TIMEOUT_MS)
                        page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
                    elif action == "assert_visible":
                        page.wait_for_selector(target, state="visible", timeout=TIMEOUT_MS)
                    elif action == "assert_text":
                        page.wait_for_function(
                            "([sel, txt]) => document.querySelector(sel)?.innerText?.includes(txt)",
                            arg=[target, step.get("value", "")], timeout=TIMEOUT_MS,
                        )
                except Exception as e:
                    browser.close()
                    return False, (
                        f"flow '{flow_id}' broke at step {i} ({action} {target!r}): {e} — "
                        "this module boundary is not actually wired together"
                    )
            browser.close()
    except Exception as e:
        return False, f"flow '{flow_id}' failed to run in a headless browser: {e}"

    return True, f"flow '{flow_id}' completed all {len(steps)} step(s) successfully"
