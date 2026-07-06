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

import yaml
from pathlib import Path

from pcp.pcp_dir import get_ontology_state
from pcp import ontology_review_log

AMBIGUOUS_SCORE_THRESHOLD = 0.65


class ReviewError(Exception):
    pass


def apply_review_action(pcp_dir: Path, item_id: str, action: str, new_label: str | None = None) -> dict:
    """Shared logic behind `pcp ontology-review` and `pcp ontology-serve`'s
    approve/reject/edit buttons — one implementation, two callers, so the
    CLI and the live dashboard can never drift apart on what an action does.

    action: "approve" | "reject" | "edit" (edit requires new_label).
    Raises ReviewError with a human-readable message on any failure (no
    state file, unknown id, missing label for edit) — callers translate
    that into a CLI exit code or an HTTP error response as appropriate.
    Returns {"kind": "node"|"edge", "action": action}.
    """
    if action == "edit" and not new_label:
        raise ReviewError("--edit requires a new label")

    state_path = get_ontology_state(pcp_dir)
    if not state_path.exists():
        raise ReviewError("No ontology_state.yaml — run `pcp ontology-extract` first.")

    state = yaml.safe_load(state_path.read_text()) or {}
    nodes = state.get("nodes", [])
    edges = state.get("edges", [])

    item = next((n for n in nodes if n["id"] == item_id), None)
    collection, kind = nodes, "node"
    if item is None:
        item = next((e for e in edges if e["id"] == item_id), None)
        collection, kind = edges, "edge"

    if item is None:
        raise ReviewError(f"no node or edge with id '{item_id}' in ontology_state.yaml.")

    original_confidence_score = item.get("confidence_score")

    if action == "reject":
        collection.remove(item)
    elif action == "approve":
        item["review_status"] = "green"
    elif action == "edit":
        if kind == "node":
            item["label"] = new_label
        else:
            item["relation"] = new_label
        item["review_status"] = "green"
    else:
        raise ReviewError(f"unknown action '{action}' (must be approve/reject/edit)")

    state_path.write_text(yaml.dump(
        {"generated_at": state.get("generated_at"), "nodes": nodes, "edges": edges},
        default_flow_style=False, sort_keys=False,
    ))

    ontology_review_log.record(
        pcp_dir, item_id=item_id, kind=kind, action=action,
        original_confidence_score=original_confidence_score,
        new_label=new_label if action == "edit" else None,
    )

    return {"kind": kind, "action": action}


def extract_ontology(project_root: Path) -> dict:
    """Returns {"available": False} if graphify isn't installed (same
    try/except-ImportError shape as coupling.py's compute_communities).
    Otherwise {"available": True, "nodes": [...], "edges": [...]} in PCP's
    own normalized shape, all review_status defaulted (nothing starts green —
    green is only ever reached via an explicit human review decision).

    Prefers `graphify-out/graph.json` when it exists — the output of a full
    `/graphify` skill run (structural AST + semantic subagent extraction),
    which carries the actual meaning layer (rationale/concept nodes,
    conceptually_related_to/semantically_similar_to edges, real AMBIGUOUS-
    confidence items). Confirmed bug, found by actually looking: the plain
    graphify.extract() library call this used to always go through is
    AST-only — it can never produce a red item, because pure code structure
    has no ambiguity to flag. Falls back to that plain call only when no
    `/graphify` run has ever produced graph.json, so `pcp ontology-extract`
    still works standalone without requiring the heavier skill pipeline."""
    graph_json_path = project_root / "graphify-out" / "graph.json"
    if graph_json_path.exists():
        return _extract_from_graph_json(graph_json_path)

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


def _extract_from_graph_json(graph_json_path: Path) -> dict:
    """graph.json is networkx node-link format: nodes carry file_type/label/
    source_file same as the plain extract() output; edges live under "links"
    (not "edges") but with the identical field shape (source/target/relation/
    confidence/confidence_score/source_file) — _normalize_node/_normalize_edge
    apply unchanged."""
    import json
    data = json.loads(graph_json_path.read_text())
    nodes = [_normalize_node(n) for n in data.get("nodes", [])]
    edges = [_normalize_edge(e) for e in data.get("links", [])]
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


def to_display_items(state: dict) -> list[dict]:
    """Flattens nodes+edges from ontology_state.yaml into one list shaped for
    a review UI: {id, kind, name, detail, file, status}. Shared by the
    ontology-serve API and any future consumer that needs the same flat
    display shape (avoids re-deriving this per caller)."""
    items = []
    for n in state.get("nodes", []):
        items.append({
            "id": n["id"], "kind": "node", "name": n.get("label", n["id"]),
            "detail": n.get("file_type", ""), "file": n.get("source_file") or "",
            "status": n["review_status"],
        })
    for e in state.get("edges", []):
        items.append({
            "id": e["id"], "kind": "edge",
            "name": f"{e['source']} -{e['relation']}-> {e['target']}",
            "detail": e.get("confidence") or "", "file": e.get("source_file") or "",
            "status": e["review_status"],
            # from/to: unused by the table view, needed by the graph view
            # (vis-network requires an edge's endpoints as distinct fields,
            # not embeddable in "name") -- kept on the same shared item shape
            # rather than a second flatten function, so both UIs stay in sync.
            "from": e["source"], "to": e["target"],
        })
    return items
