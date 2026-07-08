from pcp import evidence


def test_store_writes_content_and_returns_relative_path(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    rel = evidence.store(pcp_dir, "add", "A001", 1, "test-suite", "full pytest output here")
    stored = (pcp_dir / rel).read_text()
    assert stored == "full pytest output here"
    assert rel == "evidence/add/A001/attempt_1/test-suite.txt"


def test_store_handles_none_content(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    rel = evidence.store(pcp_dir, "add", "A001", 1, "lint", None)
    assert (pcp_dir / rel).read_text() == ""


def test_store_coerces_non_string_content(tmp_path):
    """Defensive boundary: a caller passing something odd (e.g. a mocked
    subprocess result in a test) shouldn't crash evidence storage."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    rel = evidence.store(pcp_dir, "add", "A001", 1, "sast", 12345)
    assert (pcp_dir / rel).read_text() == "12345"


def test_store_separates_attempts(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    rel1 = evidence.store(pcp_dir, "add", "A001", 1, "test-suite", "attempt one output")
    rel2 = evidence.store(pcp_dir, "add", "A001", 2, "test-suite", "attempt two output")
    assert rel1 != rel2
    assert (pcp_dir / rel1).read_text() == "attempt one output"
    assert (pcp_dir / rel2).read_text() == "attempt two output"


def test_store_handles_missing_module_or_criterion(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    rel = evidence.store(pcp_dir, None, None, 1, "test-suite", "output")
    assert (pcp_dir / rel).exists()
