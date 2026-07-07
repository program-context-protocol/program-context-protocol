"""Requirements traceability map — "which code implements this feature," the
concrete goal named directly: map BRD items (business requirements) and
module specs (architecture) to actual code, so a human who already knows
their own project can verify the mapping instead of exploring a generic
code graph.

Deliberately NOT built on graphify's extraction — that pipeline treats every
parsed entity (including raw JSON schema keys) as equally "in the ontology,"
which is exactly the noise problem that made the code-ontology graph
unreadable. This works of PCP's own already-structured data instead:
.pcp/brd_items.yaml (features), module spec.yaml's objective_coverage
(architecture's claimed coverage), acceptance.yaml (concrete criteria),
current_state.md (real completion status). Nothing here is a generic
entity — every row is a feature, a module, or a criterion, by construction,
matching Palantir's actual Ontology Manager lesson: an object type IS a
business concept because a human deliberately modeled it that way, not
because a parser found it.

No link between a specific BRD item and a specific module exists yet in
PCP's own data model -- that's the real gap this closes. One LLM classify
pass (Haiku, matching gate.py/validate_module.py's judge-tier routing)
suggests candidate links with a confidence score and rationale; a human
approves/rejects/edits through the same review state machine as ontology.py
(red/blue/green, append-only audit log) -- reusing the pattern, not
duplicating it.
"""

from pathlib import Path

import yaml

from pcp.pcp_dir import get_modules_dir
from pcp import traceability_review_log
from pcp.llm import client as llm

AMBIGUOUS_SCORE_THRESHOLD = 0.65

SYSTEM_PROMPT = """\
You are analyzing which existing code modules likely implement a given \
business requirement, for a requirements traceability map.

You are given one requirement and a list of all existing modules (name, \
description, and what each module's spec.yaml claims to cover).

Return the module(s) that most likely implement or relate to this \
requirement, each with a confidence score (0.0-1.0) and a one-line \
rationale citing what in the module's description/coverage justifies the \
match. If NO module plausibly covers this requirement, return an empty \
list -- do not force a match onto the closest-sounding module. A \
requirement can legitimately have zero, one, or several matching modules.

Output ONLY valid JSON: {"matches": [{"module": "module_name", "confidence_score": 0.0, "rationale": "..."}]}
"""


class TraceabilityError(Exception):
    pass


def _load_active_brd_items(pcp_dir: Path) -> list[dict]:
    path = pcp_dir / "brd_items.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    return [i for i in data.get("items", []) if i.get("status") == "active"]


def _load_modules_info(pcp_dir: Path) -> list[dict]:
    modules_dir = get_modules_dir(pcp_dir)
    if not modules_dir.exists():
        return []
    modules = []
    for spec_path in sorted(modules_dir.glob("*/spec.yaml")):
        spec = yaml.safe_load(spec_path.read_text()) or {}
        if spec.get("deprecated"):
            continue
        modules.append({
            "name": spec.get("module", spec_path.parent.name),
            "description": (spec.get("description") or "").strip(),
            "objective_coverage": spec.get("objective_coverage") or [],
        })
    return modules


def _module_criteria(pcp_dir: Path, module_name: str) -> list[dict]:
    """Confirmed bug, same pattern as observatory.py's earlier one: raw
    acceptance.yaml status is the pre-scan default ("pending"), not the
    actually-recorded outcome. Must apply the current_state.md overlay the
    same way pcp status's fast path does, or a fully-built module (like
    gates, exercised heavily this whole session) shows every criterion as
    pending here."""
    acc_path = get_modules_dir(pcp_dir) / module_name / "acceptance.yaml"
    if not acc_path.exists():
        return []
    data = yaml.safe_load(acc_path.read_text()) or {}
    criteria = data.get("criteria", [])

    from pcp.commands.status import _parse_from_current_state
    statuses = _parse_from_current_state(pcp_dir / "current_state.md")
    return [
        {**c, "status": statuses.get(f"{module_name.upper()}/{c['id']}", c.get("status", "pending"))}
        for c in criteria
    ]


def _initial_review_status(confidence_score: float) -> str:
    return "red" if confidence_score < AMBIGUOUS_SCORE_THRESHOLD else "blue"


