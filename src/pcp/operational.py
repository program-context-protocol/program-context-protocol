"""Files PCP itself generates while running — never subject matter for a gate.

These are machine-written records *about* the code (telemetry, ledgers, scans,
audit trails). They are not code, nobody authored them, and no rule should ever
be evaluated against their contents.

`build.py` already excluded them from criterion diffs. `check.py` did not, and
that gap produced a real Layer 1 failure on Project O, 2026-07-30:

    reason: R008 matched its own rule text quoted inside generated
            telemetry.jsonl, not a real property_hints persistence

An `ast_pattern` rule searched `.pcp/telemetry.jsonl` and found its own pattern
there — because telemetry records the findings of the rules, so a rule's pattern
text is written into the very file the next commit stages and scans. The gate
flagged its own audit trail.

The consequence was worse than one false finding. A `[pcp-bypass]` is
all-or-nothing across rules, so that single self-match caused **R001-R010 to be
bypassed together** for that commit, in an unattended run, with nobody reading it.
One false positive from a generated file voided the whole Layer 1 gate.

Kept in its own module because `build.py` imports `check.py`, so the constants
cannot live in either without a cycle. Any NEW file PCP writes under `.pcp/`
during a run must be added here at the same time it starts being written.
"""

OPERATIONAL_PATHS: tuple[str, ...] = (
    ".pcp/token_ledger.yaml", ".pcp/telemetry.jsonl", ".pcp/decision_log.jsonl",
    ".pcp/brd.md", ".pcp/brd_items.yaml", ".pcp/coverage_audit.jsonl",
    ".pcp/escalations.yaml", ".pcp/prune_log.yaml", ".pcp/current_state.md",
    ".pcp/diff.md", ".pcp/notify_heartbeat.yaml", ".pcp/build_progress.yaml",
    ".pcp/bypass_log.yaml", ".pcp/audit_trend.jsonl", ".pcp/attestations.jsonl",
    ".pcp/symbol_fingerprints.json", ".pcp/install_approvals.yaml",
    ".pcp/run_ledger.jsonl", ".pcp/pressure_test_log.jsonl",
    ".pcp/audit.md", ".pcp/provenance.md", ".pcp/control_audit.md",
    ".pcp/build_report.md", ".pcp/design_audit.md", ".pcp/narrative_lint.md",
    ".pcp/architecture_justification.md",
    "pcp.md",
)

OPERATIONAL_DIRS: tuple[str, ...] = (".pcp/evidence/", ".pcp/transcripts/")


def is_operational(path: str) -> bool:
    """Is this a file PCP wrote about itself, rather than project content?

    Note the prefix strip is deliberate and `lstrip` would be wrong here:
    `lstrip("./")` removes a character SET, so it turns `.pcp/telemetry.jsonl`
    into `pcp/telemetry.jsonl` and every path stops matching. That is exactly
    what the first version did, and it silently made this whole module a no-op —
    the tests caught it, the code read fine.
    """
    norm = str(path).replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return norm in OPERATIONAL_PATHS or any(norm.startswith(d) for d in OPERATIONAL_DIRS)


def filter_operational(paths: list[str]) -> tuple[list[str], list[str]]:
    """(paths to gate, paths skipped as PCP's own output).

    Returns both halves rather than silently dropping: a gate that quietly
    narrows its own scope is indistinguishable from one that found nothing, and
    that conflation is the single most repeated defect in this codebase.
    """
    keep, skipped = [], []
    for p in paths:
        (skipped if is_operational(p) else keep).append(p)
    return keep, skipped
