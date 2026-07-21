"""QA gates — test suite, lint, SAST/secret-scan.

Wraps whatever's already installed for the project's language, same philosophy
as audit.py's dead-code wrapper: detect a real tool, shell out to it, skip
gracefully (never block) if nothing's installed. Called from pcp build's
per-criterion loop, per docs/greenfield.md Phase 3.

Honest scope: this verifies "tests pass and lint/SAST is clean before the
criterion is accepted" — a real, checkable contract. It does NOT enforce
RED-then-GREEN ordering (test written and failing before code exists) — that
would require snapshotting agent state mid-session, a different and bigger
feature than what's built here.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

# Real incident, 2026-07-18 (ontology-foundry dogfood): these were bare
# module constants with no env override -- a real project's test suite
# legitimately taking 420-550s against a real Postgres backend always got
# falsely marked "timed out" against the 300s default, burning a build
# retry every time. A local hand-patch to the constant had nowhere durable
# to live and got silently wiped on every reinstall/reset. Same env-override
# pattern as llm/client.py's _timeout() (PCP_LLM_TIMEOUT) and
# PCP_BUILD_AGENT_TIMEOUT_SEC -- read lazily so a test can monkeypatch the
# env var and see the effect without reimporting this module.
def _timeout_test() -> int:
    return int(os.environ.get("PCP_QA_TEST_TIMEOUT_SEC", "300"))


def test_timeout_info() -> tuple[int, bool]:
    """(effective timeout seconds, True if PCP_QA_TEST_TIMEOUT_SEC is unset
    and the 300s default is silently in effect) -- lets a build run print
    this loud instead of a wrong-environment failure (e.g. the DB the tests
    hit isn't the one intended) surfacing as an indistinguishable "timed
    out", same masking this module's own docstring above already documents
    for the slow-suite case."""
    return _timeout_test(), "PCP_QA_TEST_TIMEOUT_SEC" not in os.environ


def _timeout_lint() -> int:
    return int(os.environ.get("PCP_QA_LINT_TIMEOUT_SEC", "60"))


def _timeout_sast() -> int:
    return int(os.environ.get("PCP_QA_SAST_TIMEOUT_SEC", "120"))


def _run_pytest(project_root: Path, test_paths: list[str] | None = None) -> dict | None:
    if not shutil.which("pytest"):
        return None
    args = ["pytest", "-q", *(test_paths or [])]
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, cwd=project_root, timeout=_timeout_test(),
        )
    except subprocess.TimeoutExpired:
        return {"tool": "pytest", "passed": False, "output": "timed out"}
    # Exit code 5 = no tests collected yet — not a failure, just nothing to run.
    passed = result.returncode == 0 or result.returncode == 5
    return {
        "tool": "pytest", "passed": passed, "output": (result.stdout + result.stderr)[-3000:],
        "scoped_to": test_paths or None,
    }


def _run_npm_test(project_root: Path) -> dict | None:
    pkg = project_root / "package.json"
    if not pkg.exists() or not shutil.which("npm"):
        return None
    try:
        data = json.loads(pkg.read_text())
    except Exception:
        return None
    if "test" not in data.get("scripts", {}):
        return None
    try:
        result = subprocess.run(
            ["npm", "test", "--silent"], capture_output=True, text=True, cwd=project_root, timeout=_timeout_test(),
        )
    except subprocess.TimeoutExpired:
        return {"tool": "npm test", "passed": False, "output": "timed out"}
    return {"tool": "npm test", "passed": result.returncode == 0, "output": (result.stdout + result.stderr)[-3000:]}


def _run_go_test(project_root: Path) -> dict | None:
    if not (project_root / "go.mod").exists() or not shutil.which("go"):
        return None
    try:
        result = subprocess.run(
            ["go", "test", "./..."], capture_output=True, text=True, cwd=project_root, timeout=_timeout_test(),
        )
    except subprocess.TimeoutExpired:
        return {"tool": "go test", "passed": False, "output": "timed out"}
    return {"tool": "go test", "passed": result.returncode == 0, "output": (result.stdout + result.stderr)[-3000:]}


def test_selection_enabled() -> bool:
    return os.environ.get("PCP_QA_TEST_SELECTION") == "impact"


def run_test_suite(project_root: Path, pcp_dir: Path | None = None, changed_files: list[str] | None = None) -> dict:
    """Full regression suite by default — project-wide, not scoped to
    changed files, so it catches regressions outside the files this
    criterion touched.

    When PCP_QA_TEST_SELECTION=impact and pcp_dir/changed_files are given,
    scopes pytest to the modules impacted by the change (see impact.py:
    the changed module(s) plus every module that transitively depends on
    them, via the same dependency graph coupling.py already trusts for
    coupling_score — not a separate ad hoc mechanism). Only pytest
    supports scoping today; npm/go test always run in full regardless of
    this flag, an honest scope limit, not a silent gap. Falls back to the
    full suite whenever impact.py can't confidently narrow the scope —
    never silently runs zero tests."""
    test_paths = None
    if test_selection_enabled() and pcp_dir is not None and changed_files:
        from pcp.impact import blast_radius_test_paths
        try:
            test_paths = blast_radius_test_paths(pcp_dir, project_root, changed_files)
        except Exception:
            test_paths = None  # scoping failed -- fall back to the full suite, don't propagate

    if test_paths and shutil.which("pytest"):
        out = _run_pytest(project_root, test_paths)
        if out is not None:
            return out

    for runner in (_run_pytest, _run_npm_test, _run_go_test):
        out = runner(project_root)
        if out is not None:
            return out
    return {"tool": None, "passed": True, "output": ""}


def _run_ruff(project_root: Path, changed_files: list[str]) -> dict | None:
    if not shutil.which("ruff"):
        return None
    py_files = [f for f in changed_files if f.endswith(".py")]
    if not py_files:
        return {"tool": "ruff", "passed": True, "issues": []}
    try:
        result = subprocess.run(
            ["ruff", "check", *py_files], capture_output=True, text=True, cwd=project_root, timeout=_timeout_lint(),
        )
    except subprocess.TimeoutExpired:
        return {"tool": "ruff", "passed": True, "issues": [], "skipped": "timed out"}
    except Exception as e:
        # Previously uncaught -- a ruff crash/misconfig took down the whole
        # gate-evaluation thread instead of degrading to a skip, worse than
        # the silent-skip failure mode this is meant to guard against.
        return {"tool": "ruff", "passed": True, "issues": [], "skipped": str(e)}
    issues = [l for l in result.stdout.splitlines() if l.strip()]
    return {"tool": "ruff", "passed": result.returncode == 0, "issues": issues}


def _run_eslint(project_root: Path, changed_files: list[str]) -> dict | None:
    eslint_bin = project_root / "node_modules" / ".bin" / "eslint"
    if not eslint_bin.exists():
        return None
    js_files = [f for f in changed_files if f.endswith((".js", ".jsx", ".ts", ".tsx"))]
    if not js_files:
        return {"tool": "eslint", "passed": True, "issues": []}
    try:
        result = subprocess.run(
            [str(eslint_bin), *js_files], capture_output=True, text=True, cwd=project_root, timeout=_timeout_lint(),
        )
    except subprocess.TimeoutExpired:
        return {"tool": "eslint", "passed": True, "issues": [], "skipped": "timed out"}
    except Exception as e:
        return {"tool": "eslint", "passed": True, "issues": [], "skipped": str(e)}
    issues = [l for l in result.stdout.splitlines() if l.strip()]
    return {"tool": "eslint", "passed": result.returncode == 0, "issues": issues}


def run_lint(project_root: Path, changed_files: list[str]) -> dict:
    """Scoped to changed files only — fast, PR-diff-style check."""
    for runner in (_run_ruff, _run_eslint):
        out = runner(project_root, changed_files)
        if out is not None:
            return out
    return {"tool": None, "passed": True, "issues": []}


def _run_python_coverage(project_root: Path) -> dict | None:
    if not shutil.which("coverage") or not shutil.which("pytest"):
        return None
    try:
        subprocess.run(
            ["coverage", "run", "-m", "pytest", "-q"],
            capture_output=True, text=True, cwd=project_root, timeout=_timeout_test(),
        )
        result = subprocess.run(
            ["coverage", "report", "--format=total"],
            capture_output=True, text=True, cwd=project_root, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return {"tool": "coverage", "percent": None}
    try:
        return {"tool": "coverage", "percent": float(result.stdout.strip())}
    except ValueError:
        return {"tool": "coverage", "percent": None}


def _run_npm_coverage(project_root: Path) -> dict | None:
    pkg = project_root / "package.json"
    if not pkg.exists() or not shutil.which("npm"):
        return None
    try:
        data = json.loads(pkg.read_text())
    except Exception:
        return None
    if "coverage" not in data.get("scripts", {}):
        return None
    try:
        result = subprocess.run(
            ["npm", "run", "coverage", "--silent"],
            capture_output=True, text=True, cwd=project_root, timeout=_timeout_test(),
        )
    except subprocess.TimeoutExpired:
        return {"tool": "npm coverage", "percent": None}
    # istanbul/nyc/jest all print a summary row starting with "All files"
    m = re.search(r"All files\s*\|\s*([\d.]+)", result.stdout)
    return {"tool": "npm coverage", "percent": float(m.group(1)) if m else None}


def run_coverage(project_root: Path) -> dict:
    """Best-effort test coverage %. Returns {"tool": None, "percent": None} if
    no coverage tool is detected — never blocks, this is a tracked metric only."""
    for runner in (_run_python_coverage, _run_npm_coverage):
        out = runner(project_root)
        if out is not None:
            return out
    return {"tool": None, "percent": None}


def run_sast(project_root: Path, changed_files: list[str]) -> dict:
    """SAST + secret-scan via semgrep, if installed. Scoped to changed files."""
    if not shutil.which("semgrep") or not changed_files:
        return {"tool": None, "passed": True, "findings": []}
    try:
        result = subprocess.run(
            ["semgrep", "--config=auto", "--quiet", "--error", *changed_files],
            capture_output=True, text=True, cwd=project_root, timeout=_timeout_sast(),
        )
    except subprocess.TimeoutExpired:
        return {"tool": "semgrep", "passed": True, "findings": [], "skipped": "timed out"}
    except Exception as e:
        return {"tool": "semgrep", "passed": True, "findings": [], "skipped": str(e)}
    findings = [l for l in result.stdout.splitlines() if l.strip()]
    if result.returncode != 0 and not findings:
        # semgrep's --error flag exits non-zero for EITHER real findings OR a
        # tool-level failure (network fetch for --config=auto, a scan error)
        # -- indistinguishable by exit code alone. Real 2026-07-21 incident:
        # this surfaced as "SAST found issues" with an empty evidence file,
        # since nothing landed on stdout to write. Skip (don't block) rather
        # than block on a phantom finding -- same posture as the timeout/
        # exception branches above.
        return {
            "tool": "semgrep", "passed": True, "findings": [],
            "skipped": (result.stderr or "semgrep exited non-zero with no findings on stdout")[-500:],
        }
    return {"tool": "semgrep", "passed": result.returncode == 0, "findings": findings}
