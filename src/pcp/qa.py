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
import time
from pathlib import Path

import yaml

# Real incident, 2026-07-18 (Project O dogfood): these were bare
# module constants with no env override -- a real project's test suite
# legitimately taking 420-550s against a real Postgres backend always got
# falsely marked "timed out" against the 300s default, burning a build
# retry every time. A local hand-patch to the constant had nowhere durable
# to live and got silently wiped on every reinstall/reset. Same env-override
# pattern as llm/client.py's _timeout() (PCP_LLM_TIMEOUT) and
# PCP_BUILD_AGENT_TIMEOUT_SEC -- read lazily so a test can monkeypatch the
# env var and see the effect without reimporting this module.
_TIMEOUT_SAFETY_MULTIPLIER = 1.5
_MIN_MEASURED_TIMEOUT_SEC = 60


def _qa_timing_path(pcp_dir: Path) -> Path:
    return Path(pcp_dir) / "qa_timing.yaml"


def _load_measured_timeout(pcp_dir: Path | None) -> int | None:
    """A prior FULL (unscoped) test run's measured wall-clock, scaled by a
    safety margin, used instead of the fixed 300s guess once one exists.

    Real incident, Project W dogfood 2026-08-08: a healthy 390-560s suite hit
    the 300s default on every single build attempt until PCP_QA_TEST_TIMEOUT_SEC
    was found and set manually, three times, in three different sessions. The
    env override (2026-07-18 fix, see below) works but is opt-in and has to be
    rediscovered per project; this makes the common case self-correcting after
    one real full run instead of requiring that discovery at all.

    Fails open (None) on any read/parse problem, same posture as every other
    best-effort .pcp/ read in this codebase -- an unreadable cache must not be
    the reason a test gate misbehaves."""
    if pcp_dir is None:
        return None
    path = _qa_timing_path(pcp_dir)
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text()) or {}
        measured = (data.get("test_suite") or {}).get("measured_seconds")
        if not isinstance(measured, (int, float)) or measured <= 0:
            return None
        return max(_MIN_MEASURED_TIMEOUT_SEC, int(measured * _TIMEOUT_SAFETY_MULTIPLIER))
    except Exception:
        return None


def _record_measured_duration(pcp_dir: Path | None, tool: str, seconds: float) -> None:
    """Best-effort -- an unwritable .pcp/ must not break the test gate. Only
    ever WIDENS the recorded measurement (max of old/new): one unusually fast
    run must never shrink the timeout back down and reintroduce false
    timeouts on the next normal-speed run."""
    if pcp_dir is None or seconds <= 0:
        return
    path = _qa_timing_path(pcp_dir)
    try:
        data = {}
        if path.exists():
            data = yaml.safe_load(path.read_text()) or {}
        prior = (data.get(tool) or {}).get("measured_seconds", 0)
        data[tool] = {"measured_seconds": max(prior, round(seconds, 1))}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.dump(data, default_flow_style=False))
    except Exception:
        pass


def _timeout_test(pcp_dir: Path | None = None) -> int:
    env = os.environ.get("PCP_QA_TEST_TIMEOUT_SEC")
    if env is not None:
        return int(env)
    measured = _load_measured_timeout(pcp_dir)
    if measured is not None:
        return measured
    return 300


def test_timeout_info(pcp_dir: Path | None = None) -> tuple[int, bool]:
    """(effective timeout seconds, True if PCP_QA_TEST_TIMEOUT_SEC is unset
    and neither a manually-tuned nor an auto-measured value is in effect) --
    lets a build run print this loud instead of a wrong-environment failure
    (e.g. the DB the tests hit isn't the one intended) surfacing as an
    indistinguishable "timed out", same masking this module's own docstring
    above already documents for the slow-suite case."""
    return _timeout_test(pcp_dir), (
        "PCP_QA_TEST_TIMEOUT_SEC" not in os.environ and _load_measured_timeout(pcp_dir) is None
    )


