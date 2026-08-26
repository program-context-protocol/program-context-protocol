"""kb_grounding.py — Grounding layer for build-time decision making (kb module).

A015: Tier-1 deterministic check. Flags a staged diff that touches a
field/line an existing spec.yaml `constraints` entry protects, with no
accompanying constraint-text update in the same diff. Zero LLM calls, pure
regex + `git diff` — same posture as every other rung-1 check in this repo
(coupling.py, narrative_lint.py's mechanical half, protected_writes.py).

A constraint entry in a module's spec.yaml is free-text prose, but real
constraints routinely embed literal code-shaped tokens inside that prose —
a field name (`card_version`), a filename (`topics.yaml`), a tag
(`tier:cited`). Those tokens are the "field/line" the constraint protects.
If a staged diff changes a line elsewhere in the repo that mentions one of
those tokens, but the owning module's spec.yaml constraint text was not
itself touched in that same staged changeset, the constraint may now be
stale (silently out of sync with the code it was written to describe) —
this check flags that gap for human review. It never blocks by itself; a
caller (e.g. a future `check: constraint_protection` ci_rules.yaml entry)
decides severity, the same separation ast_pattern/protected_path rules keep
from their own runner functions in commands/check.py.
"""

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml

from pcp.pcp_dir import get_modules_dir

# A "protected identifier" is a code-shaped token embedded in constraint
# prose: an underscore-joined name (card_version), a dotted filename
# (topics.yaml), or a colon-joined tag (tier:cited). Requiring an internal
# '_', '.', or ':' is what tells a literal field/file/tag name apart from an
# ordinary English word in the surrounding sentence — plain prose never
# matches. Known limitation: an abbreviation like "e.g." technically fits
# the same shape; harmless in practice since it never recurs verbatim in a
# real source-line diff, so it never produces a false-positive violation.
_IDENTIFIER_RE = re.compile(
    r"`?([A-Za-z_][A-Za-z0-9_]*(?:[._:][A-Za-z_][A-Za-z0-9_]*)+)`?"
)


def extract_protected_identifiers(constraint_text: str) -> set[str]:
    """Code-shaped tokens (contains '_', '.', or ':') inside one constraint
    sentence — the field/file/tag names that sentence protects. Plain
    English words never match."""
    return {m.group(1) for m in _IDENTIFIER_RE.finditer(constraint_text)}


def _module_spec_paths(pcp_dir: Path, project_root: Path) -> dict[str, str]:
    """module_name -> spec.yaml path, relative to project_root."""
    modules_dir = get_modules_dir(pcp_dir)
    paths: dict[str, str] = {}
    if not modules_dir.exists():
        return paths
    for module_dir in sorted(modules_dir.iterdir()):
        if not module_dir.is_dir():
            continue
        spec_path = module_dir / "spec.yaml"
        if not spec_path.exists():
            continue
        try:
            rel = str(spec_path.relative_to(project_root))
        except ValueError:
            rel = str(spec_path)
        paths[module_dir.name] = rel
    return paths


def load_constraint_index(pcp_dir: Path) -> dict[str, list[tuple[str, str]]]:
    """identifier -> [(module_name, constraint_text), ...] across every
    module's spec.yaml `constraints` list. Malformed YAML is skipped, not
    raised — a broken spec.yaml is Layer 1's schema-validation problem, not
    this check's; this check degrades to "nothing to protect" for that
    module rather than crashing the whole gate."""
    modules_dir = get_modules_dir(pcp_dir)
    index: dict[str, list[tuple[str, str]]] = {}
    if not modules_dir.exists():
        return index

    for module_dir in sorted(modules_dir.iterdir()):
        if not module_dir.is_dir():
            continue
        spec_path = module_dir / "spec.yaml"
        if not spec_path.exists():
            continue
        try:
            data = yaml.safe_load(spec_path.read_text(errors="replace")) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(data, dict):
            continue
        for constraint in data.get("constraints") or []:
            if not isinstance(constraint, str):
                continue
            for ident in extract_protected_identifiers(constraint):
                index.setdefault(ident, []).append((module_dir.name, constraint))
    return index


