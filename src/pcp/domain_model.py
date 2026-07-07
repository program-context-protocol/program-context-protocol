"""PCP's own domain model — 5 objects, 5 relationships, matching the
Palantir-simple discipline applied back to PCP itself: Objective, Module,
Criterion, Requirement, Gate. No graphify, no extraction, no review
workflow -- every fact here is already asserted by a structured, human-
authored or auto-generated file (objective.md, spec.yaml, acceptance.yaml,
brd_items.yaml, traceability_map.yaml, telemetry.jsonl, controls.yaml).
Nothing here is an uncertain claim needing approve/reject; it's a read-only
unification of facts that already existed in five separate places.

Relationships:
- Module covers Objective       (spec.yaml's objective_coverage)
- Module contains Criterion     (acceptance.yaml)
- Module depends on Module      (spec.yaml's dependencies)
- Requirement maps to Module    (traceability_map.yaml, green links only --
                                  only human-confirmed mappings count as a
                                  fact here, not every suggested candidate)
- Gate evaluates Criterion      (derived from telemetry.jsonl's per-
                                  criterion QA records -- the one edge that
                                  wasn't explicit anywhere before this)
"""

from collections import defaultdict
from pathlib import Path

import yaml

from pcp.traceability import load_active_brd_items, load_modules_info, module_criteria
from pcp import telemetry as telemetry_mod


def _load_controls(pcp_dir: Path) -> dict:
    path = pcp_dir / "controls.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return {c["id"]: c for c in data.get("controls", [])}


def _load_traceability_links(pcp_dir: Path) -> list[dict]:
    path = pcp_dir / "traceability_map.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    return [l for l in data.get("links", []) if l.get("review_status") == "green"]


def _gate_evaluates(pcp_dir: Path) -> dict:
    """{control_id: {criterion_key: {pass, fail, skip, total}}} derived from
    telemetry.jsonl's qa-cycle records -- the one relationship in this model
    that wasn't already explicit in a human-authored file."""
    records = [r for r in telemetry_mod.load(pcp_dir) if r.get("cycle") == "qa" and r.get("control_id")]
    by_gate = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for r in records:
        cid = r.get("control_id")
        module = r.get("module") or "?"
        criterion = r.get("criterion_id") or "?"
        key = f"{module}/{criterion}"
        result = r.get("result", "pass")
        by_gate[cid][key][result] += 1
        by_gate[cid][key]["total"] += 1
    return {cid: dict(v) for cid, v in by_gate.items()}


def build_domain_model(pcp_dir: Path) -> dict:
    """Pure aggregation over already-persisted files -- safe to call at any
    point, no side effects besides read, same posture as provenance.py."""
    objective_path = pcp_dir / "objective.md"
    objective_text = objective_path.read_text().strip() if objective_path.exists() else None

    modules = load_modules_info(pcp_dir)
    for m in modules:
        m["criteria"] = module_criteria(pcp_dir, m["name"])

    requirements = load_active_brd_items(pcp_dir)
    links = _load_traceability_links(pcp_dir)
    req_to_modules = defaultdict(list)
    for l in links:
        req_to_modules[l["feature_id"]].append(l["module"])
    for r in requirements:
        r["maps_to_modules"] = req_to_modules.get(r["id"], [])

    controls = _load_controls(pcp_dir)
    evaluates = _gate_evaluates(pcp_dir)
    gates = []
    for cid, c in controls.items():
        gates.append({
            "id": cid,
            "name": c.get("name", cid),
            "layer": c.get("layer"),
            "evaluates": evaluates.get(cid, {}),
        })

    return {
        "objective": objective_text,
        "modules": modules,
        "requirements": requirements,
        "gates": gates,
    }
