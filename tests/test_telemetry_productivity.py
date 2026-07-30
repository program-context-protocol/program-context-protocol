"""Output per dollar, over time.

Nothing in PCP reported this, so a ~6x degradation on ontology-foundry went unseen
for a week while commits/day rose. Commit count and output per dollar pointed in
opposite directions and only the flattering one was visible.
"""
from pcp.telemetry import productivity_by_week


def _r(ts, cost=0.0, added=0, removed=0, cycle="build"):
    return {"timestamp": ts, "cost_usd": cost, "lines_added": added,
            "lines_removed": removed, "cycle": cycle}


def test_groups_by_iso_week_and_computes_dollars_per_net_line():
    out = productivity_by_week([
        _r("2026-07-16T10:00:00Z", cost=200.0, added=3000, removed=100),
        _r("2026-07-17T10:00:00Z", cost=164.0, added=2100, removed=46),
    ])
    assert len(out) == 1
    w = out[0]
    assert w["net_lines"] == 4954
    assert w["cost_usd"] == 364.0
    assert w["usd_per_net_line"] == round(364.0 / 4954, 3)


def test_separate_weeks_expose_a_degradation():
    out = productivity_by_week([
        _r("2026-07-16T10:00:00Z", cost=364.77, added=5000, removed=46),
        _r("2026-07-27T10:00:00Z", cost=263.89, added=700, removed=104),
    ])
    assert len(out) == 2
    assert out[0]["usd_per_net_line"] < out[1]["usd_per_net_line"]


def test_a_week_that_spent_money_and_netted_nothing_reports_none_not_zero():
    """The most important week to see must not render as a tidy $0.00/line."""
    out = productivity_by_week([_r("2026-07-27T10:00:00Z", cost=99.0, added=10, removed=200)])
    assert out[0]["net_lines"] == -190
    assert out[0]["usd_per_net_line"] is None


def test_zero_net_lines_is_also_none_not_a_division_error():
    out = productivity_by_week([_r("2026-07-27T10:00:00Z", cost=5.0, added=50, removed=50)])
    assert out[0]["net_lines"] == 0
    assert out[0]["usd_per_net_line"] is None


def test_only_build_records_count_as_attempts_but_all_costs_count():
    """QA/judge calls cost real money and belong in the spend figure."""
    out = productivity_by_week([
        _r("2026-07-16T10:00:00Z", cost=10.0, added=100, cycle="build"),
        _r("2026-07-16T11:00:00Z", cost=2.5, cycle="qa"),
    ])
    assert out[0]["attempts"] == 1
    assert out[0]["cost_usd"] == 12.5


def test_malformed_and_missing_timestamps_are_skipped_not_fatal():
    out = productivity_by_week([
        {"cost_usd": 1.0}, {"timestamp": "", "cost_usd": 1.0},
        {"timestamp": "not-a-date", "cost_usd": 1.0},
        {"timestamp": "2026-13-99T00:00:00Z", "cost_usd": 1.0},
        _r("2026-07-16T10:00:00Z", cost=3.0, added=30),
    ])
    assert len(out) == 1 and out[0]["cost_usd"] == 3.0


def test_weeks_come_back_in_chronological_order():
    out = productivity_by_week([
        _r("2026-07-27T10:00:00Z", cost=1.0, added=10),
        _r("2026-07-06T10:00:00Z", cost=1.0, added=10),
        _r("2026-07-16T10:00:00Z", cost=1.0, added=10),
    ])
    assert [w["week"] for w in out] == sorted(w["week"] for w in out)


def test_empty_input_is_empty_output():
    assert productivity_by_week([]) == []


# ── written vs landed ─────────────────────────────────────────────────────────

def test_repo_net_lines_splits_test_from_non_test(tmp_path):
    """Test churn must not be counted as product output — it is the thing that
    inflated ontology-foundry's ratio to 1.79 in the first place."""
    import subprocess
    from pcp.telemetry import repo_net_lines_by_week

    r = tmp_path / "r"
    (r / "tests").mkdir(parents=True)
    (r / "src").mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=r, check=True)
    (r / "src" / "a.py").write_text("x = 1\n" * 10)
    (r / "tests" / "test_a.py").write_text("assert 1\n" * 40)
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=r, check=True)

    out = repo_net_lines_by_week(r)["by_week"]
    assert len(out) == 1
    assert list(out.values())[0] == 10      # 40 test lines excluded

    both = repo_net_lines_by_week(r, exclude_tests=False)["by_week"]
    assert list(both.values())[0] == 50


def test_repo_net_lines_returns_empty_outside_a_git_repo(tmp_path):
    """Best-effort: callers degrade to the telemetry-only view, never break."""
    from pcp.telemetry import repo_net_lines_by_week
    out = repo_net_lines_by_week(tmp_path / "nope")
    assert out["by_week"] == {} and out["bulk_commits_skipped"] == {}


def test_survival_ratio_exposes_churn_that_written_lines_hide():
    """The ontology-foundry W31 case: 12,342 written, 599 landed = 5% survived,
    yet $/written line read as the best week of the run."""
    weeks = productivity_by_week([_r("2026-07-27T10:00:00Z", cost=223.26, added=12500, removed=158)])
    written = weeks[0]["net_lines"]
    assert written == 12342
    assert weeks[0]["usd_per_net_line"] < 0.02        # looks excellent
    landed = 599
    assert landed / written < 0.06                    # actually 5% survived


def test_vendored_bulk_commit_is_excluded_whole_and_reported(tmp_path):
    """ontology-foundry committed a whole drawio distribution (~450k lines of
    third-party .js) in one week and moved it the next, producing survival rates
    of 107372% and -11249%. No path heuristic catches that; size does."""
    import subprocess
    from pcp.telemetry import repo_net_lines_by_week

    r = tmp_path / "r"
    (r / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=r, check=True)

    (r / "src" / "real.py").write_text("x = 1\n" * 40)
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-qm", "authored"], cwd=r, check=True)

    # a vendored drop in a directory no vendor-name heuristic would flag
    (r / "web").mkdir()
    (r / "web" / "editor.js").write_text("// vendor\n" * 9000)
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-qm", "vendor drop"], cwd=r, check=True)

    out = repo_net_lines_by_week(r, bulk_commit_threshold=5000)
    assert sum(out["by_week"].values()) == 40           # the vendor drop is gone
    assert sum(out["bulk_commits_skipped"].values()) == 1
    assert out["bulk_threshold"] == 5000

    # and it is a threshold, not a hardcoded rule
    loose = repo_net_lines_by_week(r, bulk_commit_threshold=100000)
    assert sum(loose["by_week"].values()) == 9040
    assert loose["bulk_commits_skipped"] == {}


def test_lockfiles_and_generated_trees_never_count(tmp_path):
    from pcp.telemetry import _is_authored_source
    for p in ("package-lock.json", "poetry.lock", "web/node_modules/x/a.js",
              "dist/bundle.js", ".venv/lib/site-packages/y.py", "src/__pycache__/z.py",
              "docs/readme.md", "data/big.csv", "assets/logo.svg"):
        assert _is_authored_source(p) is False, p
    for p in ("src/a.py", "web/src/App.tsx", "cmd/main.go", "lib/x.rb", "q.sql"):
        assert _is_authored_source(p) is True, p
