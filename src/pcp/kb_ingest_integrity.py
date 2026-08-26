"""kb_ingest_integrity.py — A011: ingestion integrity gate (kb module).

Real precedent this ports: a comparable project's own deterministic
`kb_integrity_check.py` -- a grep-based, no-LLM scanner run over freshly
fetched external content BEFORE it is trusted as real documentation. Real
incident that class of check catches: a page that was actually a
JS-driven video-listing app-shell (its real content just says "No video
found." because the scraper couldn't execute the JS) got mislabeled as a
real technical document in a self-documented ingestion campaign --
caught only because a deterministic integrity scanner existed. This
module exists to catch exactly that failure mode here.

Three deterministic checks (logic_tier 1, zero LLM calls):

1. SPA-shell / bot-wall / junk-page fingerprint detection -- a curated,
   fixed list of phrases that indicate the fetched page is a client-side
   app shell or a bot-wall challenge page, not real content
   (`detect_fingerprint_flags`).
2. Unstripped-HTML detection -- raw HTML markup left over from a fetch
   that should have been converted to clean text/markdown
   (`detect_unstripped_html`).
3. Exact-duplicate detection via hash -- the same content (by sha256)
   appearing twice within one ingestion batch, or matching content
   already on record in `.pcp/kb/ingest_state.yaml` from an earlier run
   (`check_ingestion_batch`'s duplicate pass).

Honesty about false positives (both checks 1 and 2 have a real class):
a page whose actual SUBJECT is one of the fingerprint phrases (a doc
explaining HTTP 404 semantics, a security writeup about bot-wall
detection) will match check 1; a doc that legitimately shows HTML/XML
syntax examples can match check 2. Neither check silently drops content
-- every match is FLAGGED for human/caller review via
`.pcp/kb/integrity_flags.yaml`, never auto-deleted. Same
disclosed-not-silently-blocked posture as this module's siblings
(kb_ingest.py's staged-candidate / ingest-state machinery,
kb_grounding.py's claim tiers).

Deliberately does NOT import kb_ingest.py -- that module imports THIS one
(A010's `ingest_approved_candidates` calls `check_ingestion_batch` as its
gate before the catalog/index rebuild step), so this module reads
`ingest_state.yaml`'s own on-disk shape directly rather than importing
kb_ingest.py's loader, to avoid a circular import.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

# ── check 1: SPA-shell / bot-wall / junk-page fingerprints ─────────────────
#
# A small, curated, fixed list -- deliberately not exhaustive or fuzzy.
# Case-insensitive substring match against the whole fetched body. Real
# false-positive class: a genuine technical document whose SUBJECT is one
# of these phrases (an HTTP-status-codes reference explaining what "404
# Not Found" means, a bot-detection writeup discussing "access denied"
# challenge pages). That is exactly why a match here is a FLAG for human
# review, never an automatic drop.
SHELL_FINGERPRINTS: tuple[str, ...] = (
    # SPA / client-side-render shells that never got their JS executed
    "enable javascript",
    "please enable javascript",
    "javascript is disabled",
    "you need to enable javascript to run this app",
    "this app requires javascript",
    "no video found",
    "no results found",
    "loading...",
    # bot-wall / anti-scraping challenge pages
    "just a moment...",
    "checking your browser before accessing",
    "ddos protection by cloudflare",
    "attention required! | cloudflare",
    "are you a human",
    "please verify you are a human",
    "please verify you are human",
    "complete the captcha",
    "captcha",
    "unusual traffic from your computer network",
    # junk / error pages
    "access denied",
    "403 forbidden",
    "404 not found",
    "page not found",
    "the page you requested was not found",
    "this site can't be reached",
    "this page isn't working",
)

FLAG_FINGERPRINT = "fingerprint"
FLAG_UNSTRIPPED_HTML = "unstripped_html"
FLAG_DUPLICATE = "duplicate"

FINGERPRINT_FALSE_POSITIVE_NOTE = (
    "Could be a false positive if the page's real SUBJECT legitimately discusses "
    "this phrase (e.g. a doc explaining HTTP 404 semantics, or a security writeup "
    "about bot-wall detection) -- review the stored content, don't auto-drop it."
)


def detect_fingerprint_flags(content: str) -> list[str]:
    """Returns every SHELL_FINGERPRINTS phrase found as a case-insensitive
    substring anywhere in `content` -- empty list if none match. Grep-based
    on purpose (same posture as the ported precedent): no NLP, no
    tokenization, no relevance judgment, just a fixed-string scan."""
    lowered = content.lower()
    return [phrase for phrase in SHELL_FINGERPRINTS if phrase in lowered]


# ── check 2: unstripped HTML ─────────────────────────────────────────────────
#
# Two signals, either sufficient on its own:
#   (a) a "strong" whole-document signal (doctype/html/head/body/script/
#       style) -- content that is clearly still a raw HTML document, not
#       converted text.
#   (b) tag DENSITY above a threshold, for content with no strong signal
#       but many inline tags (a fragment rather than a full page).
# Real false-positive class: a genuine technical document that legitimately
# shows HTML/XML syntax as an example (a tutorial explaining `<div>` usage,
# an XML config sample) can trip the density signal -- same
# flag-don't-drop posture as the fingerprint check above.

_HTML_STRONG_SIGNALS: tuple[str, ...] = (
    "<!doctype html", "<html", "<head>", "<head ", "</head>",
    "<body>", "<body ", "</body>", "<script", "<style",
)
_HTML_TAG_RE = re.compile(r"<[a-zA-Z][a-zA-Z0-9]*(?:\s[^<>]*)?/?>")
DEFAULT_HTML_TAG_DENSITY_THRESHOLD = 8


def detect_unstripped_html(
    content: str,
    tag_count_threshold: int = DEFAULT_HTML_TAG_DENSITY_THRESHOLD,
) -> dict | None:
    """Returns {"strong_signals": [...], "tag_count": N} when `content`
    looks like it still carries raw HTML markup that should have been
    stripped to clean text/markdown before storage -- None when clean.

    Triggers on either a strong whole-document signal (doctype/html/head/
    body/script/style, any one is sufficient) or a generic-tag count at or
    above `tag_count_threshold` with no strong signal required -- catches
    a raw HTML fragment even without a full <html>...</html> wrapper.
    """
    lowered = content.lower()
    strong = [s for s in _HTML_STRONG_SIGNALS if s in lowered]
    tag_matches = _HTML_TAG_RE.findall(content)
    if strong or len(tag_matches) >= tag_count_threshold:
        return {"strong_signals": strong, "tag_count": len(tag_matches)}
    return None


# ── check 3: exact-duplicate detection via hash ─────────────────────────────
#
# Reuses the sha256 A010 (kb_ingest.py's ingest_approved_candidates) already
# computes per stored file (IngestResult.content_hash) rather than
# rehashing -- one hash of record per stored file, not two competing ones.

INTEGRITY_FLAGS_FILENAME = "integrity_flags.yaml"
INGEST_STATE_FILENAME = "ingest_state.yaml"

FLAG_STATUS_PENDING = "pending"
FLAG_STATUS_CLEARED = "cleared"


def _load_known_hashes(pcp_dir: Path) -> dict[str, str]:
    """content_hash -> stored_path, read directly from
    `.pcp/kb/ingest_state.yaml`'s own on-disk shape (A010's idempotency
    ledger) -- not via kb_ingest.py's loader, to avoid the circular import
    described in this module's docstring. Same defensive posture as every
    other kb_*.py reader: missing file or malformed YAML degrades to
    "nothing known" rather than raising."""
    path = Path(pcp_dir) / "kb" / INGEST_STATE_FILENAME
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(errors="replace")) or {}
    except yaml.YAMLError:
        return {}
    entries = data.get("ingested") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return {}
    known: dict[str, str] = {}
    for e in entries:
        if isinstance(e, dict) and e.get("content_hash") and e.get("stored_path"):
            known[str(e["content_hash"])] = str(e["stored_path"])
    return known


def _field(item, name, default=None):
    """Duck-typed accessor -- `item` may be a kb_ingest.IngestResult
    instance or a plain dict with the same field names (tests use the
    latter so this module never needs to import kb_ingest.py)."""
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


@dataclass
class IntegrityFlag:
    """One flagged item from one integrity check, over one stored file."""

    stored_path: str
    gap_topic_id: str
    url: str
    check: str  # FLAG_FINGERPRINT | FLAG_UNSTRIPPED_HTML | FLAG_DUPLICATE
    reason: str
    detail: str = ""


@dataclass
class IntegrityReport:
    """Result of running all three checks over one freshly-ingested batch."""

    checked: int
    flags: list[IntegrityFlag] = field(default_factory=list)

    @property
    def has_flags(self) -> bool:
        return bool(self.flags)


def check_ingestion_batch(
    pcp_dir: Path,
    batch: Sequence,
    known_hashes: dict[str, str] | None = None,
) -> IntegrityReport:
    """Runs all three checks over every freshly-stored item in `batch`
    (kb_ingest.IngestResult instances, or dicts with the same field names)
    whose `result` is "ingested" -- any other result kind (already_ingested,
    fetch_failed, empty_content) has no fresh stored content to scan and is
    skipped, never dereferenced as a path.

    `known_hashes` (content_hash -> stored_path) defaults to whatever is
    already on record in `.pcp/kb/ingest_state.yaml`; overridable for
    tests/callers that already hold it (same dependency-injection posture
    as kb_ingest.py's own `fetcher` argument).

    Returns a structured IntegrityReport -- what was flagged, which check,
    why -- never raises on a missing/unreadable stored file (skipped, same
    posture as every other kb_*.py reader degrading rather than crashing).
    """
    pcp_dir = Path(pcp_dir)
    known_hashes = dict(known_hashes) if known_hashes is not None else _load_known_hashes(pcp_dir)

    loaded: list[tuple] = []  # (stored_path, gap_topic_id, url, content_hash, content)
    checked = 0

    for item in batch:
        if _field(item, "result") != "ingested":
            continue
        stored_path = _field(item, "stored_path")
        if not stored_path:
            continue
        checked += 1

        full_path = pcp_dir / "kb" / stored_path
        try:
            content = full_path.read_text(errors="replace")
        except OSError:
            continue

        loaded.append((
            stored_path,
            _field(item, "gap_topic_id", ""),
            _field(item, "url", ""),
            _field(item, "content_hash"),
            content,
        ))

    flags: list[IntegrityFlag] = []
    hash_to_paths: dict[str, list[tuple[str, str, str]]] = {}

    for stored_path, gap_topic_id, url, content_hash, content in loaded:
        fingerprints = detect_fingerprint_flags(content)
        if fingerprints:
            flags.append(IntegrityFlag(
                stored_path=stored_path, gap_topic_id=gap_topic_id, url=url,
                check=FLAG_FINGERPRINT,
                reason=f"matched shell/bot-wall/junk-page fingerprint(s): {', '.join(fingerprints)}",
                detail=FINGERPRINT_FALSE_POSITIVE_NOTE,
            ))

        html = detect_unstripped_html(content)
        if html is not None:
            signal_note = f", signals: {', '.join(html['strong_signals'])}" if html["strong_signals"] else ""
            flags.append(IntegrityFlag(
                stored_path=stored_path, gap_topic_id=gap_topic_id, url=url,
                check=FLAG_UNSTRIPPED_HTML,
                reason=f"raw HTML markup left in stored content ({html['tag_count']} tag(s){signal_note})",
                detail="Fetch should have converted this to clean text/markdown before storage.",
            ))

        if content_hash:
            # against content already ingested in an earlier run
            prior_path = known_hashes.get(content_hash)
            if prior_path and prior_path != stored_path:
                flags.append(IntegrityFlag(
                    stored_path=stored_path, gap_topic_id=gap_topic_id, url=url,
                    check=FLAG_DUPLICATE,
                    reason=f"exact-duplicate content already ingested at {prior_path}",
                ))
            hash_to_paths.setdefault(content_hash, []).append((stored_path, gap_topic_id, url))

    # against other items in this same batch
    for content_hash, entries in hash_to_paths.items():
        if len(entries) < 2:
            continue
        all_paths = [e[0] for e in entries]
        for stored_path, gap_topic_id, url in entries:
            others = [p for p in all_paths if p != stored_path]
            flags.append(IntegrityFlag(
                stored_path=stored_path, gap_topic_id=gap_topic_id, url=url,
                check=FLAG_DUPLICATE,
                reason=f"exact-duplicate content within this batch: {', '.join(others)}",
            ))

    return IntegrityReport(checked=checked, flags=flags)


# ── flag ledger: .pcp/kb/integrity_flags.yaml ───────────────────────────────
#
# Same write/load/status-transition shape as kb_ingest.py's own
# candidates.yaml (write_staged_candidates/load_staged_candidates):
# append-and-dedupe by key, forward-only status transitions, never
# regress a resolved row backward on a re-scan.

def write_integrity_flags(pcp_dir: Path, flags: Sequence[IntegrityFlag]) -> Path:
    """Appends `flags` to `.pcp/kb/integrity_flags.yaml`, deduped by
    (stored_path, check) so re-running check_ingestion_batch over the same
    stored file never piles up duplicate rows. A row a human has already
    advanced to FLAG_STATUS_CLEARED is left untouched -- this never
    regresses a cleared flag back to pending, same posture as
    write_staged_candidates never regressing an approved/rejected
    candidate back to 'staged'."""
    pcp_dir = Path(pcp_dir)
    kb_dir = pcp_dir / "kb"
    kb_dir.mkdir(parents=True, exist_ok=True)
    out = kb_dir / INTEGRITY_FLAGS_FILENAME

    existing: dict = {}
    if out.exists():
        try:
            existing = yaml.safe_load(out.read_text(errors="replace")) or {}
        except yaml.YAMLError:
            existing = {}
    if not isinstance(existing, dict):
        existing = {}

    rows = existing.get("flags")
    rows = list(rows) if isinstance(rows, list) else []
    index = {
        (r.get("stored_path"), r.get("check")): i
        for i, r in enumerate(rows) if isinstance(r, dict)
    }

    for f in flags:
        key = (f.stored_path, f.check)
        row = {
            "stored_path": f.stored_path,
            "gap_topic_id": f.gap_topic_id,
            "url": f.url,
            "check": f.check,
            "reason": f.reason,
            "detail": f.detail,
            "status": FLAG_STATUS_PENDING,
        }
        if key in index:
            prior = rows[index[key]]
            prior_status = prior.get("status") if isinstance(prior, dict) else None
            if prior_status == FLAG_STATUS_CLEARED:
                continue  # never regress a cleared flag back to pending
            rows[index[key]] = row
        else:
            index[key] = len(rows)
            rows.append(row)

    existing["flags"] = rows
    existing["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out.write_text(yaml.safe_dump(existing, sort_keys=False))
    return out


def load_integrity_flags(pcp_dir: Path) -> list[dict]:
    """Pure read of `.pcp/kb/integrity_flags.yaml`'s `flags` list -- same
    posture as kb_ingest.load_staged_candidates: empty list on a missing
    file, malformed YAML, or a non-list field, never a crash."""
    path = Path(pcp_dir) / "kb" / INTEGRITY_FLAGS_FILENAME
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(errors="replace")) or {}
    except yaml.YAMLError:
        return []
    rows = data.get("flags") if isinstance(data, dict) else None
    return rows if isinstance(rows, list) else []


def has_unresolved_flags(pcp_dir: Path) -> bool:
    """True when at least one row in integrity_flags.yaml is still
    FLAG_STATUS_PENDING -- this is the gate condition A010's
    ingest_approved_candidates checks before running the catalog/index
    rebuild. Deliberately checks EVERY pending row on file, not just this
    run's own batch: kb_catalog.py/kb_index.py do a full rebuild each time
    (no incremental staleness tracking -- see kb_catalog.py's module
    docstring), so a full rebuild triggered by an unrelated later ingest
    run would otherwise sweep an earlier run's still-unresolved flagged
    content back into the searchable index regardless of when it was
    flagged."""
    return any(
        isinstance(row, dict) and row.get("status") == FLAG_STATUS_PENDING
        for row in load_integrity_flags(pcp_dir)
    )


def clear_integrity_flag(pcp_dir: Path, stored_path: str, check: str, reason: str) -> bool:
    """Human/caller review clears one pending flag -- forward-only
    (pending -> cleared), same accountability posture as
    `[pcp-bypass: reason]` and `pcp objective-conflicts --dismiss`: a
    non-empty reason is mandatory, and the reason is recorded alongside
    the clear so the review trail is auditable, not just a boolean flip.

    Returns True if a matching pending row was found and cleared, False
    if no such row exists (already cleared, or never flagged) -- a no-op,
    never an error, for that case."""
    if not reason or not reason.strip():
        raise ValueError("clear_integrity_flag requires a non-empty reason")

    path = Path(pcp_dir) / "kb" / INTEGRITY_FLAGS_FILENAME
    if not path.exists():
        return False
    try:
        data = yaml.safe_load(path.read_text(errors="replace")) or {}
    except yaml.YAMLError:
        return False
    if not isinstance(data, dict) or not isinstance(data.get("flags"), list):
        return False

    cleared = False
    for row in data["flags"]:
        if (
            isinstance(row, dict)
            and row.get("stored_path") == stored_path
            and row.get("check") == check
            and row.get("status") == FLAG_STATUS_PENDING
        ):
            row["status"] = FLAG_STATUS_CLEARED
            row["cleared_reason"] = reason.strip()
            row["cleared_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            cleared = True

    if cleared:
        path.write_text(yaml.safe_dump(data, sort_keys=False))
    return cleared
