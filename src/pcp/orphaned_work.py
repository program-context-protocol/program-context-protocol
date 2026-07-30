"""Criteria whose work landed in the repo but whose status still says `pending`.

Three independent occurrences on ontology-foundry inside a week, via two
different code paths:

- **The wave-gate reopen path.** `core-data-model` A022/A030/A033/A038 — $30.04
  spent, all four branches merged into `main`, all four set back to `pending`
  because a wave gate blocked on findings about a module outside the wave. Fixed
  2026-07-30 (`_finding_blames_outside_wave`).
- **The run-stopped path, still unfixed.** `query-eval-harness`, 2026-07-30 —
  $31.65 spent, A001 and A008 merged into `main` with source and tests present
  (`32be3fa`, `bbb134c`), MOD_A002 committed (`4276c70`), and **all 18 criteria
  reading `pending`**. No wave-block record exists; the run simply stopped after
  its last commit and nothing wrote status back.

Whatever the cause, the consequence is the same and it is the worst kind PCP can
have: the project's own record of what is built becomes false in the direction
that makes people rebuild finished work. `pcp scan` regenerates `current_state.md`
from acceptance status, so a wrong status propagates into every downstream view —
the dashboard, `diff.md`, `validate-strategy`'s coverage, the next build's pending
list.

This module does not fix status. It **reports the contradiction**, deterministically
(rung 1, no LLM), because `acceptance.yaml` is human-approved and PCP must not
silently mark work complete on its own — that would trade a false "not built" for
a false "built", which is strictly worse.

The signal is a landed COMMIT written by PCP's own conventions -- `Merge
feat/<module>-<criterion_id>` from the merge path, or `<module>/<criterion_id>:`
from the auto-commit path. Branch-merged state is deliberately NOT used: worktree
reuse resets branches to the current base, so a branch that never carried a commit
still reports as merged. See `_landed_commit_subjects`. No heuristics about file
contents, no guessing.
"""

import subprocess
from pathlib import Path

import yaml as _yaml

from pcp.schema.validator import load_yaml


def dump_yaml(data: dict) -> str:
    return _yaml.dump(data, default_flow_style=False, sort_keys=False)