def build_traceability(pcp_dir: Path) -> dict:
    """One LLM classify call per active BRD item (Haiku, judge-tier —
    cheap, matches gate.py's routing). Returns
    {"available": True, "links": [...]} or {"available": False} if there
    are no active BRD items / no modules to match against (nothing to
    classify, not an error)."""
    items = _load_active_brd_items(pcp_dir)
    modules = _load_modules_info(pcp_dir)
    if not items or not modules:
        return {"available": False}

    modules_text = "\n\n".join(
        f"Module: {m['name']}\nDescription: {m['description']}\n"
        f"Objective coverage:\n" + "\n".join(f"- {c}" for c in m["objective_coverage"])
        for m in modules
    )

    links = []
    for item in items:
        user_prompt = (
            f"Requirement [{item['id']}]: {item['description']}\n\n"
            f"## Modules\n{modules_text}"
        )
        try:
            result = llm.call_json(
                SYSTEM_PROMPT, user_prompt, model=llm.JUDGE_MODEL,
                pcp_dir=pcp_dir, command="trace-map-classify",
            )
        except Exception:
            continue
        for m in result.get("matches", []):
            score = m.get("confidence_score", 0.0)
            links.append({
                "id": f"{item['id']}__{m['module']}",
                "feature_id": item["id"],
                "feature_description": item["description"],
                "module": m["module"],
                "confidence_score": score,
                "rationale": m.get("rationale", ""),
                "review_status": _initial_review_status(score),
            })

    return {"available": True, "links": links}


def merge_with_existing(fresh: dict, existing: dict | None, rejected_ids: set[str] | None = None) -> dict:
    """Same shape as ontology.merge_with_existing: preserve prior green/
    edited decisions by id across re-runs, drop ids the review log's most
    recent action marked rejected (same sticky-reject fix, applied here
    from the start instead of discovering it as a bug a second time)."""
    rejected_ids = rejected_ids or set()
    existing_by_id = {link["id"]: link for link in (existing or {}).get("links", [])}

    merged = []
    for link in fresh.get("links", []):
        if link["id"] in rejected_ids:
            continue
        prior = existing_by_id.get(link["id"])
        if prior and prior.get("review_status") == "green":
            merged.append({**link, "review_status": "green", "module": prior.get("module", link["module"])})
        else:
            merged.append(link)

    return {"available": True, "links": merged}


def apply_review_action(pcp_dir: Path, link_id: str, action: str, new_label: str | None = None) -> dict:
    """approve -> green, reject -> removed, edit -> relabel the module field
    + green. Same three actions, same audit-log shape as ontology.py's
    apply_review_action -- one pattern, reused for a different edge type."""
    if action == "edit" and not new_label:
        raise TraceabilityError("--edit requires a new module name")

    state_path = get_traceability_map(pcp_dir)
    if not state_path.exists():
        raise TraceabilityError("No traceability_map.yaml — run `pcp trace-map` first.")

    state = yaml.safe_load(state_path.read_text()) or {}
    links = state.get("links", [])
    link = next((l for l in links if l["id"] == link_id), None)
    if link is None:
        raise TraceabilityError(f"no link with id '{link_id}' in traceability_map.yaml.")

    original_confidence_score = link.get("confidence_score")

    if action == "reject":
        links.remove(link)
    elif action == "approve":
        link["review_status"] = "green"
    elif action == "edit":
        link["module"] = new_label
        link["review_status"] = "green"
    else:
        raise TraceabilityError(f"unknown action '{action}' (must be approve/reject/edit)")

    state_path.write_text(yaml.dump(
        {"generated_at": state.get("generated_at"), "links": links},
        default_flow_style=False, sort_keys=False,
    ))

    traceability_review_log.record(
        pcp_dir, link_id=link_id, action=action,
        original_confidence_score=original_confidence_score,
        new_label=new_label if action == "edit" else None,
    )

    return {"action": action}


def build_full_view(pcp_dir: Path) -> dict:
    """Everything the review UI needs in one call: active features, the
    modules catalog (each with its acceptance criteria so an approved link
    can drill down to concrete, status-tracked implementation units), and
    the current suggested/reviewed links. Small data volume (features and
    modules are both single-digit-to-low-double-digit counts) -- no
    pagination needed, unlike the code ontology's ~2500 items."""
    features = _load_active_brd_items(pcp_dir)
    modules = _load_modules_info(pcp_dir)
    for m in modules:
        m["criteria"] = _module_criteria(pcp_dir, m["name"])

    state_path = get_traceability_map(pcp_dir)
    links = []
    generated_at = None
    if state_path.exists():
        state = yaml.safe_load(state_path.read_text()) or {}
        links = state.get("links", [])
        generated_at = state.get("generated_at")

    return {"generated_at": generated_at, "features": features, "modules": modules, "links": links}


def get_traceability_map(pcp_dir: Path) -> Path:
    return pcp_dir / "traceability_map.yaml"
