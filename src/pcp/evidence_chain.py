"""Hash-chaining for append-only evidence logs (telemetry.jsonl,
decision_log.jsonl, bypass_log.yaml).

Each record's own hash covers its content plus the previous record's hash,
so retroactively editing an earlier entry breaks every hash after it. This
is tamper-EVIDENCE, not tamper-prevention: nothing stops someone editing the
file directly, but doing so without also recomputing every downstream hash
is now detectable by `verify_chain()` — and recomputing every downstream
hash to hide a change is a much louder, more deliberate act than quietly
editing one line, which is the actual threat model plain JSON-lines-append
had no defense against at all.
"""

import hashlib
import json


def _canonical_hash(fields: dict) -> str:
    return hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()


def chain_entry(prev_hash: str | None, fields: dict) -> dict:
    """Returns fields with prev_hash/entry_hash added. entry_hash covers the
    full entry INCLUDING prev_hash, so it's a real link, not two independent
    hashes that happen to sit in the same record."""
    entry = {**fields, "prev_hash": prev_hash or "genesis"}
    entry["entry_hash"] = _canonical_hash(entry)
    return entry


def verify_chain(records: list[dict]) -> list[dict]:
    """Returns a list of break descriptions — empty means the chain is
    intact. Checks, per record: (1) its own entry_hash matches a fresh
    recompute of its content, (2) its prev_hash matches the actual previous
    record's entry_hash (catches reordering/deletion, not just edits)."""
    breaks = []
    prev_hash = None
    for i, r in enumerate(records):
        claimed_hash = r.get("entry_hash")
        claimed_prev = r.get("prev_hash")
        content = {k: v for k, v in r.items() if k != "entry_hash"}
        expected_hash = _canonical_hash(content)
        expected_prev = prev_hash or "genesis"

        if claimed_prev != expected_prev:
            breaks.append({
                "index": i, "issue": "prev_hash mismatch (reordered or deleted entry)",
                "expected": expected_prev, "found": claimed_prev,
            })
        if claimed_hash != expected_hash:
            breaks.append({
                "index": i, "issue": "entry_hash mismatch (content altered after the fact)",
                "expected": expected_hash, "found": claimed_hash,
            })
        prev_hash = claimed_hash
    return breaks