def _changed_lines(project_root: Path, rel_path: str) -> list[str]:
    """Content of lines this staged diff actually added or removed for
    rel_path — not the whole file. `-U0` limits hunks to zero context lines
    so every '+'/'-' line returned is a real change, not surrounding
    context that merely happens to be near one."""
    result = subprocess.run(
        ["git", "diff", "--cached", "-U0", "--", rel_path],
        cwd=project_root, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return []
    lines = []
    for line in result.stdout.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith(("+", "-")):
            lines.append(line[1:])
    return lines


def check_constraint_protection(
    staged_files: list[str], project_root: Path, pcp_dir: Path,
) -> list[str]:
    """Tier-1 deterministic: flags a staged diff that touches a field/line an
    existing spec.yaml constraint entry protects, with no accompanying
    constraint-text update in the same staged changeset.

    For every staged, non-spec.yaml file whose changed lines mention a
    protected identifier, the owning module's own spec.yaml must ALSO be
    staged with a changed line that still mentions that same identifier —
    proof the constraint text was reviewed alongside the code it protects,
    not silently left behind. spec.yaml itself is never the flagged file:
    it is the constraint-text side of the check, not the protected side.

    Returns a list of human-readable violation strings (empty when clean).
    Zero LLM calls: pure regex + `git diff --cached`.
    """
    index = load_constraint_index(pcp_dir)
    if not index:
        return []

    spec_paths = _module_spec_paths(pcp_dir, project_root)
    spec_rel_paths = set(spec_paths.values())
    staged_set = set(staged_files)
    violations: list[str] = []

    for rel_path in staged_files:
        if rel_path in spec_rel_paths:
            continue  # constraint-text side, not the protected side

        changed = _changed_lines(project_root, rel_path)
        if not changed:
            continue

        for ident, entries in index.items():
            if not any(ident in line for line in changed):
                continue

            for module_name, constraint_text in entries:
                spec_rel = spec_paths.get(module_name)
                spec_touched = False
                if spec_rel and spec_rel in staged_set:
                    spec_changed = _changed_lines(project_root, spec_rel)
                    spec_touched = any(ident in line for line in spec_changed)

                if not spec_touched:
                    violations.append(
                        f"{rel_path}: touches `{ident}`, protected by "
                        f"{module_name}/spec.yaml constraint "
                        f'("{constraint_text.strip()}") — no accompanying '
                        f"constraint-text update in this diff"
                    )

    return violations


# ── A016: tier-6 LLM-judged non-mechanical contradiction check ─────────────
#
# check_constraint_protection above is tier-1: a protected identifier is a
# literal token, "did this line change" is a fact a regex can settle. Most
# of what a file-metadata card actually claims (A001's `claims` list, once
# A003 populates it) is not that mechanical -- "this endpoint validates
# input server-side before use", "this cache is invalidated on every write"
# -- whether a diff still honors that claim is a judgment call, not a
# string match. That is a genuine rung-6 problem (CLAUDE.md's ladder: "would
# two competent humans reasonably disagree?" — whether a refactor preserves
# a described guarantee is exactly that shape), not a tier-1 one stretched
# past its fit.
#
# "Genuinely blocking, same posture as CTRL-015" (build.py's
# _run_design_justification_check) means two specific things this check
# mirrors, not just a return-type convention:
#   1. A non-empty result is meant to fail the criterion it's checked
#      against -- unlike this repo's advisory-only single-pass judge checks
#      (narrative_lint.check_narrative_contradictions, which fails open by
#      design and stays advisory even when the judge is confident). The
#      caller here is not asked to merely print a warning.
#   2. Because blocking has real cost, a raw judge finding is not trusted
#      on its own -- an adversarial second pass (_verify_contradiction_
#      findings below), cross-model from the model that raised the finding,
#      has to fail to refute it first. Same judge-decorrelation rationale
#      build.py's _verify_block_findings documents (arXiv:2605.29800,
#      arXiv:2502.01534): a same-model self-check adds little independent
#      signal.
# What this function does NOT do: decide HOW a caller enforces "block" --
# that plumbing (wiring this into architect-review) is the grounding gate's
# own later criterion, per the module's component-5 description. This
# function's contract stops at: never asks a caller to treat a non-empty
# result as merely informational.

CONTRADICTION_SYSTEM_PROMPT = (
    "You are a grounding-contradiction auditor. You are given claims authored in a "
    "project's kb/file_metadata cards about specific source files, alongside a diff "
    "touching those files. Flag a NON-MECHANICAL contradiction only: the diff changes "
    "the file's actual behavior or guarantee in a way that directly contradicts a claim "
    "(e.g. a claim says input is validated server-side before use, and the diff removes "
    "that validation; a claim says a value is append-only, and the diff adds in-place "
    "mutation). Do NOT flag mechanical or cosmetic changes -- renames, formatting, "
    "comments, added tests, or anything that leaves the claim's substance intact. "
    'Respond with JSON only: {"contradictions": [{"index": <int>, "reason": '
    '"<short, cites the claim and the specific diff change>"}]}. Empty list if none found.'
)

CONTRADICTION_VERIFY_SYSTEM_PROMPT = (
    "You are an adversarial verifier for grounding-contradiction findings. You are given "
    "a diff and a list of findings, each claiming the diff contradicts a specific "
    "kb/file_metadata claim. For each finding, decide whether it holds up against the "
    "diff. Mark refuted=true only if the diff does not actually support the claimed "
    "contradiction -- the cited change is not really present in the diff, or it is a "
    "mechanical/cosmetic change with no real behavioral contradiction. Respond with JSON "
    'only: {"verdicts": [{"index": 0, "refuted": false, "reason": "..."}, ...]} -- '
    "exactly one entry per finding above, in order."
)


def load_kb_claims_for_files(
    project_root: Path, pcp_dir: Path, files: list[str],
) -> list[tuple[str, str]]:
    """(source_path, claim_text) pairs for every claim authored in the
    file-metadata card of a file in `files` (paths relative to
    project_root). A file with no card yet, or whose card's `claims` list
    is still empty -- authoring real claims is A003's separate criterion;
    A001 only guarantees every source file HAS a card -- contributes
    nothing. That is expected, not an error: this check has nothing to
    compare a diff against until a claim actually exists for that file.
    Malformed card YAML is skipped the same way load_constraint_index skips
    a malformed spec.yaml -- Layer 1's problem, not this check's.

    A claim entry may be a plain string, or a dict with a `text` or `claim`
    key -- A003 has not landed yet to fix the exact shape, so this stays
    permissive rather than assuming a schema that does not exist yet.
    """
    from pcp.kb_file_metadata import card_path_for

    project_root = Path(project_root)
    claims: list[tuple[str, str]] = []
    for rel in files:
        source_file = project_root / rel
        try:
            card_path = card_path_for(pcp_dir, project_root, source_file)
        except ValueError:
            continue  # source_file resolves outside project_root
        if not card_path.exists():
            continue
        try:
            data = yaml.safe_load(card_path.read_text(errors="replace")) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(data, dict):
            continue
        for claim in data.get("claims") or []:
            if isinstance(claim, str):
                text = claim
            elif isinstance(claim, dict):
                text = claim.get("text") or claim.get("claim") or ""
            else:
                text = ""
            if text:
                claims.append((rel, text))
    return claims


def _staged_diff_text(project_root: Path, staged_files: list[str]) -> str:
    """Full unified diff (default context, not check_constraint_protection's
    -U0) for the given staged files -- the judge needs surrounding context
    to tell a real behavioral change from a cosmetic one; a bare +/- line
    alone is exactly what tier-1's mechanical scan already handles."""
    if not staged_files:
        return ""
    result = subprocess.run(
        ["git", "diff", "--cached", "--", *staged_files],
        cwd=project_root, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout


def _verify_contradiction_findings(
    pcp_dir: Path, diff: str, findings: list[str],
) -> list[str]:
    """Adversarial second pass over check_kb_contradiction's own raw
    findings, before they are trusted enough to block a criterion --
    the CTRL-015 posture this module mirrors (see the section docstring
    above). Cross-model: the verifier runs on BUILD_MODEL, a different
    model from the JUDGE_MODEL call that raised the finding, for the same
    decorrelation reason build.py's _verify_block_findings documents.

    Fails OPEN on any verifier error (timeout, bad JSON, call failure):
    keeps every finding unverified rather than risk silently dropping a
    real contradiction because the verifier itself broke. A hallucinated
    finding that slips through costs one blocked criterion a human can
    review; a real one silently dropped ships an actual defect -- the same
    asymmetry build.py's own verifier documents.
    """
    if not findings:
        return []

    from pcp.llm import client as llm

    numbered = "\n".join(f"[{i}] {f}" for i, f in enumerate(findings))
    prompt = f"## Diff\n{diff[:14000]}\n\n## Findings to verify\n{numbered}"
    try:
        res = llm.call_json(
            CONTRADICTION_VERIFY_SYSTEM_PROMPT, prompt, model=llm.BUILD_MODEL,
            pcp_dir=pcp_dir, command="kb-grounding-contradiction-verify",
        )
    except Exception:
        return findings  # fail open -- see docstring

    verdicts = {v.get("index"): v for v in res.get("verdicts", []) if isinstance(v, dict)}
    kept = []
    for i, f in enumerate(findings):
        v = verdicts.get(i)
        if v and v.get("refuted"):
            continue
        kept.append(f)
    return kept


def check_kb_contradiction(
    staged_files: list[str], project_root: Path, pcp_dir: Path,
) -> list[str]:
    """Tier-6 LLM-judged: compares a staged diff against loaded
    kb/file_metadata content (load_kb_claims_for_files) for non-mechanical
    contradictions. Returns a genuinely-blocking verdict, same posture as
    CTRL-015 -- see the section docstring above for what that means
    concretely. Empty list = clean (nothing to compare, or the judge and
    its adversarial verifier both found nothing that survives scrutiny).

    Zero LLM calls when there is nothing to compare against (no claims
    touched, or no actual diff) -- Token Discipline: never spend a judge
    call on a criterion this check has no evidence for either way.
    """
    claims = load_kb_claims_for_files(project_root, pcp_dir, staged_files)
    if not claims:
        return []

    diff = _staged_diff_text(project_root, staged_files)
    if not diff.strip():
        return []

    from pcp.llm import client as llm

    numbered = "\n".join(f"[{i}] {path}: {text}" for i, (path, text) in enumerate(claims))
    user_prompt = f"## Diff\n{diff[:14000]}\n\n## Claims from kb/file_metadata\n{numbered}"
    try:
        res = llm.call_json(
            CONTRADICTION_SYSTEM_PROMPT, user_prompt, model=llm.JUDGE_MODEL,
            pcp_dir=pcp_dir, command="kb-grounding-contradiction",
        )
    except Exception:
        # Fails open only on an infra-shaped error (call/parse failure) --
        # an unreachable judge is not evidence of a real contradiction.
        # This is the one place this check is forgiving; a returned verdict
        # never is (see the section docstring).
        return []

    findings: list[str] = []
    for c in res.get("contradictions", []):
        if not isinstance(c, dict):
            continue
        i = c.get("index")
        if isinstance(i, int) and 0 <= i < len(claims):
            path, text = claims[i]
            findings.append(
                f"{path}: diff contradicts kb claim {text[:160]!r} — {c.get('reason', '')[:200]}"
            )

    if not findings:
        return []

    return _verify_contradiction_findings(pcp_dir, diff, findings)


# ── A013: grounding-gate context-package builder ────────────────────────────
#
# The grounding gate (module spec.yaml, component 5) has two halves: this
# is the FIRST -- a disclosed context package assembled pre-build, so an
# agent starting work on a target file sees what grounding evidence already
# exists before it writes anything. check_kb_contradiction/A016 above is
# the second half -- a genuinely-blocking AFTER-the-fact check run at
# architect-review, once a diff exists to compare against loaded claims.
# The two are complementary, not redundant: this builder never blocks
# anything by itself, it discloses; A016 blocks.
#
# "Build-Plan-style" mirrors commands/build_plan.py's own posture exactly
# (see that module's docstring): pure aggregation over data other kb
# components already compute, zero LLM calls, spawns nothing, one
# JSON-serializable dict. Nothing here recomputes a card, a catalog, an
# index, a gap list, or a dependency graph -- every section below is a thin
# read over an existing kb component's own output/input.
#
# Four sections, each assembled and disclosed INDEPENDENTLY -- a target
# file missing a card, missing topic citations, missing known-issues hits,
# or unattributable to any module for blast-radius purposes is real,
# actionable grounding information (exactly what a caller needs to decide
# whether it's safe to proceed on thin evidence), never silently folded
# into a package that merely reports "built" with nothing to show:
#   1. file_metadata   -- kb_file_metadata.py's per-file card (A001-A003)
#   2. topic_citations -- kb_index.py's basename cross-reference resolved
#                          against kb_catalog.py's heading-TOC (A006/A007),
#                          i.e. which kb topic docs mention this file, and
#                          under which headings
#   3. known_issues     -- kb/domain + kb/adr prose that mentions this file
#                          by basename -- the human-curated failure-mode/
#                          gotcha layer CLAUDE.md documents for kb/domain/
#                          *.md, reusing kb_ingest.py's own definition of
#                          "kb content" (the same two directories its gap
#                          detector treats as real prose, A008)
#   4. blast_radius      -- which OTHER modules would be affected if this
#                          file's owning module changes. Reuses impact.py's
#                          own module-attribution + dependency-graph
#                          traversal (built for QA test-selection, same
#                          graph coupling.py's coupling_score already
#                          trusts) rather than a second implementation,
#                          optionally enriched with graphify's community
#                          detection (coupling.py's own compute_communities
#                          -- graphify is already an adopted dependency in
#                          this repo, see that module's own docstring;
#                          degrades to {"available": False} if graphify
#                          isn't installed, never an error)


def build_file_metadata_section(pcp_dir: Path, project_root: Path, target_file: str) -> dict:
    """{"found": bool, "card": dict | None} -- the target file's
    kb_file_metadata card (A001-A003), read straight off disk. A missing
    card, a target_file outside project_root, or malformed card YAML all
    disclose as found=False rather than raising -- Layer 1's schema-
    validation problem, not this builder's, same posture load_constraint_
    index/load_kb_claims_for_files already take on a broken spec/card."""
    from pcp.kb_file_metadata import card_path_for

    project_root = Path(project_root)
    source_file = project_root / target_file
    try:
        card_path = card_path_for(pcp_dir, project_root, source_file)
    except ValueError:
        return {"found": False, "card": None}
    if not card_path.exists():
        return {"found": False, "card": None}
    try:
        data = yaml.safe_load(card_path.read_text(errors="replace")) or {}
    except yaml.YAMLError:
        return {"found": False, "card": None}
    if not isinstance(data, dict):
        return {"found": False, "card": None}
    return {"found": True, "card": data}


def build_topic_citations_section(pcp_dir: Path, project_root: Path, target_file: str) -> dict:
    """{"found": bool, "citations": [{"doc": str, "headings": [...]}, ...]}
    -- every kb topic doc that mentions target_file's basename as a
    filename-shaped token (kb_index.py's own cross-reference, A007),
    resolved into that doc's real heading TOC (kb_catalog.py's
    extract_headings, A006) so a citation points at a section, not just a
    filename. Reads live rather than requiring `pcp kb-catalog`/
    `pcp kb-index` to have been run first -- both underlying scans are
    already cheap, deterministic, single-basename-scoped reads here."""
    from pcp.kb_catalog import extract_headings
    from pcp.kb_index import collect_kb_references

    project_root = Path(project_root)
    basename = Path(target_file).name
    references = collect_kb_references(pcp_dir, {basename})
    doc_rels = references.get(basename, [])

    citations = []
    for doc_rel in doc_rels:
        doc_path = project_root / doc_rel
        citations.append({"doc": doc_rel, "headings": extract_headings(doc_path)})

    return {"found": bool(citations), "citations": citations}


def build_known_issues_section(pcp_dir: Path, target_file: str) -> dict:
    """{"found": bool, "hits": [{"doc": str, "matches": [str, ...]}, ...]}
    -- lines in kb/domain + kb/adr prose (kb_ingest.py's own definition of
    real kb content, A008's _kb_content_files) that mention target_file's
    basename. "Known issues" here means the human-curated failure-mode/
    invariant/gotcha layer CLAUDE.md documents for kb/domain/*.md -- a
    verbatim substring match against that prose, same posture as
    kb_ingest.detect_content_gaps's own matching (deterministic, no
    summarisation, no relevance judgment)."""
    from pcp.kb_ingest import _kb_content_files

    needle = Path(target_file).name.lower()
    hits = []
    for path in _kb_content_files(pcp_dir):
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        matches = [
            line.strip() for line in text.splitlines()
            if needle in line.lower() and line.strip()
        ]
        if matches:
            hits.append({"doc": path.name, "matches": matches})

    return {"found": bool(hits), "hits": hits}


def build_blast_radius_section(pcp_dir: Path, project_root: Path, target_file: str) -> dict:
    """{"found": bool, "owning_modules": [...], "affected_modules": [...],
    "graphify_communities": {...}} -- which module(s) own target_file
    (impact.py's changed_files_to_modules -- declared `target` +
    path-convention attribution) and every module that transitively
    depends on them (impact.py's blast_radius_modules, nx.ancestors on
    coupling.py's own dependency graph -- one graph, not a second one).
    "found": False (with empty module lists) when target_file can't be
    attributed to any declared module -- degrade honestly, never guess.
    graphify_communities enriches with coupling.py's compute_communities
    (advisory, {"available": False} if graphify isn't installed)."""
    from pcp.coupling import build_dependency_graph, compute_communities
    from pcp.impact import (
        _load_modules_for_impact,
        blast_radius_modules,
        changed_files_to_modules,
    )

    pcp_dir = Path(pcp_dir)
    modules = _load_modules_for_impact(pcp_dir)
    empty = {
        "found": False, "owning_modules": [], "affected_modules": [],
        "graphify_communities": {"available": False},
    }
    if not modules:
        return empty

    owning = changed_files_to_modules(pcp_dir, modules, [target_file])
    if not owning:
        return empty

    radius = blast_radius_modules(modules, owning)
    affected = sorted(radius - owning)

    G = build_dependency_graph(modules)
    communities = compute_communities(G)

    return {
        "found": True,
        "owning_modules": sorted(owning),
        "affected_modules": affected,
        "graphify_communities": communities,
    }


def build_context_package(
    project_root: Path, pcp_dir: Path, target_files: list[str],
) -> dict:
    """The grounding-gate context package (module spec.yaml component 5,
    first half -- see the section docstring above): {"target_files": [...],
    "files": {target_file: {"file_metadata": ..., "topic_citations": ...,
    "known_issues": ..., "blast_radius": ...}}, "generated_at": "..."}.

    Build-Plan-style: pure aggregation, zero LLM calls, spawns nothing,
    JSON-serializable. Every one of the four sections is assembled and
    disclosed independently per target file -- see the module-level
    section docstring above for why that independence matters."""
    project_root = Path(project_root)
    pcp_dir = Path(pcp_dir)

    files: dict[str, dict] = {}
    for target_file in target_files:
        files[target_file] = {
            "file_metadata": build_file_metadata_section(pcp_dir, project_root, target_file),
            "topic_citations": build_topic_citations_section(pcp_dir, project_root, target_file),
            "known_issues": build_known_issues_section(pcp_dir, target_file),
            "blast_radius": build_blast_radius_section(pcp_dir, project_root, target_file),
        }

    return {
        "target_files": list(target_files),
        "files": files,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
