"""Proactive flaky-test detection (2026-08-08) -- facet 2 of the testing
prior-art sweep (facet 1: falsegreen, static false-green pattern scan;
this facet: empirically catching a test that flips pass/fail across
identical runs of identical code).

Real gap this closes: `pcp watch` only classifies a test as flaky
*reactively*, via LLM judgment, after a real CI failure already happened
(watch.py's auto-fix prompt). Nothing in PCP ever asked "is this test
flaky?" before that failure occurred.

Prior-art check (2026-08-08):
  Candidates considered:
    - pytest-randomly (MIT, mature, widely used) -- shuffles test
      execution order and reseeds `random`/`np.random` each run, the
      standard way to surface ORDER-dependent state leaks. Doesn't itself
      detect anything; it's an execution-order engine other tooling
      layers on top of.
    - pytest-repeat (MIT) -- reruns a single test/session N times in one
      process. Overlapping goal, narrower (one test at a time, not a
      structured full-suite N-run diff); pytest-randomly's shuffling is
      the more valuable property to reuse since order-dependency is the
      single most common real-world flaky-test root cause (arXiv:2101.09077).
    - pytest-rerunfailures / the `flaky` plugin -- these are MITIGATION
      (auto-retry a failing test into a pass), not detection. Using them
      would actively hide the exact signal this module exists to surface.
    - FLAPY (academic research tool) -- does constant-vs-random-order
      rerun diffing, closest conceptual match, but a research prototype:
      not on PyPI as an installable package, not positioned for
      programmatic embedding the way this needs.
  Decision: reuse-as-dependency for pytest-randomly (order-randomization
  engine, detected via shutil.which/importlib same posture as every other
  optional tool in this codebase); build-fresh for the N-run diff/report
  wrapper itself -- no existing tool exposes "run N times, diff per-test
  outcome, tell me which ones flipped" as a single reusable unit.
  Rationale: matches this project's own detect-and-shell-out convention
  (qa.py/audit.py/mutation_confirm.py) rather than vendoring a heavier,
  less-maintained research tool for a piece pytest-randomly already
  covers.

Same cost tier as mutation_confirm.py: real, opt-in, never auto-run. A
full-suite pytest invocation repeated N times costs N times a normal
test-suite run -- this is `pcp audit --flaky-detect`, not something wired
into `pcp build`'s per-criterion loop.

Detection has a known ceiling, honestly documented: order-dependent and
gross state-leak flakiness surface within a handful of runs; low-probability
timing/network flakiness needs on the order of 100 reruns to reliably catch
(same empirical finding this module's own docstring cites its literature
search against). Default run count (3) is tuned for catching the common
case cheaply, not exhaustive detection -- PCP_FLAKY_DETECT_RUNS raises it
for a deeper sweep at proportionally higher cost.
"""

import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from pcp.qa import project_tool

_OUTCOME_PASSED = "passed"
_OUTCOME_FAILED = "failed"
_OUTCOME_ERROR = "error"
_OUTCOME_SKIPPED = "skipped"


def pytest_randomly_available(project_root: Path) -> bool:
    """Whether the target project's own pytest can load pytest-randomly --
    checked by asking that project's pytest to list its plugins, not this
    process's own environment (the target project's venv is what actually
    executes the runs)."""
    pytest_bin = project_tool(project_root, "pytest")
    if not pytest_bin:
        return False
    try:
        result = subprocess.run(
            [pytest_bin, "--version", "-V"], capture_output=True, text=True,
            cwd=project_root, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return "randomly" in (result.stdout + result.stderr).lower()


def _parse_junit_outcomes(xml_path: Path) -> dict[str, str]:
    """Node ID -> outcome for one run's JUnit XML. Missing/unparseable file
    fails open to an empty dict -- a bad run contributes no evidence rather
    than crashing the whole sweep."""
    if not xml_path.exists():
        return {}
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError:
        return {}
    outcomes = {}
    for case in tree.getroot().iter("testcase"):
        classname = case.get("classname", "")
        name = case.get("name", "")
        node_id = f"{classname}::{name}" if classname else name
        if case.find("failure") is not None:
            outcomes[node_id] = _OUTCOME_FAILED
        elif case.find("error") is not None:
            outcomes[node_id] = _OUTCOME_ERROR
        elif case.find("skipped") is not None:
            outcomes[node_id] = _OUTCOME_SKIPPED
        else:
            outcomes[node_id] = _OUTCOME_PASSED
    return outcomes


def run_flaky_detection(
    project_root: Path, runs: int = 3, timeout_sec: float = 300.0,
) -> dict:
    """Runs the full suite `runs` times, diffs per-test outcomes, reports
    every test that was BOTH passed and failed/errored across identical
    runs of identical code -- the direct empirical definition of flaky.

    Skipped tests are tracked but never counted as flaky on their own (a
    test that's skipped in every run reveals nothing either way).

    Returns {"available": False} if this project has no detectable pytest.
    Never raises -- a single run's timeout/crash is recorded as a run with
    zero collected outcomes rather than aborting the whole sweep, since a
    run that times out on one seed is itself sometimes the interesting
    signal (a hang is worth surfacing, not swallowing)."""
    pytest_bin = project_tool(project_root, "pytest")
    if not pytest_bin:
        return {"available": False}

    randomized = pytest_randomly_available(project_root)
    per_run_outcomes = []
    per_run_timed_out = []

    with tempfile.TemporaryDirectory() as tmp:
        for i in range(runs):
            xml_path = Path(tmp) / f"run_{i}.xml"
            args = [pytest_bin, "-q", f"--junit-xml={xml_path}"]
            timed_out = False
            try:
                subprocess.run(
                    args, capture_output=True, text=True, cwd=project_root,
                    timeout=timeout_sec,
                )
            except subprocess.TimeoutExpired:
                timed_out = True
            except OSError:
                timed_out = True
            per_run_timed_out.append(timed_out)
            per_run_outcomes.append(_parse_junit_outcomes(xml_path))

    all_ids = set()
    for outcomes in per_run_outcomes:
        all_ids.update(outcomes)

    flaky_tests = []
    for test_id in sorted(all_ids):
        seen = [outcomes.get(test_id) for outcomes in per_run_outcomes]
        seen_present = [o for o in seen if o is not None]
        has_pass = _OUTCOME_PASSED in seen_present
        has_fail = _OUTCOME_FAILED in seen_present or _OUTCOME_ERROR in seen_present
        if has_pass and has_fail:
            flaky_tests.append({"test_id": test_id, "outcomes": seen})

    return {
        "available": True,
        "runs": runs,
        "order_randomized": randomized,
        "any_run_timed_out": any(per_run_timed_out),
        "total_unique_tests": len(all_ids),
        "flaky_tests": flaky_tests,
    }


def flaky_detect_available(project_root: Path) -> bool:
    return project_tool(project_root, "pytest") is not None
