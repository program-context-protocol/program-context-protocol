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

`set_append_only`/`clear_append_only` add a real (if partial) second layer,
2026-08-07: the macOS user-settable `uappnd` file flag, which the kernel
enforces by rejecting any write that isn't a true append (a plain `open(p,
"a")`/O_APPEND write still succeeds; a truncate-and-rewrite does not, EPERM).
This stops a casual/accidental edit (an editor save, a script doing
`path.write_text(...)`) at the OS level instead of only detecting it after
the fact. It is still not tamper-PROOF: `uappnd` is user-settable, so
whoever owns the file can `chflags nouappnd` it and edit freely — no local
file-flag mechanism defends against a fully deliberate, privileged actor;
that needs a remote/signed ledger, out of scope here. Best-effort and silent
everywhere it can't apply (non-macOS, a filesystem without flag support) —
this must never be the thing that breaks a real evidence write.
"""

import hashlib
import json
import platform
import subprocess
from pathlib import Path


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
    """Returns a list of finding descriptions, each tagged `severity`
    ("critical" or "info") — empty means every record is either verified
    intact or honestly unchained. Checks, per record: (1) its own entry_hash
    matches a fresh recompute of its content, (2) its prev_hash matches the
    actual previous record's entry_hash (catches reordering/deletion, not
    just edits).

    A record with NEITHER `prev_hash` nor `entry_hash` present is "info", not
    "critical" — it never claimed to be chained (a legacy entry written
    before hash-chaining was adopted, or an ad-hoc write that bypassed the
    record() API entirely), so there is no hash claim to have been broken.
    Conflating that with a real break was a real bug, found 2026-08-08 on
    first live contact with `pcp build`'s new hard-fail-on-broken-chain gate
    (chain_guard.assert_chain_integrity): a win2mac decision_log.jsonl entry
    hand-appended by a build-session agent (bypassing decision_log.record()
    entirely — real, dated evidence of the exact ad-hoc-bypass problem
    CTRL-037 exists to catch) got flagged identically to actual tampering
    and hard-blocked an unrelated build. The chain naturally re-anchors at
    "genesis" after an unchained record (matches decision_log.py's own
    `_last_entry_hash`, which reads `.get("entry_hash")` — None on a legacy
    entry, so the next real record() call restarts the chain there too)."""
    breaks = []
    prev_hash = None
    for i, r in enumerate(records):
        has_prev = "prev_hash" in r
        has_hash = "entry_hash" in r
        if not has_prev and not has_hash:
            breaks.append({
                "index": i, "severity": "info",
                "issue": "unchained (legacy entry or a write that bypassed record()'s API — never claimed a hash, nothing to verify)",
            })
            prev_hash = None
            continue

        claimed_hash = r.get("entry_hash")
        claimed_prev = r.get("prev_hash")
        content = {k: v for k, v in r.items() if k != "entry_hash"}
        expected_hash = _canonical_hash(content)
        expected_prev = prev_hash or "genesis"

        if claimed_prev != expected_prev:
            breaks.append({
                "index": i, "severity": "critical",
                "issue": "prev_hash mismatch (reordered or deleted entry)",
                "expected": expected_prev, "found": claimed_prev,
            })
        if claimed_hash != expected_hash:
            breaks.append({
                "index": i, "severity": "critical",
                "issue": "entry_hash mismatch (content altered after the fact)",
                "expected": expected_hash, "found": claimed_hash,
            })
        prev_hash = claimed_hash
    return breaks


def set_append_only(path: Path) -> None:
    """Best-effort: mark `path` append-only (macOS `chflags uappnd`) right
    after a write that appended to it. No-op off macOS, or if the flag
    can't be set (filesystem doesn't support it, permissions) — the write
    that already happened must never be undone by a failure here."""
    if platform.system() != "Darwin":
        return
    try:
        subprocess.run(["chflags", "uappnd", str(path)], capture_output=True, timeout=5)
    except Exception:
        pass


def clear_append_only(path: Path) -> None:
    """Inverse of set_append_only — needed before a read-modify-rewrite
    write (bypass_log.yaml is a single YAML document, not JSONL, so its
    writer can't use a pure O_APPEND write). Same best-effort/no-op posture."""
    if platform.system() != "Darwin":
        return
    try:
        subprocess.run(["chflags", "nouappnd", str(path)], capture_output=True, timeout=5)
    except Exception:
        pass
