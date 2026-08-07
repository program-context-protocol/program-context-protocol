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


def test_apply_technical_items_threads_severity_into_decision_log(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    capture.apply_technical_items(
        pcp_dir,
        [{"category": "architecture", "summary": "No durable store backs rollout state",
          "evidence": "...", "severity": "high"}],
        source="session:abc",
        session_id="sess-1",
    )
    records = decision_log.load(pcp_dir)
    assert records[0]["severity"] == "high"


def test_apply_technical_items_defaults_severity_to_low(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    capture.apply_technical_items(
        pcp_dir,
        [{"category": "library-choice", "summary": "Chose Postgres", "evidence": "..."}],
        source="session:abc",
        session_id="sess-1",
    )
    records = decision_log.load(pcp_dir)
    assert records[0]["severity"] == "low"


def test_escalation_candidates_filters_to_high_severity_only():
    items = [
        {"summary": "no durable store", "severity": "high"},
        {"summary": "picked yaml over json", "severity": "low"},
        {"summary": "worth remembering", "severity": "medium"},
    ]
    result = capture.escalation_candidates(items)
    assert len(result) == 1
    assert result[0]["summary"] == "no durable store"


def test_run_capture_surfaces_escalations(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("# Objective")
    transcript = tmp_path / "session.jsonl"
    _write_transcript(transcript, [("user", "hello"), ("assistant", "hi")])

    mock_response = {
        "business_items": [],
        "technical_items": [
            {"category": "architecture", "summary": "State resets on redeploy, no durable store",
             "evidence": "...", "severity": "high"},
        ],
    }
    with patch("pcp.llm.client.call_json") as mock_call_json:
        mock_call_json.return_value = mock_response
        result = capture.run_capture(pcp_dir, transcript, source="session:x", session_id="sess1")

    assert len(result["escalations"]) == 1
    assert result["escalations"][0]["summary"] == "State resets on redeploy, no durable store"


def test_capture_cli_prints_escalation_suggestion(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("# Objective")
    transcript = tmp_path / "session.jsonl"
    _write_transcript(transcript, [("user", "hello"), ("assistant", "hi")])

    mock_response = {
        "business_items": [],
        "technical_items": [
            {"category": "architecture", "summary": "No durable store for rollout state",
             "evidence": "...", "severity": "high"},
        ],
    }
    with patch("pcp.llm.client.call_json") as mock_call_json:
        mock_call_json.return_value = mock_response
        runner = CliRunner()
        result = runner.invoke(cli, [
            "capture", "--path", str(tmp_path), "--transcript-file", str(transcript),
        ])

    assert result.exit_code == 0
    assert "high-severity technical decision" in result.output
    assert "pcp pm" in result.output
    assert "No durable store for rollout state" in result.output


def test_run_capture_never_raises_on_bad_transcript(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("# Objective")
    bad_transcript = tmp_path / "empty.jsonl"
    bad_transcript.write_text("")
    result = capture.run_capture(pcp_dir, bad_transcript, source="session:x")
    assert result["skipped"] == "empty transcript"
    # Archived anyway -- an empty/routine transcript is still a real record.
    assert result["archived_path"] is not None


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


# ── raw transcript archival (CTRL-011) ──

def test_archive_transcript_writes_gzipped_copy(tmp_path):
    import gzip

    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"message": {"role": "user", "content": "hi"}}\n')

    rel = capture.archive_transcript(pcp_dir, transcript, "abc123")
    assert rel == "transcripts/abc123.jsonl.gz"
    with gzip.open(pcp_dir / rel, "rt") as f:
        assert f.read() == transcript.read_text()


def test_archive_transcript_none_when_transcript_missing(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    assert capture.archive_transcript(pcp_dir, tmp_path / "nope.jsonl", "abc123") is None
    assert capture.archive_transcript(pcp_dir, None, "abc123") is None


def test_archive_transcript_falls_back_to_filename_without_session_id(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    transcript = tmp_path / "mysession.jsonl"
    transcript.write_text("content")
    rel = capture.archive_transcript(pcp_dir, transcript, None)
    assert rel == "transcripts/mysession.jsonl.gz"


def test_run_capture_records_archival_to_telemetry(tmp_path):
    from pcp import telemetry

    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("# Objective")
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")  # empty -- classification skips, archival still runs

    capture.run_capture(pcp_dir, transcript, source="session:x", session_id="sess1")

    records = telemetry.load(pcp_dir)
    archive_records = [r for r in records if r.get("check") == "transcript-archive"]
    assert len(archive_records) == 1
    assert archive_records[0]["control_id"] == "CTRL-011"
    assert archive_records[0]["result"] == "pass"
    assert archive_records[0]["evidence_path"] == "transcripts/sess1.jsonl.gz"


def test_run_capture_includes_archived_path_on_success(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("# Objective")
    transcript = tmp_path / "session.jsonl"
    _write_transcript(transcript, [("user", "hello"), ("assistant", "hi")])

    with patch("pcp.llm.client.call_json") as mock_call_json:
        mock_call_json.return_value = {"business_items": [], "technical_items": []}
        result = capture.run_capture(pcp_dir, transcript, source="session:x", session_id="sess2")

    assert result["archived_path"] == "transcripts/sess2.jsonl.gz"
