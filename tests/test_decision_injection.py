"""Decision-log feedback loop into pcp build's agent prompt (ECC instincts pattern)."""

from pcp import decision_log
from pcp.commands.build import _build_agent_prompt


def _pcp(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    return pcp_dir


def _record(pcp_dir, module=None, source=None, summary="use httpx not requests", category="library-choice"):
    fields = {"summary": summary, "category": category}
    if module:
        fields["module"] = module
    if source:
        fields["source"] = source
    decision_log.record(pcp_dir, **fields)


def test_module_decisions_selected_first(tmp_path):
    pcp_dir = _pcp(tmp_path)
    _record(pcp_dir, module="other", summary="other module thing")
    _record(pcp_dir, module="auth", summary="auth uses argon2")
    _record(pcp_dir, summary="project-wide: pin sqlalchemy 2.x")  # module-less
    selected = decision_log.select_relevant(pcp_dir, "auth")
    summaries = [r["summary"] for r in selected]
    assert "auth uses argon2" in summaries
    assert "project-wide: pin sqlalchemy 2.x" in summaries
    assert "other module thing" not in summaries  # other modules never injected
    assert summaries[0] == "auth uses argon2"     # module match outranks global


def test_build_source_counts_as_module_match(tmp_path):
    pcp_dir = _pcp(tmp_path)
    _record(pcp_dir, source="build:auth:A1", summary="A1 root cause: tz-naive datetimes")
    selected = decision_log.select_relevant(pcp_dir, "auth")
    assert len(selected) == 1


def test_limit_and_char_budget_respected(tmp_path):
    pcp_dir = _pcp(tmp_path)
    for i in range(10):
        _record(pcp_dir, module="auth", summary=f"decision {i}")
    assert len(decision_log.select_relevant(pcp_dir, "auth", limit=3)) == 3
    tight = decision_log.select_relevant(pcp_dir, "auth", limit=10, max_chars=60)
    assert len(tight) < 10


def test_prompt_includes_decisions_section(tmp_path):
    pcp_dir = _pcp(tmp_path)
    _record(pcp_dir, module="auth", summary="auth uses argon2")
    prompt = _build_agent_prompt(pcp_dir, "auth", {"id": "A1", "description": "login"}, {"name": "auth"})
    assert "Prior technical decisions" in prompt
    assert "auth uses argon2" in prompt


def test_prompt_omits_section_when_no_decisions(tmp_path):
    pcp_dir = _pcp(tmp_path)
    prompt = _build_agent_prompt(pcp_dir, "auth", {"id": "A1", "description": "login"}, {"name": "auth"})
    assert "Prior technical decisions" not in prompt


def test_injection_off_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("PCP_BUILD_INJECT_DECISIONS", "0")
    pcp_dir = _pcp(tmp_path)
    _record(pcp_dir, module="auth", summary="auth uses argon2")
    prompt = _build_agent_prompt(pcp_dir, "auth", {"id": "A1", "description": "login"}, {"name": "auth"})
    assert "auth uses argon2" not in prompt
