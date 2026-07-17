import os

import pytest

from pcp.commands.build import check_agent_depth_or_exit, _max_agent_depth

# PCP_AGENT_DEPTH/PCP_MAX_AGENT_DEPTH isolation is handled globally by the
# autouse fixture in tests/conftest.py.


def test_max_agent_depth_defaults_to_one():
    assert _max_agent_depth() == 1


def test_max_agent_depth_overridable():
    os.environ["PCP_MAX_AGENT_DEPTH"] = "3"
    assert _max_agent_depth() == 3


def test_first_call_passes_and_sets_depth_to_one():
    check_agent_depth_or_exit()
    assert os.environ["PCP_AGENT_DEPTH"] == "1"


def test_nested_call_at_default_max_is_refused():
    # Simulates: build() ran once (depth->1), spawned a coding agent that
    # inherited PCP_AGENT_DEPTH=1, and that agent tries to run pcp build again.
    os.environ["PCP_AGENT_DEPTH"] = "1"
    with pytest.raises(SystemExit) as exc_info:
        check_agent_depth_or_exit()
    assert exc_info.value.code == 1


def test_raised_max_depth_allows_one_more_level():
    os.environ["PCP_MAX_AGENT_DEPTH"] = "2"
    os.environ["PCP_AGENT_DEPTH"] = "1"
    check_agent_depth_or_exit()  # should not raise
    assert os.environ["PCP_AGENT_DEPTH"] == "2"


def test_second_nested_call_at_raised_max_is_refused():
    os.environ["PCP_MAX_AGENT_DEPTH"] = "2"
    os.environ["PCP_AGENT_DEPTH"] = "2"
    with pytest.raises(SystemExit) as exc_info:
        check_agent_depth_or_exit()
    assert exc_info.value.code == 1
