from pcp.evidence_chain import chain_entry, verify_chain


def test_first_entry_chains_to_genesis():
    entry = chain_entry(None, {"a": 1})
    assert entry["prev_hash"] == "genesis"
    assert "entry_hash" in entry


def test_second_entry_chains_to_first():
    e1 = chain_entry(None, {"a": 1})
    e2 = chain_entry(e1["entry_hash"], {"a": 2})
    assert e2["prev_hash"] == e1["entry_hash"]


def test_intact_chain_verifies_clean():
    e1 = chain_entry(None, {"a": 1})
    e2 = chain_entry(e1["entry_hash"], {"a": 2})
    e3 = chain_entry(e2["entry_hash"], {"a": 3})
    assert verify_chain([e1, e2, e3]) == []


def test_content_edit_after_the_fact_is_detected():
    e1 = chain_entry(None, {"a": 1})
    e2 = chain_entry(e1["entry_hash"], {"a": 2})
    e2_tampered = {**e2, "a": 999}  # edited without recomputing entry_hash
    breaks = verify_chain([e1, e2_tampered])
    assert len(breaks) == 1
    assert "content altered" in breaks[0]["issue"]


def test_deleted_entry_breaks_the_chain():
    e1 = chain_entry(None, {"a": 1})
    e2 = chain_entry(e1["entry_hash"], {"a": 2})
    e3 = chain_entry(e2["entry_hash"], {"a": 3})
    # e2 quietly removed -- e3's prev_hash no longer matches e1's entry_hash.
    breaks = verify_chain([e1, e3])
    assert len(breaks) == 1
    assert "prev_hash mismatch" in breaks[0]["issue"]


def test_reordered_entries_break_the_chain():
    e1 = chain_entry(None, {"a": 1})
    e2 = chain_entry(e1["entry_hash"], {"a": 2})
    breaks = verify_chain([e2, e1])
    assert len(breaks) >= 1


def test_empty_chain_verifies_clean():
    assert verify_chain([]) == []