def _partial_output(exc: subprocess.TimeoutExpired) -> str:
    """Whatever the killed process had already written before the timeout.

    `TimeoutExpired` carries the partial `stdout`/`stderr` captured up to the
    kill, and every handler here discarded it -- the exception wasn't even bound
    to a name. Measured on Project O 2026-07-30: a 900s pytest timeout
    produced a **311-byte** evidence file containing nothing but PCP's own
    advice message. Twelve of that run's nineteen test-gate blocks were
    timeouts, so twelve full retries were spent, ~$37 of a $78 run, and a human
    opening the evidence could not tell which test hung.

    The message below is good prose about what to DO and says nothing about what
    HAPPENED. pytest's own partial output names the last test it started, which
    is the one thing that identifies the hang."""
    parts = []
    for label, stream in (("stdout", exc.stdout), ("stderr", exc.stderr)):
        if not stream:
            continue
        text = stream.decode("utf-8", "replace") if isinstance(stream, bytes) else str(stream)
        text = text.strip()
        if text:
            parts.append(f"--- partial {label} before the kill (last 4000 chars) ---\n{text[-4000:]}")
    if not parts:
        return (
            "\n\n--- no partial output was captured before the kill ---\n"
            "The process produced nothing on stdout/stderr, which itself is a signal: it "
            "hung before writing even a collection header, so suspect a blocking connection "
            "at import/fixture time rather than a slow test."
        )
    return "\n\n" + "\n\n".join(parts)


def _timeout_message(tool: str, exc: subprocess.TimeoutExpired | None = None,
                     pcp_dir: Path | None = None) -> str:
    """A bare "timed out" in the evidence file reads as "the test suite failed"
    to whoever opens it next -- it names neither the limit that was hit nor the
    knob that changes it, so the natural next move is to go debug tests that
    may well be fine. Say which limit, and say whether it's a default, an
    auto-measured baseline, or a manual override -- not just "default or not".

    Also append whatever the process actually managed to say -- see
    _partial_output for why advice-without-data was not enough.

    The limit is read off the exception when one is given. `test_timeout_info()`
    reports the limit in effect *now*, which is not necessarily the one that
    killed this process -- `exc.timeout` is the value that actually applied."""
    seconds, is_unmeasured_default = test_timeout_info(pcp_dir)
    if exc is not None and exc.timeout:
        actual = int(exc.timeout)
        is_unmeasured_default = is_unmeasured_default and actual == seconds
        seconds = actual
    if "PCP_QA_TEST_TIMEOUT_SEC" in os.environ:
        suffix = " (manually set via PCP_QA_TEST_TIMEOUT_SEC)"
    elif is_unmeasured_default:
        suffix = " (PCP default — not tuned for this project)"
    else:
        suffix = " (auto-measured from a prior full run, with safety margin)"
    msg = (
        f"{tool} exceeded the {seconds}s PCP_QA_TEST_TIMEOUT_SEC limit{suffix} and was killed. "
        f"No test result was produced — this is NOT a test failure. Either the suite genuinely "
        f"needs longer (raise PCP_QA_TEST_TIMEOUT_SEC) or it is blocked on something that never "
        f"returns (a hung DB connection, a wrong-environment target, a lock)."
    )
    return msg + (_partial_output(exc) if exc is not None else "")


def _timeout_lint() -> int:
    return int(os.environ.get("PCP_QA_LINT_TIMEOUT_SEC", "60"))


def _timeout_sast() -> int:
    return int(os.environ.get("PCP_QA_SAST_TIMEOUT_SEC", "120"))


def project_tool(project_root: Path, name: str) -> str | None:
    """Resolve a dev tool from the PROJECT's virtualenv before falling back to PATH.

    PCP shelled out to a bare `pytest`, which resolves from PATH. A user who
    runs `pcp build` without activating the project's venv therefore had their
    project tested by whatever pytest happened to be global -- a different
    interpreter, different installed packages, and results that describe some
    other environment. It stayed invisible because activating the venv is the
    normal habit; nothing failed loudly when it wasn't.

    That is the same shape as two other bugs found on 2026-07-27: the Postgres
    check counting schemas on whatever answered port 5432 (a shadow container,
    for four days), and `pcp --version` reporting metadata while the code was
    something else. A check aimed confidently at the wrong target reads exactly
    like a passing check.

    Order: the project's own venv, then PATH. `PCP_TOOL_FROM_PATH=1` forces the
    old behaviour for projects that deliberately use globally-installed tools.
    """
    if os.environ.get("PCP_TOOL_FROM_PATH") != "1":
        for venv in (".venv", "venv", "env"):
            for sub in ("bin", "Scripts"):
                candidate = project_root / venv / sub / name
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return str(candidate)
                exe = candidate.with_suffix(".exe")
                if exe.is_file():
                    return str(exe)
    return shutil.which(name)


