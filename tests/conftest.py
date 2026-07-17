import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_agent_depth_env():
    """PCP_AGENT_DEPTH is set on os.environ by build.py/watch.py's
    check_agent_depth_or_exit() so it's inherited by spawned `claude`
    subprocess children the same way PCP_AGENT_SESSION already is. Unlike
    that var, PCP_AGENT_DEPTH directly sys.exit(1)s once the max is reached
    -- if one test's build()/watch() call sets it and never resets it, every
    later test in the same pytest process (same OS process, same os.environ)
    inherits the pollution and starts failing with no relation to what it's
    actually testing. Reset around every test, not just the ones that know
    to care."""
    saved_depth = os.environ.pop("PCP_AGENT_DEPTH", None)
    saved_max = os.environ.pop("PCP_MAX_AGENT_DEPTH", None)
    yield
    os.environ.pop("PCP_AGENT_DEPTH", None)
    os.environ.pop("PCP_MAX_AGENT_DEPTH", None)
    if saved_depth is not None:
        os.environ["PCP_AGENT_DEPTH"] = saved_depth
    if saved_max is not None:
        os.environ["PCP_MAX_AGENT_DEPTH"] = saved_max
