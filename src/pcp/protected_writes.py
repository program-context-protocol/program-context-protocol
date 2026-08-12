"""Tracks which exact content of a `protected_path`-scoped file was written
through a sanctioned path (pcp init's scaffold, or the propose/diff/approve
mechanic behind pcp correct-objective / pcp amend / pcp kickoff / pcp pm),
so check.py's protected_path rule can tell "this content was approved" apart
from "this content just appeared in the working tree" without caring how it
got there -- a human editing a file by hand and a non-PCP agent editing it
look identical to git, and there is no way to tell them apart after the
fact. Hard rule 2 was previously enforced only inside pcp build's own
unattended coding-agent session (PCP_AGENT_SESSION=1) -- a real gap for any
other agent (or human) committing directly outside that one harness,
confirmed by an independent cold-clone review, 2026-08-12.

Not tamper-proof: this store is a plain JSON file with no integrity
protection of its own, so an actor able to edit the protected file directly
could in principle also edit this store to match. Disclosed here rather
than pretending otherwise -- closing that would mean hash-chaining this
store the way evidence_chain.py does telemetry/decision logs, deliberately
out of scope for this fix."""

import hashlib
import json
from pathlib import Path

_STORE_NAME = "approved_write_hashes.json"


def pcp_dir_of(path: Path) -> Path:
    """Walks up from `path` to find the ancestor directory literally named
    `.pcp`. Lets a write helper stamp approval without threading a separate
    pcp_dir parameter through every call site, as long as the path it wrote
    is under pcp_dir (true for every protected-path write in this codebase)."""
    path = Path(path)
    for parent in [path] + list(path.parents):
        if parent.name == ".pcp":
            return parent
    return path.parent  # shouldn't happen for a genuine .pcp/ write


def _store_path(pcp_dir: Path) -> Path:
    return Path(pcp_dir) / _STORE_NAME


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _load(pcp_dir: Path) -> dict:
    p = _store_path(pcp_dir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _save(pcp_dir: Path, data: dict) -> None:
    _store_path(pcp_dir).write_text(json.dumps(data, indent=2, sort_keys=True))


def record_approved_write(pcp_dir: Path, abs_path: Path, content: str) -> None:
    """Call immediately after writing a protected file through a sanctioned
    path. Stamps this exact content's hash as approved."""
    data = _load(pcp_dir)
    data[str(Path(abs_path).resolve())] = _hash(content)
    _save(pcp_dir, data)


def is_approved_exact(pcp_dir: Path, abs_path: Path, content: str) -> bool:
    """Strict check, no grandfathering: True only if this exact content was
    previously stamped. Used for pcp build's own agent-session path, where
    the original design intent (never write protected files directly) stays
    absolute -- an unattended build loop gets no first-run leniency."""
    data = _load(pcp_dir)
    return data.get(str(Path(abs_path).resolve())) == _hash(content)


def check_approved(pcp_dir: Path, abs_path: Path, content: str) -> tuple[bool, bool]:
    """Returns (is_approved, was_grandfathered). A path with NO prior record
    at all gets a one-time grandfather pass (auto-approved and stamped) --
    an existing project upgrading to this mechanism must not have every
    already-legitimate file suddenly block on its very next commit, just
    because this store didn't exist yet when that content was written. A
    path that already HAS a record is held to it strictly."""
    data = _load(pcp_dir)
    key = str(Path(abs_path).resolve())
    if key not in data:
        record_approved_write(pcp_dir, abs_path, content)
        return True, True
    return data[key] == _hash(content), False
