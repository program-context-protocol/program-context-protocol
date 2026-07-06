"""Ontology extraction + merge logic — the "what exists, what it means" layer
of Tier 2 (see project_pcp_palantir_vision.md memory for the full design).
Wraps graphify's deterministic AST extraction (library API, same optional-
dependency pattern as coupling.py's compute_communities) into PCP's own
node/edge shape with a human review state machine (red/blue/green) on top.

Scope note, stated plainly rather than overclaimed: graphify's library
extract() call is pure AST-derived structural extraction (imports, calls) —
fast, free, no LLM involved. Every AST-derived edge is EXTRACTED confidence
(graphify's own convention: EXTRACTED implies 1.0), so review_status starts
"blue" (high-confidence, unreviewed) for everything produced here. graphify's
deeper semantic pass — the one that produces INFERRED/AMBIGUOUS edges from
docs/rationale, which is where "red" items would genuinely come from — runs
via subagent orchestration inside the `graphify` skill itself, not through
this plain library call. That's out of scope for this first slice; the red
bucket legitimately stays empty on a code-only extraction until semantic
extraction is layered on top later, and that's not a bug to hide.

Never touches objective.md/spec.yaml/etc. — same "advisory, additive only"
posture as pcp audit/pcp capture.
"""

from pathlib import Path

AMBIGUOUS_SCORE_THRESHOLD = 0.65


def extract_ontology(project_root: Path) -> dict:
    """Returns {"available": False} if graphify isn't installed (same
    try/except-ImportError shape as coupling.py's compute_communities).
    Otherwise {"available": True, "nodes": [...], "edges": [...]} in PCP's
    own normalized shape, all review_status defaulted (nothing starts green —
    green is only ever reached via an explicit human review decision)."""
    try:
        from graphify.extract import extract, collect_files
    except ImportError:
        return {"available": False}

    files = collect_files(project_root)
    if not files:
        return {"available": True, "nodes": [], "edges": []}

    raw = extract(files, parallel=True)

    nodes = [_normalize_node(n) for n in raw.get("nodes", [])]
    edges = [_normalize_edge(e) for e in raw.get("edges", [])]
    return {"available": True, "nodes": nodes, "edges": edges}


def _initial_review_status(confidence: str | None, confidence_score: float | None) -> str:
    """AMBIGUOUS, or a numeric score below threshold, starts red (don't trust
    for anything decision-critical yet). Everything else starts blue
    (high-confidence but still unreviewed). Never starts green — green is
    reserved for an explicit human `pcp ontology-review --approve`."""
    if confidence == "AMBIGUOUS":
        return "red"
    if confidence_score is not None and confidence_score < AMBIGUOUS_SCORE_THRESHOLD:
        return "red"
    return "blue"


def _normalize_node(n: dict) -> dict:
    return {
        "kind": "node",
        "id": n["id"],
        "label": n.get("label", n["id"]),
        "file_type": n.get("file_type", "code"),
        "source_file": n.get("source_file"),
        "rationale": n.get("rationale"),
        "review_status": "blue",
    }


def _normalize_edge(e: dict) -> dict:
    confidence = e.get("confidence")
    confidence_score = e.get("confidence_score")
    if confidence_score is None and confidence == "EXTRACTED":
        confidence_score = 1.0
    return {
        "kind": "edge",
        "id": f"{e['source']}__{e.get('relation', 'related_to')}__{e['target']}",
        "source": e["source"],
        "target": e["target"],
        "relation": e.get("relation", "related_to"),
        "confidence": confidence,
        "confidence_score": confidence_score,
        "source_file": e.get("source_file"),
        "review_status": _initial_review_status(confidence, confidence_score),
    }


def merge_with_existing(fresh: dict, existing: dict | None, rejected_ids: set[str] | None = None) -> dict:
    """Re-extraction must not wipe prior human review decisions. Matches
    items by id: any item already 'green' (or edited) in the existing state
    keeps its review_status/label; anything new gets its freshly computed
    initial status; anything that no longer appears in the fresh extraction
    (e.g. the file was deleted) is dropped.

    rejected_ids (the review log's ids whose most recent action was 'reject')
    are dropped from the fresh extraction too — confirmed bug, caught by
    actually testing this: the underlying code fact still exists, so a plain
    id-not-in-existing-state check let a rejected item silently reappear on
    the very next extract. Reject has to be sticky the same way green is,
    or it's nearly useless for anything but a one-off transient artifact."""
    rejected_ids = rejected_ids or set()

    existing_nodes = {n["id"]: n for n in (existing or {}).get("nodes", [])}
    existing_edges = {e["id"]: e for e in (existing or {}).get("edges", [])}

    merged_nodes = []
    for n in fresh.get("nodes", []):
        if n["id"] in rejected_ids:
            continue
        prior = existing_nodes.get(n["id"])
        if prior and prior.get("review_status") == "green":
            merged = {**n, "review_status": "green", "label": prior.get("label", n["label"])}
        else:
            merged = n
        merged_nodes.append(merged)

    merged_edges = []
    for e in fresh.get("edges", []):
        if e["id"] in rejected_ids:
            continue
        prior = existing_edges.get(e["id"])
        if prior and prior.get("review_status") == "green":
            merged = {**e, "review_status": "green", "relation": prior.get("relation", e["relation"])}
        else:
            merged = e
        merged_edges.append(merged)

    return {"available": True, "nodes": merged_nodes, "edges": merged_edges}
