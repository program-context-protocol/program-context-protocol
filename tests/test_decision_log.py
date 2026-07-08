from pcp import decision_log


def test_record_appends_jsonl_with_timestamp(tmp_path):
    decision_log.record(tmp_path, category="library-choice", summary="Chose Postgres", source="session:1")
    records = decision_log.load(tmp_path)
    assert len(records) == 1
    assert records[0]["category"] == "library-choice"
    assert "timestamp" in records[0]


def test_load_returns_empty_list_when_no_file(tmp_path):
    assert decision_log.load(tmp_path) == []


def test_load_skips_corrupt_lines(tmp_path):
    path = tmp_path / "decision_log.jsonl"
    path.write_text('{"category": "a", "summary": "ok"}\nnot json\n{"category": "b", "summary": "ok2"}\n')
    records = decision_log.load(tmp_path)
    assert len(records) == 2
    assert records[0]["category"] == "a"
    assert records[1]["category"] == "b"


def test_aggregate_groups_by_category():
    records = [
        {"category": "architecture", "summary": "a"},
        {"category": "architecture", "summary": "b"},
        {"category": "workaround", "summary": "c"},
        {"summary": "no category"},
    ]
    result = decision_log.aggregate(records)
    assert len(result["by_category"]["architecture"]) == 2
    assert len(result["by_category"]["workaround"]) == 1
    assert len(result["by_category"]["uncategorized"]) == 1
    assert result["records"] == records


def test_record_appends_multiple_entries_in_order(tmp_path):
    decision_log.record(tmp_path, category="a", summary="first")
    decision_log.record(tmp_path, category="b", summary="second")
    records = decision_log.load(tmp_path)
    assert [r["summary"] for r in records] == ["first", "second"]


def test_records_are_hash_chained(tmp_path):
    from pcp.evidence_chain import verify_chain

    decision_log.record(tmp_path, category="a", summary="first")
    decision_log.record(tmp_path, category="b", summary="second")
    records = decision_log.load(tmp_path)
    assert records[0]["prev_hash"] == "genesis"
    assert records[1]["prev_hash"] == records[0]["entry_hash"]
    assert verify_chain(records) == []
