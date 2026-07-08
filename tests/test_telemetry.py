from pcp import telemetry


def test_infer_languages_maps_known_extensions():
    langs = telemetry.infer_languages(["a.py", "b.ts", "c.go", "d.md"])
    assert langs == ["Go", "Markdown", "Python", "TypeScript"]


def test_infer_languages_falls_back_to_bare_extension():
    langs = telemetry.infer_languages(["weird.xyz"])
    assert langs == ["xyz"]


def test_infer_languages_handles_extensionless_file():
    langs = telemetry.infer_languages(["Makefile"])
    assert langs == ["unknown"]


def test_count_diff_lines_excludes_headers():
    diff = "--- a/file.py\n+++ b/file.py\n+added line\n-removed line\n context line\n"
    added, removed = telemetry.count_diff_lines(diff)
    assert added == 1
    assert removed == 1


def test_count_diff_lines_empty_diff():
    assert telemetry.count_diff_lines("") == (0, 0)


def test_record_and_load_roundtrip(tmp_path):
    telemetry.record(tmp_path, cycle="build", module="add", criterion_id="A001", token_input=100)
    records = telemetry.load(tmp_path)
    assert len(records) == 1
    assert records[0]["module"] == "add"
    assert records[0]["cycle"] == "build"
    assert "timestamp" in records[0]


def test_load_returns_empty_when_missing(tmp_path):
    assert telemetry.load(tmp_path) == []


def test_successive_records_are_hash_chained(tmp_path):
    from pcp.evidence_chain import verify_chain

    telemetry.record(tmp_path, cycle="build", module="add", criterion_id="A001")
    telemetry.record(tmp_path, cycle="qa", module="add", criterion_id="A001", result="pass")
    telemetry.record(tmp_path, cycle="qa", module="add", criterion_id="A001", result="block")

    records = telemetry.load(tmp_path)
    assert records[0]["prev_hash"] == "genesis"
    assert records[1]["prev_hash"] == records[0]["entry_hash"]
    assert records[2]["prev_hash"] == records[1]["entry_hash"]
    assert verify_chain(records) == []


def test_hand_edited_record_breaks_the_chain(tmp_path):
    from pcp.evidence_chain import verify_chain

    telemetry.record(tmp_path, cycle="build", module="add")
    telemetry.record(tmp_path, cycle="qa", module="add", result="block")

    path = tmp_path / "telemetry.jsonl"
    lines = path.read_text().splitlines()
    import json
    tampered = json.loads(lines[1])
    tampered["result"] = "pass"  # someone quietly "fixed" a block after the fact
    lines[1] = json.dumps(tampered)
    path.write_text("\n".join(lines) + "\n")

    breaks = verify_chain(telemetry.load(tmp_path))
    assert len(breaks) == 1
    assert breaks[0]["index"] == 1


def test_load_skips_corrupt_lines(tmp_path):
    path = tmp_path / "telemetry.jsonl"
    path.write_text('{"cycle": "build"}\n{{broken\n{"cycle": "qa"}\n')
    records = telemetry.load(tmp_path)
    assert len(records) == 2


def test_aggregate_rolls_up_build_and_qa_by_module():
    records = [
        {"cycle": "build", "module": "add", "criterion_id": "A001", "token_input": 100, "token_output": 50,
         "cost_usd": 0.01, "languages": ["Python"]},
        {"cycle": "build", "module": "add", "criterion_id": "A002", "token_input": 200, "token_output": 80,
         "cost_usd": 0.02, "languages": ["Python"]},
        {"cycle": "qa", "module": "add", "result": "pass"},
        {"cycle": "qa", "module": "add", "result": "block"},
        {"cycle": "build", "module": "sub", "criterion_id": "S001", "token_input": 10, "token_output": 5},
    ]
    result = telemetry.aggregate(records)
    add = result["by_module"]["add"]
    assert add["attempts"] == 2
    assert add["criteria"] == {"A001", "A002"}
    assert add["tokens_in"] == 300
    assert add["cost"] == 0.03
    assert add["languages"] == {"Python"}
    assert add["qa_total"] == 2
    assert add["qa_blocks"] == 1
    assert result["by_module"]["sub"]["attempts"] == 1


def test_aggregate_handles_missing_module_field():
    records = [{"cycle": "build", "criterion_id": "X001"}]
    result = telemetry.aggregate(records)
    assert result["by_module"]["?"]["attempts"] == 1
