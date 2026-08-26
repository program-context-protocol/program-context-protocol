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
