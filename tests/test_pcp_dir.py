import pytest

from pcp.pcp_dir import find_pcp_dir, get_modules_dir, get_objective, get_decomposition, get_ontology_state, NoPCPDir


def test_find_pcp_dir_at_start(tmp_path):
    (tmp_path / ".pcp").mkdir()
    assert find_pcp_dir(tmp_path) == tmp_path / ".pcp"


def test_find_pcp_dir_walks_up_from_subdirectory(tmp_path):
    (tmp_path / ".pcp").mkdir()
    sub = tmp_path / "src" / "nested" / "deep"
    sub.mkdir(parents=True)
    assert find_pcp_dir(sub) == tmp_path / ".pcp"


def test_find_pcp_dir_raises_when_absent(tmp_path):
    with pytest.raises(NoPCPDir):
        find_pcp_dir(tmp_path)


def test_accessors_return_expected_relative_paths(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    assert get_modules_dir(pcp_dir) == pcp_dir / "strategy" / "modules"
    assert get_objective(pcp_dir) == pcp_dir / "objective.md"
    assert get_decomposition(pcp_dir) == pcp_dir / "strategy" / "decomposition.md"
    assert get_ontology_state(pcp_dir) == pcp_dir / "ontology_state.yaml"
