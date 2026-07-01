import json
from pathlib import Path
from unittest.mock import patch

import yaml
from click.testing import CliRunner

from pcp.cli import cli
from pcp import capture, decision_log


def _write_transcript(path: Path, turns: list[tuple[str, str]]) -> None:
    lines = []
    for role, text in turns:
        lines.append(json.dumps({"message": {"role": role, "content": [{"type": "text", "text": text}]}}))
    # Interleave a tool_use block to confirm it's ignored.
    lines.insert(1, json.dumps({"message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Read", "input": {}}]}}))
    path.write_text("\n".join(lines))


def test_extract_conversation_text_skips_tool_blocks(tmp_path):
    transcript = tmp_path / "session.jsonl"
    _write_transcript(transcript, [
        ("user", "Let's drop the payments module and add referrals instead."),
        ("assistant", "Sounds good, I'll scaffold the referral module."),
    ])
    text = capture.extract_conversation_text(transcript)
    assert "drop the payments module" in text
    assert "Read" not in text
    assert text.startswith("User:")


def test_apply_business_items_creates_brd_and_supersedes(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()

    capture.apply_business_items(
        pcp_dir,
        [{"description": "Support payments via Stripe", "drift_flag": None, "supersedes": None}],
        source="session:abc",
    )
    items = yaml.safe_load((pcp_dir / "brd_items.yaml").read_text())["items"]
    assert len(items) == 1
    assert items[0]["id"] == "BRD-001"
    assert items[0]["status"] == "active"

    capture.apply_business_items(
        pcp_dir,
        [{"description": "Drop payments, add referral system instead",
          "drift_flag": "conflicts with objective.md's payments-first scope", "supersedes": "BRD-001"}],
        source="session:def",
    )
    items = yaml.safe_load((pcp_dir / "brd_items.yaml").read_text())["items"]
    assert len(items) == 2
    superseded = next(i for i in items if i["id"] == "BRD-001")
    assert superseded["status"] == "superseded"
    assert superseded["superseded_by"] == "BRD-002"

    brd_md = (pcp_dir / "brd.md").read_text()
    assert "Drift Flags" in brd_md
    assert "BRD-002" in brd_md
    assert "Superseded" in brd_md


def test_apply_technical_items_appends_decision_log(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    capture.apply_technical_items(
        pcp_dir,
        [{"category": "library-choice", "summary": "Chose Postgres over SQLite for concurrency", "evidence": "..."}],
        source="build:core:A001",
        session_id="sess-1",
    )
    records = decision_log.load(pcp_dir)
    assert len(records) == 1
    assert records[0]["category"] == "library-choice"
    assert records[0]["source"] == "build:core:A001"


def test_run_capture_never_raises_on_bad_transcript(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("# Objective")
    bad_transcript = tmp_path / "empty.jsonl"
    bad_transcript.write_text("")
    result = capture.run_capture(pcp_dir, bad_transcript, source="session:x")
    assert result == {"skipped": "empty transcript"}


def test_capture_cli_classifies_and_writes_artifacts(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("# Objective\nBuild a calculator app.")

    transcript = tmp_path / "session.jsonl"
    _write_transcript(transcript, [
        ("user", "Actually let's also support subtraction, not just addition."),
        ("assistant", "Got it, I'll add a subtract module."),
    ])

    mock_response = {
        "business_items": [
            {"description": "Support subtraction in addition to addition", "evidence": "...", "supersedes": None, "drift_flag": None}
        ],
        "technical_items": [
            {"category": "architecture", "summary": "Added a separate subtract module", "evidence": "..."}
        ],
    }

    with patch("pcp.llm.client.call_json") as mock_call_json:
        mock_call_json.return_value = mock_response
        runner = CliRunner()
        result = runner.invoke(cli, [
            "capture", "--path", str(tmp_path), "--transcript-file", str(transcript),
        ])

    assert result.exit_code == 0
    assert "1 business item(s)" in result.output
    assert "1 technical item(s)" in result.output
    assert (pcp_dir / "brd.md").exists()
    assert (pcp_dir / "decision_log.jsonl").exists()
