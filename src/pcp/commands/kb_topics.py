"""pcp kb-topics — deterministically derives .pcp/kb/topics.yaml, the kb
module's topic_registry entity. Zero LLM calls: pure aggregation/regex over
four already-structured inputs already on disk --

1. objective/target_state scope: markdown headings extracted from
   objective.md and target_state.md (structural parsing, same rung as
   narrative_lint's stale-file detection -- no summarisation, no judgment).
2. build_vs_buy.candidates_considered: aggregated from every module's
   spec.yaml (module-level build_vs_buy) and every criterion's acceptance.yaml
   build_vs_buy, deduped by candidate string across the whole program.
3. dependency-manifest parse: regex/JSON extraction of declared package
   names from pyproject.toml / requirements.txt / package.json -- not a real
   TOML parser (tomllib is 3.11+, this project targets 3.10+), just enough
   structure-aware extraction to be exact on the array/object shapes these
   manifests actually use.
4. per-criterion logic_tier rung 2/3 flags: every criterion whose logic_tier
   is 2 (solver/optimization) or 3 (statistical/ML) -- these are the two
   rungs whose correctness rests on an external candidate/prior-art choice
   the same way build_vs_buy does (a specific solver library, a specific
   model/algorithm), so they are surfaced as grounding-review topics too.
   Rungs 4/5/6 are already covered by other mechanisms (retrieval-score
   checks, tier-distribution policy) -- this flag is deliberately narrow.

No fresh research call happens here, per the kb module's own constraint:
"topics.yaml is derived only from the four already-structured inputs -- no
fresh research call inside topic finalization itself." Same posture as
traceability.md/architecture_justification.md: pure aggregation over what's
already on disk, never hand-edited, this is a rollup view, not a second
place to author any of the four inputs."""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, get_modules_dir, NoPCPDir

console = Console()

_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
_DEP_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+")
_QUOTED_RE = re.compile(r"[\"']([^\"']+)[\"']")


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return {}


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower())
    return slug.strip("-") or "untitled"


def _extract_scope_headings(md_path: Path) -> list[str]:
    """Deterministic markdown-heading extraction. No LLM -- structural
    parsing only."""
    if not md_path.exists():
        return []
    text = md_path.read_text(errors="replace")
    return [h.strip() for h in _HEADING_RE.findall(text) if h.strip()]


def _strip_version(name: str) -> str:
    match = _DEP_NAME_RE.match(name.strip())
    return match.group(0).strip() if match else name.strip()


def _parse_pyproject_dependencies(path: Path) -> list[tuple[str, str]]:
    """Regex extraction, not a TOML parser -- pyproject.toml's dependency
    arrays are simple enough that pulling in tomllib/tomli isn't worth it
    here. Returns (name, group) pairs; group is 'dependencies' for the main
    array, or the optional-dependencies extra's own key name."""
    if not path.exists():
        return []
    text = path.read_text(errors="replace")
    results: list[tuple[str, str]] = []

    main_match = re.search(r"(?m)^dependencies\s*=\s*\[(.*?)\]", text, re.DOTALL)
    if main_match:
        for raw in _QUOTED_RE.findall(main_match.group(1)):
            results.append((_strip_version(raw), "dependencies"))

    opt_section = re.search(
        r"\[project\.optional-dependencies\](.*?)(?=\n\[|\Z)", text, re.DOTALL
    )
    if opt_section:
        for group_match in re.finditer(
            r"(?m)^([A-Za-z0-9_\-]+)\s*=\s*\[(.*?)\]", opt_section.group(1), re.DOTALL
        ):
            group_name, body = group_match.group(1), group_match.group(2)
            for raw in _QUOTED_RE.findall(body):
                results.append((_strip_version(raw), group_name))

    return results


def _parse_requirements_txt(path: Path) -> list[tuple[str, str]]:
    if not path.exists():
        return []
    results = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        results.append((_strip_version(line), "requirements.txt"))
    return results


