"""module_logic_breakdown (lazy-agent backlog item 9), 2026-07-20 --
decompose-first one layer deeper than capabilities_enumerated: kickoff/pm's
pre-build coverage check (check_module_logic_breakdown_coverage) and
build.py's post-build built-code verification (CTRL-031)."""

from unittest.mock import patch

import yaml

from pcp.commands.kickoff import (
    check_module_logic_breakdown_coverage, loop_until_dry_breakdown, _is_genuinely_new,
)
from pcp.commands.build import _run_wave_logic_breakdown_check


# ── check_module_logic_breakdown_coverage (pre-build, deterministic) ──

def test_coverage_check_flags_uncovered_breakdown_item():
    specs = {"billing": {"module_logic_breakdown": ["invoice generation with tax calculation"]}}
    accs = {"billing": {"criteria": [{"description": "renders a billing summary page"}]}}
    findings = check_module_logic_breakdown_coverage(specs, accs)
    assert any("invoice generation" in f for f in findings)


def test_coverage_check_silent_when_criteria_mention_breakdown():
    specs = {"billing": {"module_logic_breakdown": ["invoice generation with tax calculation"]}}
    accs = {"billing": {"criteria": [{"description": "generate invoice with tax calculation applied"}]}}
    assert check_module_logic_breakdown_coverage(specs, accs) == []


def test_coverage_check_skips_modules_without_declared_breakdown():
    specs = {"billing": {}}
    accs = {"billing": {"criteria": []}}
    assert check_module_logic_breakdown_coverage(specs, accs) == []


# ── loop_until_dry_breakdown (opt-in multi-lens completeness pass) ──

def test_is_genuinely_new_true_for_distinct_item():
    assert _is_genuinely_new("rate limiting per user session", ["invoice generation"])


def test_is_genuinely_new_false_for_reworded_duplicate():
    assert not _is_genuinely_new(
        "generation of invoices", ["invoice generation with tax calculation"],
    )


def test_loop_until_dry_stops_after_two_consecutive_dry_rounds(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    responses = [
        {"new_items": ["rate limiting per user"]},
        {"new_items": []},
        {"new_items": []},
    ]
    with patch("pcp.commands.kickoff.llm.call_json", side_effect=responses):
        result, additions = loop_until_dry_breakdown(pcp_dir, "billing", "handles billing", ["invoice generation"])
    assert "rate limiting per user" in result
    assert len(result) == 2
    assert additions == [{"item": "rate limiting per user", "lens": "data-model"}]


def test_loop_until_dry_respects_max_rounds_cap(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    # Each round's item must use words unrelated to every other round's,
    # or the dedup check (_is_genuinely_new) correctly treats a repeated
    # word set as a reworded duplicate rather than a genuinely new finding.
    distinct_items = [
        "quarterly tax remittance scheduling",
        "currency conversion rounding policy",
        "refund eligibility windowing logic",
        "chargeback dispute escalation pathway",
    ]
    call_count = [0]

    def _always_new(*args, **kwargs):
        item = distinct_items[call_count[0]]
        call_count[0] += 1
        return {"new_items": [item]}

    with patch("pcp.commands.kickoff.llm.call_json", side_effect=_always_new):
        result, additions = loop_until_dry_breakdown(pcp_dir, "billing", "handles billing", [], max_rounds=4)
    assert call_count[0] == 4
    assert len(result) == 4
    assert len(additions) == 4
    assert [a["lens"] for a in additions] == ["data-model", "edge-case", "integration-dependency", "data-model"]


def test_loop_until_dry_breaks_cleanly_on_llm_error(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    with patch("pcp.commands.kickoff.llm.call_json", side_effect=RuntimeError("timeout")):
        result, additions = loop_until_dry_breakdown(pcp_dir, "billing", "handles billing", ["existing item"])
    assert result == ["existing item"]
    assert additions == []


# ── CTRL-031: built-code verification (post-build, wave-merge) ──

def _mod_dirs(pcp_dir, name, spec_extra, criteria):
    mod_dir = pcp_dir / "strategy" / "modules" / name
    mod_dir.mkdir(parents=True)
    spec = {"version": "2.0", "module": name, "description": "x module", **spec_extra}
    (mod_dir / "spec.yaml").write_text(yaml.dump(spec))
    (mod_dir / "acceptance.yaml").write_text(yaml.dump({"version": "2.0", "module": name, "criteria": criteria}))
    return mod_dir


def test_wave_logic_breakdown_check_inert_without_declared_breakdown(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _mod_dirs(pcp_dir, "billing", {}, [{"id": "A1", "description": "x", "status": "complete"}])
    findings = _run_wave_logic_breakdown_check(pcp_dir, [{"name": "billing"}], 0)
    assert findings == []


def test_wave_logic_breakdown_check_flags_item_with_no_built_evidence(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (tmp_path / "app.py").write_text("def render():\n    return 'ok'\n")
    _mod_dirs(
        pcp_dir, "billing",
        {"module_logic_breakdown": ["rate limiting per user session"]},
        [{"id": "A1", "description": "renders billing summary", "status": "complete", "target": "app.py"}],
    )
    findings = _run_wave_logic_breakdown_check(pcp_dir, [{"name": "billing"}], 0)
    assert any("rate limiting" in f for f in findings)


def test_wave_logic_breakdown_check_passes_when_target_file_reflects_item(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (tmp_path / "app.py").write_text("def rate_limit_per_user_session():\n    pass\n")
    _mod_dirs(
        pcp_dir, "billing",
        {"module_logic_breakdown": ["rate limiting per user session"]},
        [{"id": "A1", "description": "renders billing summary", "status": "complete", "target": "app.py"}],
    )
    findings = _run_wave_logic_breakdown_check(pcp_dir, [{"name": "billing"}], 0)
    assert findings == []


def test_wave_logic_breakdown_check_ignores_incomplete_criteria(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (tmp_path / "app.py").write_text("def rate_limit_per_user_session():\n    pass\n")
    _mod_dirs(
        pcp_dir, "billing",
        {"module_logic_breakdown": ["rate limiting per user session"]},
        [{"id": "A1", "description": "renders billing summary", "status": "pending", "target": "app.py"}],
    )
    findings = _run_wave_logic_breakdown_check(pcp_dir, [{"name": "billing"}], 0)
    assert any("rate limiting" in f for f in findings)