def _landed_commit_subjects(project_root: Path) -> list[str]:
    """Commit subjects reachable from HEAD. Empty on any git failure.

    `git branch --merged` is NOT a usable signal here and using it was the first
    version of this module. `pcp build` reuses worktrees and resets their branch
    to the current base (`_sync_worktree_to_base`), so a branch that never carried
    a single commit still reports as "merged" — it is an ancestor of HEAD by
    construction. Checked against ontology-foundry, that produced 18 findings of
    which most were false: `feat/query-eval-harness-MOD_A001` and
    `feat/core-data-model-A041` have no merge commit and never advanced.

    The trustworthy signal is PCP's own commit conventions, which only exist when
    work actually landed:
      - `_merge_module_branch` commits `Merge feat/<module>-<criterion_id>`
      - `_auto_commit_criterion` commits `<module>/<criterion_id>: <description>`
    """
    try:
        proc = subprocess.run(
            ["git", "log", "--format=%s", "HEAD"],
            cwd=project_root, capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    return proc.stdout.splitlines()


def find_orphaned_work(pcp_dir: Path, project_root: Path | None = None) -> list[dict]:
    """Pending criteria that have a landed commit written for them.

    Deliberately conservative: requires a commit subject matching PCP's own
    conventions exactly. A criterion whose work never produced such a commit is
    treated as genuinely unfinished and is not reported — the first version used
    branch-merged state instead and was wrong on 6 of 18 findings.
    """
    project_root = project_root or Path(pcp_dir).parent
    subjects = _landed_commit_subjects(project_root)
    if not subjects:
        return []

    found = []
    modules_dir = Path(pcp_dir) / "strategy" / "modules"
    if not modules_dir.exists():
        return []
    for acc_path in sorted(modules_dir.glob("*/acceptance.yaml")):
        module = acc_path.parent.name
        try:
            data = load_yaml(acc_path) or {}
        except Exception:
            continue
        for c in data.get("criteria", []) or []:
            if not isinstance(c, dict):
                continue
            status = c.get("status", "pending")
            if status == "complete":
                continue
            cid = c.get("id")
            branch = f"feat/{module}-{cid}"
            merge_subject = f"Merge {branch}"
            work_prefix = f"{module}/{cid}:"
            evidence = next(
                (s for s in subjects
                 if s.strip() == merge_subject or s.startswith(work_prefix)),
                None,
            )
            if evidence:
                found.append({
                    "module": module,
                    "criterion_id": cid,
                    "branch": branch,
                    "status": status,
                    "evidence": evidence[:120],
                    "description": (c.get("description") or "")[:120],
                })
    return found


def _unverified_now(modules_dir: Path) -> list[dict]:
    found = []
    for acc_path in sorted(modules_dir.glob("*/acceptance.yaml")):
        module = acc_path.parent.name
        try:
            data = load_yaml(acc_path) or {}
        except Exception:
            continue
        for c in data.get("criteria", []) or []:
            if not isinstance(c, dict):
                continue
            if c.get("status") == "complete" and not c.get("verified_by"):
                found.append({
                    "module": module,
                    "criterion_id": c.get("id"),
                    "description": (c.get("description") or "")[:120],
                })
    return found


def _baseline_path(pcp_dir: Path) -> Path:
    return Path(pcp_dir) / "unverified_complete_baseline.yaml"


def find_unverified_complete(pcp_dir: Path) -> list[dict]:
    """Criteria marked `complete` with no `verified_by` -- the mirror image of
    `find_orphaned_work` -- EXCLUDING a one-time grandfathered baseline.

    `verified_by` is stamped ONLY by `_mark_criterion_complete` inside `pcp
    build`'s real gated loop (or now `pcp verify`). Its absence on a `complete`
    criterion means the status was hand-edited into acceptance.yaml directly --
    no gate ever ran, no evidence exists, and PCP is reporting work as done that
    it has no basis for.

    First found live on ontology-foundry 2026-07-30: 12 criteria hand-flipped
    after the orphaned-work fix landed, because no sanctioned "mark done" path
    existed yet -- fixed by `pcp verify` in this same change.

    **The baseline exists because the naive version was shipped and run against
    the real project before the tests caught the problem it caused.** `verified_by`
    is 6 days old (added 2026-07-24); ontology-foundry has 432 criteria going back
    to early July. Un-baselined, this returns **282 of 333 complete criteria** --
    85% of all completed work, flagged as suspect for the sole reason that it
    predates a field that did not exist when it was built. That is not a signal,
    it is the field's own introduction date read back as an accusation. It is
    also exactly the shape already named once this session: a check with no
    sanctioned way to comply produces noise users learn to ignore (CTRL-018's 110
    phantom blocks, 42.5% of that project's recorded blocks).

    The baseline is a one-time snapshot, written to
    `.pcp/unverified_complete_baseline.yaml` on the first call ever made in a
    project (empty findings that call, honestly -- there is no "new" yet to
    report). Every criterion in it is accepted debt from before this check
    existed, and stays grandfathered PERMANENTLY by (module, criterion_id)
    identity -- not by status. A baselined criterion never reappears just from
    cycling pending -> complete again, deliberately: acceptance.yaml is already
    gated behind `pcp pm`/`pcp verify`/human approval, so resurrecting it on a
    status bounce would add a new failure mode (an innocent status edit making
    old debt look like a new violation) without closing any real hole. The way
    out of the baseline is `pcp verify`, which stamps `verified_by` and removes
    the criterion from the unverified set entirely -- not silence, an actual fix.
    Anything unverified-complete that was never in the baseline is new since
    this check started watching, and is reported every time.

    This is the more dangerous direction of the two orphaned-work checks. A false
    `pending` costs a wasted rebuild. A false `complete` means the work is never
    checked again -- silently absent from every future gate, audit, and coverage
    figure. Advisory only, never blocking, for the same reason CTRL-018 is warn
    by default: refusing to build over inherited debt would be the wrong failure
    mode."""
    modules_dir = Path(pcp_dir) / "strategy" / "modules"
    if not modules_dir.exists():
        return []
    current = _unverified_now(modules_dir)

    baseline_path = _baseline_path(pcp_dir)
    if not baseline_path.exists():
        keys = sorted(f"{f['module']}/{f['criterion_id']}" for f in current)
        try:
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            baseline_path.write_text(dump_yaml({
                "note": ("One-time grandfather snapshot, written automatically by the first "
                         "`pcp scan`/`pcp build` after this check shipped. Everything listed here "
                         "predates verified_by tracking and is accepted debt, not a new problem -- "
                         "see find_unverified_complete's docstring. Never hand-edit; a criterion "
                         "drops out of this list on its own once it goes through `pcp verify` or "
                         "`pcp build`."),
                "criteria": keys,
            }))
        except OSError:
            pass  # best-effort -- an unwritable .pcp/ must not break scan/build
        return []

    try:
        baseline = set((load_yaml(baseline_path) or {}).get("criteria") or [])
    except Exception:
        baseline = set()
    return [f for f in current if f"{f['module']}/{f['criterion_id']}" not in baseline]


def format_unverified_findings(found: list[dict]) -> list[str]:
    """Human-readable lines for find_unverified_complete. Empty when nothing to say."""
    if not found:
        return []
    lines = [
        f"{len(found)} criterion(s) marked 'complete' with no verified_by — no gate ever "
        f"confirmed this, the status was set by hand:"
    ]
    for f in found[:12]:
        lines.append(f"   {f['module']}/{f['criterion_id']}  <- fix: pcp verify {f['module']} {f['criterion_id']}")
    if len(found) > 12:
        lines.append(f"   ... and {len(found) - 12} more")
    lines.append(
        "If genuinely done, record it properly with the command above. Leaving it as-is means "
        "no gate, audit, or coverage figure will ever check this again."
    )
    return lines


def format_findings(found: list[dict]) -> list[str]:
    """Human-readable lines. Empty list when there is nothing to say."""
    if not found:
        return []
    lines = [
        f"{len(found)} criterion(s) marked '{found[0]['status']}' or pending whose work is "
        f"already merged into this branch — the status is stale, not the code:"
    ]
    for f in found[:12]:
        lines.append(f"   {f['module']}/{f['criterion_id']}  <- commit: {f['evidence']}")
    if len(found) > 12:
        lines.append(f"   ... and {len(found) - 12} more")
    lines.append(
        "Verify, then mark them complete via `pcp pm` — acceptance.yaml is human-approved, "
        "so PCP will not flip status on its own. Leaving it stale makes the next build "
        "redo finished work."
    )
    return lines