def _parse_package_json(path: Path) -> list[tuple[str, str]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(errors="replace"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    results = []
    for group in ("dependencies", "devDependencies"):
        deps = data.get(group)
        if isinstance(deps, dict):
            for name in deps:
                results.append((name, group))
    return results


def _parse_dependency_manifests(project_root: Path) -> list[dict]:
    """Deterministic, zero LLM -- combines every manifest found, deduped by
    package name (first manifest/group wins), sorted for stable output."""
    entries: dict[str, dict] = {}
    for name, group in _parse_pyproject_dependencies(project_root / "pyproject.toml"):
        entries.setdefault(name, {"name": name, "manifest": "pyproject.toml", "group": group})
    for name, group in _parse_requirements_txt(project_root / "requirements.txt"):
        entries.setdefault(name, {"name": name, "manifest": "requirements.txt", "group": group})
    for name, group in _parse_package_json(project_root / "package.json"):
        entries.setdefault(name, {"name": name, "manifest": "package.json", "group": group})
    return [entries[k] for k in sorted(entries)]


def _collect_build_vs_buy_candidates(modules_dir: Path) -> list[dict]:
    """Aggregates candidates_considered from every module's spec.yaml
    (module-level build_vs_buy) and every criterion's build_vs_buy
    (acceptance.yaml). Deduped by candidate string, tracking which
    module(s)/criteria named it -- 'module:module' for a module-level
    mention, 'module:CRITID' for a criterion-level one."""
    candidates: dict[str, dict] = {}

    def _add(candidate: str, module: str, where: str) -> None:
        candidate = candidate.strip()
        if not candidate:
            return
        entry = candidates.setdefault(candidate, {"candidate": candidate, "sources": []})
        entry["sources"].append(f"{module}:{where}")

    if modules_dir.exists():
        for mod_path in sorted(p for p in modules_dir.iterdir() if p.is_dir()):
            spec = _load_yaml(mod_path / "spec.yaml")
            module_bvb = spec.get("build_vs_buy") or {}
            for cand in module_bvb.get("candidates_considered") or []:
                _add(str(cand), mod_path.name, "module")

            acceptance = _load_yaml(mod_path / "acceptance.yaml")
            for c in acceptance.get("criteria", []) or []:
                if not isinstance(c, dict):
                    continue
                bvb = c.get("build_vs_buy") or {}
                for cand in bvb.get("candidates_considered") or []:
                    _add(str(cand), mod_path.name, c.get("id") or "?")

    return [candidates[k] for k in sorted(candidates)]


def _collect_logic_tier_flags(modules_dir: Path) -> list[dict]:
    """Every criterion whose logic_tier is rung 2 (solver/optimization) or
    3 (statistical/ML) -- flagged as grounding-review topics, deterministic,
    no LLM judgment about which rung a criterion is on (that's decided
    upstream, at kickoff/pm time; this just reads the field)."""
    flags = []
    if modules_dir.exists():
        for mod_path in sorted(p for p in modules_dir.iterdir() if p.is_dir()):
            acceptance = _load_yaml(mod_path / "acceptance.yaml")
            for c in acceptance.get("criteria", []) or []:
                if not isinstance(c, dict):
                    continue
                tier = c.get("logic_tier")
                if tier in (2, 3):
                    flags.append({
                        "module": mod_path.name,
                        "id": c.get("id"),
                        "description": c.get("description"),
                        "logic_tier": tier,
                    })
    return flags


def build_kb_topics(pcp_dir: Path, project_root: Path | None = None) -> dict:
    """Pure aggregation/regex, zero LLM calls. Combines the four
    deterministic inputs into one topic registry (topics.yaml's payload)."""
    project_root = project_root or pcp_dir.parent
    modules_dir = get_modules_dir(pcp_dir)

    objective_scope = [
        {"file": "objective.md", "heading": h}
        for h in _extract_scope_headings(pcp_dir / "objective.md")
    ] + [
        {"file": "target_state.md", "heading": h}
        for h in _extract_scope_headings(pcp_dir / "target_state.md")
    ]
    candidates = _collect_build_vs_buy_candidates(modules_dir)
    dependencies = _parse_dependency_manifests(project_root)
    logic_tier_flags = _collect_logic_tier_flags(modules_dir)

    topics = []
    for entry in objective_scope:
        topics.append({
            "id": f"scope:{_slugify(entry['heading'])}",
            "source": "objective_scope",
            "label": entry["heading"],
            "origin_file": entry["file"],
        })
    for entry in candidates:
        topics.append({
            "id": f"candidate:{_slugify(entry['candidate'])}",
            "source": "build_vs_buy_candidate",
            "label": entry["candidate"],
            "referenced_by": entry["sources"],
        })
    for entry in dependencies:
        topics.append({
            "id": f"dependency:{_slugify(entry['name'])}",
            "source": "dependency_manifest",
            "label": entry["name"],
            "manifest": entry["manifest"],
            "group": entry["group"],
        })
    for entry in logic_tier_flags:
        topics.append({
            "id": f"logic_tier:{entry['module']}/{entry['id']}",
            "source": "logic_tier_rung",
            "label": f"{entry['module']}/{entry['id']}: {entry['description']}",
            "rung": entry["logic_tier"],
        })

    counts = {
        "objective_scope": len(objective_scope),
        "build_vs_buy_candidate": len(candidates),
        "dependency_manifest": len(dependencies),
        "logic_tier_rung": len(logic_tier_flags),
        "total": len(topics),
    }

    return {
        "objective_scope": objective_scope,
        "build_vs_buy_candidates": candidates,
        "dependencies": dependencies,
        "logic_tier_flags": logic_tier_flags,
        "topics": topics,
        "counts": counts,
    }


def write_kb_topics(pcp_dir: Path, project_root: Path | None = None) -> Path:
    data = build_kb_topics(pcp_dir, project_root)
    data["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    kb_dir = pcp_dir / "kb"
    kb_dir.mkdir(parents=True, exist_ok=True)
    out = kb_dir / "topics.yaml"
    out.write_text(yaml.safe_dump(data, sort_keys=False))
    return out


@click.command(name="kb-topics")
@click.option("--path", "project_path", type=click.Path(), default=None)
@click.option("--json", "output_json", is_flag=True, help="Print raw JSON instead of writing kb/topics.yaml.")
def kb_topics(project_path: str | None, output_json: bool):
    """Deterministically derive .pcp/kb/topics.yaml -- zero LLM calls."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    if output_json:
        click.echo(json.dumps(build_kb_topics(pcp_dir), indent=2, default=str))
        return

    out_path = write_kb_topics(pcp_dir)
    console.print(f"[green]KB topics written[/green] -> {out_path.relative_to(pcp_dir.parent)}")
