"""in-toto attestation export for PCP evidence.

2026-07-17 (build plan 2.1): PCP's per-check evidence records are semantically
in-toto attestations (subject = file/criterion, predicate = QA outcome) but
lived in a bespoke JSONL schema. This module exports them as in-toto
Statement v1 objects wrapped in DSSE envelopes so GUAC/Archivista/cosign-
verify tooling can consume them — "PCP evidence is in-toto-compatible" is a
stronger claim than "PCP has its own audit format".

HONEST BOUNDARY: envelopes are exported UNSIGNED (signatures: []) unless
cosign is installed and --sign is passed. Unsigned DSSE is structurally
valid and machine-readable but provides no non-repudiation — the hash-chain
(evidence_chain.py) remains the tamper-evidence layer until Sigstore signing
(build-plan 2.2) is completed. Stated here and in the export itself.
"""

import base64
import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from pcp import telemetry

STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
PREDICATE_TYPE = "https://pcp-protocol.com/attestation/qa-evidence/v1"
DSSE_PAYLOAD_TYPE = "application/vnd.in-toto+json"


def _sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def build_statements(pcp_dir: Path) -> list[dict]:
    """One in-toto Statement per qa-cycle telemetry record that has a
    resolvable control and at least one subject file still on disk."""
    project_root = pcp_dir.parent
    statements = []
    for rec in telemetry.load(pcp_dir):
        if rec.get("cycle") != "qa" or not rec.get("control_id"):
            continue
        subjects = []
        for f in rec.get("files") or []:
            digest = _sha256_file(project_root / f)
            if digest:
                subjects.append({"name": f, "digest": {"sha256": digest}})
        if not subjects:
            continue
        statements.append({
            "_type": STATEMENT_TYPE,
            "subject": subjects,
            "predicateType": PREDICATE_TYPE,
            "predicate": {
                "control_id": rec.get("control_id"),
                "check": rec.get("check"),
                "result": rec.get("result"),
                "module": rec.get("module"),
                "criterion_id": rec.get("criterion_id"),
                "attempt": rec.get("cycle_number"),
                "timestamp": rec.get("timestamp"),
                "evidence_path": rec.get("evidence_path"),
                "entry_hash": rec.get("entry_hash"),
                "note": "subject digests are CURRENT file state at export time, "
                        "not at check time — PCP telemetry does not yet snapshot "
                        "per-check content digests (honest limitation)",
            },
        })
    return statements


def _dsse_envelope(statement: dict) -> dict:
    payload = base64.b64encode(json.dumps(statement, sort_keys=True).encode()).decode()
    return {"payloadType": DSSE_PAYLOAD_TYPE, "payload": payload, "signatures": []}


def export_attestations(pcp_dir: Path, sign: bool = False) -> tuple[Path, int, str]:
    """Write .pcp/attestations.jsonl (one DSSE envelope per line).
    Returns (path, count, signing_note)."""
    statements = build_statements(pcp_dir)
    out = pcp_dir / "attestations.jsonl"
    with open(out, "w") as f:
        for s in statements:
            f.write(json.dumps(_dsse_envelope(s)) + "\n")

    signing_note = "UNSIGNED (no non-repudiation; hash-chain remains the tamper-evidence layer)"
    if sign:
        if shutil.which("cosign"):
            bundle = pcp_dir / "attestations.jsonl.sig"
            result = subprocess.run(
                ["cosign", "sign-blob", "--yes", str(out),
                 "--output-signature", str(bundle)],
                capture_output=True, text=True, timeout=300,
            )
            signing_note = (
                f"signed via cosign -> {bundle.name}" if result.returncode == 0
                else f"cosign signing FAILED: {(result.stderr or '').strip()[:200]}"
            )
        else:
            signing_note = "--sign requested but cosign not installed — exported unsigned"

    meta = {
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(statements), "signing": signing_note,
    }
    (pcp_dir / "attestations.meta.json").write_text(json.dumps(meta, indent=2))
    return out, len(statements), signing_note
