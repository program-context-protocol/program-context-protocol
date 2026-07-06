"""BuildModuleWorkflow — proves Temporal's durability/retry spine wraps
PCP's existing build loop correctly. One workflow, one activity call.

maximum_attempts=1 (no outer retry) is deliberate, not an oversight — caught
by actually running this end-to-end: build.py's own per-criterion loop
already retries internally (3 attempts, --resume'd). Any outer Temporal
retry_policy compounds with that inner loop multiplicatively, not
additively -- a real test run with maximum_attempts=2 cost $2.39 and 6 real
coding-agent invocations for what should have been a single attempt at one
trivial criterion. Temporal's actual value-add here is durability across
worker crashes/restarts, not additional retries on top of an
already-retrying subprocess; a human can explicitly re-run the workflow if
it genuinely needs another attempt.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from pcp.process.activities import build_module_activity


@workflow.defn
class BuildModuleWorkflow:
    @workflow.run
    async def run(self, project_root: str, module_name: str) -> dict:
        return await workflow.execute_activity(
            build_module_activity,
            args=[project_root, module_name],
            start_to_close_timeout=timedelta(seconds=1800),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
