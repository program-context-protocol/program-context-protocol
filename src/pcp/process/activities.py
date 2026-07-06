"""Thin Temporal Activity wrapping pcp build's existing per-module CLI
invocation as a black box. Deliberately not refactoring build.py's internal
loop out into a library call for this first slice — the activity's job is
just: invoke `pcp build --module <name>`, raise on failure so Temporal's
own retry policy (an outer layer on top of build.py's own per-criterion
attempt/resume retries) can act on it, return on success.
"""

import subprocess

from temporalio import activity

BUILD_TIMEOUT_SEC = 1800


@activity.defn
async def build_module_activity(project_root: str, module_name: str) -> dict:
    result = subprocess.run(
        ["pcp", "build", "--module", module_name],
        cwd=project_root, capture_output=True, text=True, timeout=BUILD_TIMEOUT_SEC,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"pcp build --module {module_name} failed (exit {result.returncode}): "
            f"{result.stderr[-2000:] or result.stdout[-2000:]}"
        )
    return {"module": module_name, "stdout_tail": result.stdout[-2000:]}
