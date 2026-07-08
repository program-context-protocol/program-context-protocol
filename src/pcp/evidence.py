"""Full raw QA-proof storage.

telemetry.jsonl records a verdict (pass/block/skip/etc) plus a truncated
error summary — enough to see WHAT happened, not enough to independently
verify it happened the way claimed. This module stores the untruncated raw
artifact (full test output, full lint/SAST finding list, full architect-
review/gate judge response) under .pcp/evidence/<module>/<criterion_id>/
attempt_<n>/<check>.txt, and telemetry records the path instead of
embedding truncated text — proof, not just a verdict.

Always stores, on pass as well as block — "nothing to see, it passed" is
exactly the case proof is missing for today (a PASS currently records
nothing beyond the word "pass").
"""

from pathlib import Path


def _evidence_dir(pcp_dir: Path, module: str, criterion_id: str | None, attempt: int) -> Path:
    return Path(pcp_dir) / "evidence" / (module or "?") / (criterion_id or "?") / f"attempt_{attempt}"


def store(pcp_dir: Path, module: str, criterion_id: str | None, attempt: int, check: str, content: str) -> str:
    """Writes raw content, returns its path relative to pcp_dir (so telemetry
    can reference it without duplicating the content inline)."""
    directory = _evidence_dir(pcp_dir, module, criterion_id, attempt)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{check}.txt"
    path.write_text("" if content is None else str(content))
    return str(path.relative_to(pcp_dir))
