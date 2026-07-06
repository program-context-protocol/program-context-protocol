"""Tier 2, Phase C — process/workflow layer. Wraps pcp build's existing
per-module CLI invocation as a Temporal Activity/Workflow pair, proving the
durability/retry integration works. Deliberately not a rebuild of build.py's
own wave/retry logic in Temporal terms — this is an outer durability spine
around the existing loop, not a replacement of it. A LangGraph-based step/
graph layer (per-step tool scoping + context injection) is the natural next
increment on top of this skeleton, not built in this same pass.

Requires the optional `process` extra (`pip install program-context-protocol[process]`)
plus the separate `temporal` CLI for local dev (`temporal server start-dev`,
not started by any code here — PCP never silently launches a long-running
server process on your machine).
"""
