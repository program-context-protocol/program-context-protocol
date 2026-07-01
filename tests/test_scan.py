from pathlib import Path

from pcp.commands.scan import _check_ast_pattern, _check_file_exists, _SOURCE_FILES_CACHE, _FILE_CONTENT_CACHE


def _reset_caches():
    _SOURCE_FILES_CACHE.clear()
    _FILE_CONTENT_CACHE.clear()


def test_ast_pattern_found_at_declared_target(tmp_path):
    _reset_caches()
    (tmp_path / "auth.py").write_text("def login(): pass\n")

    ok, detail = _check_ast_pattern("auth.py", r"def login", tmp_path)

    assert ok is True
    assert "auth.py" in detail


def test_ast_pattern_falls_back_when_feature_moved_to_another_file(tmp_path):
    """Refactor absorbed the spec'd feature into a differently-named file.

    The exact `target` no longer contains the pattern, but the pattern
    exists elsewhere in the tree — scan should not false-negative this.
    """
    _reset_caches()
    (tmp_path / "postcar_check.py").write_text("def validate_registration(): pass\n")

    ok, detail = _check_ast_pattern("registration_check.py", r"def validate_registration", tmp_path)

    assert ok is True
    assert "postcar_check.py" in detail
    assert "registration_check.py" in detail


def test_ast_pattern_not_found_anywhere_stays_pending(tmp_path):
    _reset_caches()
    (tmp_path / "other.py").write_text("def unrelated(): pass\n")

    ok, detail = _check_ast_pattern("missing.py", r"def never_written", tmp_path)

    assert ok is False


def test_file_exists_at_declared_path(tmp_path):
    _reset_caches()
    (tmp_path / "module.py").touch()

    ok, detail = _check_file_exists("module.py", tmp_path)

    assert ok is True


def test_file_exists_falls_back_to_moved_file(tmp_path):
    _reset_caches()
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "module.py").touch()

    ok, detail = _check_file_exists("module.py", tmp_path)

    assert ok is True
    assert "moved to" in detail


def test_file_exists_false_when_truly_absent(tmp_path):
    _reset_caches()

    ok, detail = _check_file_exists("nowhere.py", tmp_path)

    assert ok is False
