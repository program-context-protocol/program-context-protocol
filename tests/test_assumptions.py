from click.testing import CliRunner

from pcp import assumptions
from pcp.cli import cli


def test_merge_new_adds_items_and_assigns_ids(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    added = assumptions.merge_new(
        pcp_dir,
        ["Users have a stable internet connection during grading", "Warehouse inventory counts refresh nightly via batch import"],
        source="kickoff",
    )
    assert [a["id"] for a in added] == ["AS001", "AS002"]
    assert all(a["status"] == "open" for a in added)
    assert all(a["source"] == "kickoff" for a in added)

    items = assumptions.load(pcp_dir)
    assert len(items) == 2
    assert (pcp_dir / "assumptions.md").exists()


def test_merge_new_dedups_reworded_duplicate(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    assumptions.merge_new(pcp_dir, ["Users have a stable internet connection during grading"], source="kickoff")
    added = assumptions.merge_new(
        pcp_dir, ["Users have a stable internet connection while grading submissions"], source="pm",
    )
    assert added == []
    assert len(assumptions.load(pcp_dir)) == 1


def test_merge_new_continues_numbering_across_calls(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    assumptions.merge_new(pcp_dir, ["Assumption about external payment gateway uptime"], source="kickoff")
    added = assumptions.merge_new(pcp_dir, ["Assumption about warehouse inventory data freshness"], source="pm")
    assert added[0]["id"] == "AS002"


def test_merge_new_empty_statements_is_noop_but_still_writes_md(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    added = assumptions.merge_new(pcp_dir, [], source="kickoff")
    assert added == []
    assert (pcp_dir / "assumptions.md").exists()
    assert "No assumptions recorded yet" in (pcp_dir / "assumptions.md").read_text()


def test_set_status_confirmed(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    added = assumptions.merge_new(pcp_dir, ["A load-bearing assumption about legacy system availability"], source="kickoff")
    item_id = added[0]["id"]
    found = assumptions.set_status(pcp_dir, item_id, "confirmed")
    assert found is True
    items = assumptions.load(pcp_dir)
    assert items[0]["status"] == "confirmed"
    assert "confirmed_at" in items[0]


def test_set_status_invalidated_requires_reason(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    added = assumptions.merge_new(pcp_dir, ["A load-bearing assumption about legacy system availability"], source="kickoff")
    item_id = added[0]["id"]
    try:
        assumptions.set_status(pcp_dir, item_id, "invalidated")
        assert False, "expected ValueError"
    except ValueError:
        pass
    found = assumptions.set_status(pcp_dir, item_id, "invalidated", "turned out the legacy system was decommissioned")
    assert found is True
    assert assumptions.load(pcp_dir)[0]["status"] == "invalidated"


def test_set_status_unknown_id_returns_false(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    assert assumptions.set_status(pcp_dir, "AS999", "confirmed") is False


def test_set_status_rejects_invalid_status(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    try:
        assumptions.set_status(pcp_dir, "AS001", "bogus")
        assert False, "expected ValueError"
    except ValueError:
        pass


# ── check_assumptions_enumerated (kickoff.py) ──

def test_check_assumptions_enumerated_flags_empty_with_real_capabilities():
    from pcp.commands.kickoff import check_assumptions_enumerated
    warnings = check_assumptions_enumerated(["A capability"], [])
    assert len(warnings) == 1
    assert "assumptions_enumerated is empty" in warnings[0]


def test_check_assumptions_enumerated_silent_when_populated():
    from pcp.commands.kickoff import check_assumptions_enumerated
    assert check_assumptions_enumerated(["A capability"], ["An assumption"]) == []


def test_check_assumptions_enumerated_silent_when_no_capabilities_either():
    from pcp.commands.kickoff import check_assumptions_enumerated
    assert check_assumptions_enumerated([], []) == []


# ── CLI ──

def test_assumptions_cli_json(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    assumptions.merge_new(pcp_dir, ["A stated assumption for CLI testing purposes"], source="kickoff")
    runner = CliRunner()
    result = runner.invoke(cli, ["assumptions", "--path", str(tmp_path), "--json"])
    assert result.exit_code == 0
    assert "AS001" in result.output


def test_assumptions_cli_no_items(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["assumptions", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "No assumptions recorded yet" in result.output


def test_assumptions_cli_confirm(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    assumptions.merge_new(pcp_dir, ["A stated assumption for CLI confirm testing"], source="kickoff")
    runner = CliRunner()
    result = runner.invoke(cli, ["assumptions", "--path", str(tmp_path), "--confirm", "AS001"])
    assert result.exit_code == 0
    assert "confirmed" in result.output.lower()
    assert assumptions.load(pcp_dir)[0]["status"] == "confirmed"


def test_assumptions_cli_invalidate_without_reason_errors(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    assumptions.merge_new(pcp_dir, ["A stated assumption for CLI invalidate testing"], source="kickoff")
    runner = CliRunner()
    result = runner.invoke(cli, ["assumptions", "--path", str(tmp_path), "--invalidate", "AS001"])
    assert result.exit_code == 2


def test_assumptions_cli_invalidate_with_reason(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    assumptions.merge_new(pcp_dir, ["A stated assumption for CLI invalidate testing"], source="kickoff")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["assumptions", "--path", str(tmp_path), "--invalidate", "AS001", "--reason", "no longer true"]
    )
    assert result.exit_code == 0
    assert assumptions.load(pcp_dir)[0]["status"] == "invalidated"


def test_assumptions_cli_no_pcp_dir_exits(tmp_path):
    runner = CliRunner()
    result = runner.invoke(cli, ["assumptions", "--path", str(tmp_path)])
    assert result.exit_code == 2


# ── prompt wiring ──

def test_kickoff_prompt_instructs_assumptions_field():
    from pcp.commands.kickoff import SYSTEM_PROMPT
    assert "assumptions_enumerated" in SYSTEM_PROMPT
    assert "DECLARE ASSUMPTIONS" in SYSTEM_PROMPT


def test_pm_prompt_instructs_assumptions_field():
    from pcp.commands.pm import SYSTEM_PROMPT
    assert "assumptions_enumerated" in SYSTEM_PROMPT
    assert "DECLARE ASSUMPTIONS" in SYSTEM_PROMPT