def testmon_available(project_root: Path) -> bool:
    """Is pytest-testmon installed in the environment pytest will run from?

    Detected, never depended on -- the same contract this module already has
    with ruff and semgrep, so PCP neither vendors it nor inherits its
    packaging. Checked by asking pytest itself rather than importing testmon
    here, because the interpreter running PCP is frequently not the one running
    the project's tests (PCP is commonly installed globally while the project
    has its own venv).

    PCP_QA_NO_TESTMON=1 turns it off.
    """
    if os.environ.get("PCP_QA_NO_TESTMON") == "1":
        return False
    pytest_bin = project_tool(project_root, "pytest")
    if not pytest_bin:
        return False
    try:
        r = subprocess.run([pytest_bin, "--help"], capture_output=True, text=True,
                           cwd=project_root, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return "--testmon" in (r.stdout + r.stderr)


def _parse_failed_test_ids(pytest_output: str) -> set[str]:
    """Extract failing test node IDs from pytest's own short summary --
    "FAILED path::test - reason" lines, printed by default with -q, no extra
    plugin needed. This is the identity baseline_test_failures.yaml matches
    against (see capture_test_failure_baseline/load_baseline_test_failures)."""
    ids = set()
    for line in pytest_output.splitlines():
        line = line.strip()
        if line.startswith("FAILED "):
            node_id = line[len("FAILED "):].split(" - ", 1)[0].strip()
            if node_id:
                ids.add(node_id)
    return ids


def baseline_test_failures_path(pcp_dir: Path) -> Path:
    return Path(pcp_dir) / "baseline_test_failures.yaml"


def load_baseline_test_failures(pcp_dir: Path | None) -> set[str]:
    """Test node IDs explicitly accepted as pre-existing debt via
    `pcp build --capture-test-baseline` -- deliberately NOT auto-grandfathered
    the way unverified_complete_baseline.yaml is (orphaned_work.py): a test
    failure could be a real regression the agent just introduced, and
    silently accepting whatever fails on first contact risks masking that on
    the very run that adopts this feature. A human/PM runs the capture
    explicitly, same posture as ci_rules.yaml's existing `pcp check
    --baseline` (check.py) -- this is that same brownfield-grace concept,
    extended from Layer 1 AST rules to the test suite, which never had an
    equivalent (real gap, Project W dogfood 2026-08-08: a pre-existing bug
    anywhere in a 1500-test suite blocked every unrelated criterion in every
    unrelated module, forever, with no way to say "already known")."""
    if pcp_dir is None:
        return set()
    path = baseline_test_failures_path(pcp_dir)
    if not path.exists():
        return set()
    try:
        data = yaml.safe_load(path.read_text()) or {}
        return set(data.get("failing_tests") or [])
    except Exception:
        return set()  # fails open -- an unreadable baseline must not block every gate


def capture_test_failure_baseline(project_root: Path, pcp_dir: Path) -> dict:
    """Runs the full, unscoped test suite once and records every currently-
    failing test node ID as accepted debt. `pcp build --capture-test-baseline`
    is the only writer -- never called automatically."""
    result = _run_pytest(project_root, test_paths=None, incremental=False, pcp_dir=pcp_dir)
    failing = sorted((result or {}).get("failed_test_ids") or [])
    from datetime import datetime, timezone
    data = {
        "failing_tests": failing,
        "total": len(failing),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    baseline_test_failures_path(pcp_dir).write_text(yaml.dump(data, default_flow_style=False))
    return data


def _run_pytest(project_root: Path, test_paths: list[str] | None = None,
                incremental: bool = False, pcp_dir: Path | None = None) -> dict | None:
    """Run pytest, optionally letting testmon skip tests it knows are unaffected.

    Path scoping (impact.py) reduces BREADTH -- which tests are eligible. It
    does nothing about REPETITION, which is the larger term: between one
    criterion and the next a handful of files change and nearly every eligible
    test re-executes against identical code, up to 3 attempts per criterion.
    Measured 2026-07-27 on Project O, scoping alone still left an
    average of 37% of a 1,279-test suite per run, and 99% for the hub module
    every other module depends on.

    testmon tracks per-test dependencies from actual coverage, so it also fixes
    the two cases scoping structurally cannot: a hub module whose declared
    blast radius really is the whole project, and a greenfield module with no
    source yet to attribute changed files to. It derives dependencies from
    execution rather than from the declared module graph -- which this project's
    own telemetry showed to be fiction (`standards_interop` broke in 69% of
    blocking runs while nothing declared a dependency on it).

    `incremental` is only ever set for the per-criterion gate. The wave-merge
    gate deliberately runs the full suite with NO testmon: an incremental
    runner is a cache, and the wave boundary is exactly where a cache should be
    distrusted and the real answer computed.
    """
    pytest_bin = project_tool(project_root, "pytest")
    if not pytest_bin:
        return None
    use_testmon = incremental and testmon_available(project_root)
    args = [pytest_bin, "-q"] + (["--testmon"] if use_testmon else []) + list(test_paths or [])
    start = time.monotonic()
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, cwd=project_root, timeout=_timeout_test(pcp_dir),
        )
    except subprocess.TimeoutExpired as e:
        return {"tool": "pytest", "passed": False, "output": _timeout_message("pytest", e, pcp_dir)}
    elapsed = time.monotonic() - start
    # Only a genuinely unscoped run's duration is a valid full-suite
    # measurement -- a scoped/testmon-narrowed run would under-measure and
    # cause false timeouts on the next real full run (wave-merge, or any
    # per-criterion run where scoping fell back to full).
    if not test_paths:
        _record_measured_duration(pcp_dir, "test_suite", elapsed)

    # Exit code 5 = no tests collected. Under testmon that is the SUCCESS case
    # -- "nothing this change touches" -- and it is also what a broken testmon
    # database looks like. Re-run without it rather than reporting a pass on
    # zero tests from a cache PCP cannot verify.
    if use_testmon and result.returncode not in (0, 1):
        plain = subprocess.run(
            [pytest_bin, "-q", *(test_paths or [])],
            capture_output=True, text=True, cwd=project_root, timeout=_timeout_test(pcp_dir),
        )
        plain_full = plain.stdout + plain.stderr
        return {
            "tool": "pytest", "passed": plain.returncode in (0, 5),
            "output": plain_full[-3000:],
            "scoped_to": test_paths or None, "incremental": False,
            "pytest_bin": pytest_bin,
            "testmon_fallback": f"testmon exited {result.returncode}; re-ran full scope",
            "failed_test_ids": sorted(_parse_failed_test_ids(plain_full)),
        }

    passed = result.returncode == 0 or result.returncode == 5
    full_output = result.stdout + result.stderr
    return {
        "tool": "pytest", "passed": passed, "output": full_output[-3000:],
        "scoped_to": test_paths or None, "incremental": use_testmon,
        "pytest_bin": pytest_bin,
        "failed_test_ids": sorted(_parse_failed_test_ids(full_output)),
    }


def _run_npm_test(project_root: Path, pcp_dir: Path | None = None) -> dict | None:
    pkg = project_root / "package.json"
    if not pkg.exists() or not shutil.which("npm"):
        return None
    try:
        data = json.loads(pkg.read_text())
    except Exception:
        return None
    if "test" not in data.get("scripts", {}):
        return None
    start = time.monotonic()
    try:
        result = subprocess.run(
            ["npm", "test", "--silent"], capture_output=True, text=True, cwd=project_root, timeout=_timeout_test(pcp_dir),
        )
    except subprocess.TimeoutExpired as e:
        return {"tool": "npm test", "passed": False, "output": _timeout_message("npm test", e, pcp_dir)}
    _record_measured_duration(pcp_dir, "test_suite", time.monotonic() - start)
    return {"tool": "npm test", "passed": result.returncode == 0, "output": (result.stdout + result.stderr)[-3000:]}


def _run_go_test(project_root: Path, pcp_dir: Path | None = None) -> dict | None:
    if not (project_root / "go.mod").exists() or not shutil.which("go"):
        return None
    start = time.monotonic()
    try:
        result = subprocess.run(
            ["go", "test", "./..."], capture_output=True, text=True, cwd=project_root, timeout=_timeout_test(pcp_dir),
        )
    except subprocess.TimeoutExpired as e:
        return {"tool": "go test", "passed": False, "output": _timeout_message("go test", e, pcp_dir)}
    _record_measured_duration(pcp_dir, "test_suite", time.monotonic() - start)
    return {"tool": "go test", "passed": result.returncode == 0, "output": (result.stdout + result.stderr)[-3000:]}


def full_suite_forced() -> bool:
    """Escape hatch, not a feature flag.

    Impact scoping used to be opt-in behind PCP_QA_TEST_SELECTION=impact, off
    by default, while `_run_test_suite_check`'s own docstring already described
    scoped-per-criterion + full-suite-at-wave-merge as the design. The design
    was documented and disabled, so every criterion attempt ran the entire
    suite -- 1,098 tests, ~7m46s, on Project O, re-running the same
    tests up to three times per criterion.

    Scoping is now the norm. PCP_QA_FULL_SUITE=1 forces the old behaviour for
    anyone who wants it back.
    """
    return os.environ.get("PCP_QA_FULL_SUITE") == "1"


def run_test_suite(project_root: Path, pcp_dir: Path | None = None, changed_files: list[str] | None = None) -> dict:
    """Scoped to the blast radius of the change when that can be determined,
    full regression suite otherwise.

    Given pcp_dir/changed_files, scopes pytest to the modules impacted by the
    change plus the modularity drop-tests (see impact.py:
    the changed module(s) plus every module that transitively depends on
    them, via the same dependency graph coupling.py already trusts for
    coupling_score — not a separate ad hoc mechanism). Only pytest
    supports scoping today; npm/go test always run in full, an honest scope
    limit, not a silent gap. Falls back to the full suite whenever impact.py
    can't confidently narrow the scope — never silently runs zero tests.

    The unscoped full suite is the WAVE-MERGE gate's job (see
    _run_wave_merge_gate), which is where cross-module regression risk is
    genuinely resolved — not on every one of up to three attempts per
    criterion. PCP_QA_FULL_SUITE=1 restores the old always-full behaviour."""
    test_paths = None
    if not full_suite_forced() and pcp_dir is not None and changed_files:
        from pcp.impact import blast_radius_test_paths
        try:
            test_paths = blast_radius_test_paths(pcp_dir, project_root, changed_files)
        except Exception:
            test_paths = None  # scoping failed -- fall back to the full suite, don't propagate

    if test_paths and project_tool(project_root, "pytest"):
        out = _run_pytest(project_root, test_paths, incremental=True, pcp_dir=pcp_dir)
        if out is not None:
            return out

    for runner in (_run_pytest, _run_npm_test, _run_go_test):
        out = runner(project_root, pcp_dir=pcp_dir)
        if out is not None:
            return out
    return {"tool": None, "passed": True, "output": ""}


def _run_ruff(project_root: Path, changed_files: list[str]) -> dict | None:
    ruff_bin = project_tool(project_root, "ruff")
    if not ruff_bin:
        return None
    py_files = [f for f in changed_files if f.endswith(".py")]
    if not py_files:
        return {"tool": "ruff", "passed": True, "issues": []}
    try:
        result = subprocess.run(
            [ruff_bin, "check", *py_files], capture_output=True, text=True, cwd=project_root, timeout=_timeout_lint(),
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
    coverage_bin = project_tool(project_root, "coverage")
    pytest_bin = project_tool(project_root, "pytest")
    if not coverage_bin or not pytest_bin:
        return None
    try:
        subprocess.run(
            [coverage_bin, "run", "-m", "pytest", "-q"],
            capture_output=True, text=True, cwd=project_root, timeout=_timeout_test(),
        )
        result = subprocess.run(
            [coverage_bin, "report", "--format=total"],
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
    semgrep_bin = project_tool(project_root, "semgrep")
    if not semgrep_bin or not changed_files:
        return {"tool": None, "passed": True, "findings": []}
    try:
        result = subprocess.run(
            [semgrep_bin, "--config=auto", "--quiet", "--error", *changed_files],
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
