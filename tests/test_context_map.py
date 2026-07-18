"""Deterministic context routing (context_map.py, CTRL-021)."""

import yaml
from click.testing import CliRunner

from pcp import context_map
from pcp.cli import cli
from pcp.commands.build import _build_agent_prompt


def _proj(tmp_path, with_slice=False):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("obj")
    (pcp_dir / "current_state.md").write_text("global state")
    if with_slice:
        d = pcp_dir / "strategy" / "modules" / "auth" / "docs"
        d.mkdir(parents=True)
        (d / "built.md").write_text("auth slice")
    return pcp_dir


def test_defaults_used_when_no_map_file(tmp_path):
    pcp_dir = _proj(tmp_path)
    assert context_map.load(pcp_dir) == context_map.DEFAULT_ROUTES


def test_module_state_routes_to_slice_when_present(tmp_path):
    pcp_dir = _proj(tmp_path, with_slice=True)
    files = context_map.resolve(pcp_dir, "module_state", module="auth")
    assert files == [".pcp/strategy/modules/auth/docs/built.md"]


def test_module_state_falls_back_to_global_state(tmp_path):
    pcp_dir = _proj(tmp_path, with_slice=False)
    files = context_map.resolve(pcp_dir, "module_state", module="auth")
    assert files == [".pcp/current_state.md"]


def test_prompt_routes_to_module_slice_not_global_state(tmp_path):
    pcp_dir = _proj(tmp_path, with_slice=True)
    prompt = _build_agent_prompt(pcp_dir, "auth", {"id": "A1", "description": "x"}, {"name": "auth"})
    assert "strategy/modules/auth/docs/built.md" in prompt
    assert "current_state.md" not in prompt  # slice replaces global file
    assert "Read ONLY these" in prompt


def test_prompt_falls_back_to_global_state_without_slice(tmp_path):
    pcp_dir = _proj(tmp_path, with_slice=False)
    prompt = _build_agent_prompt(pcp_dir, "auth", {"id": "A1", "description": "x"}, {"name": "auth"})
    assert "current_state.md" in prompt


def test_validate_flags_route_resolving_to_nothing(tmp_path):
    pcp_dir = _proj(tmp_path)
    (pcp_dir / "context_map.yaml").write_text(yaml.dump({
        "version": "1.0",
        "routes": {"always": {"files": [".pcp/ghost.md"]}},
    }))
    findings = context_map.validate(pcp_dir)
    assert len(findings) == 1
    assert "zero existing files" in findings[0]


def test_validate_accepts_fallback_satisfying_route(tmp_path):
    pcp_dir = _proj(tmp_path)
    (pcp_dir / "context_map.yaml").write_text(yaml.dump({
        "version": "1.0",
        "routes": {"module_state": {"files": [".pcp/strategy/modules/{module}/docs/built.md"],
                                     "fallback": [".pcp/current_state.md"]}},
    }))
    assert context_map.validate(pcp_dir) == []


def test_corrupt_map_falls_back_to_defaults(tmp_path):
    pcp_dir = _proj(tmp_path)
    (pcp_dir / "context_map.yaml").write_text(": not yaml [")
    assert context_map.load(pcp_dir) == context_map.DEFAULT_ROUTES


def test_init_scaffolds_context_map(tmp_path):
    CliRunner().invoke(cli, ["init", "--path", str(tmp_path)])
    cm = tmp_path / ".pcp" / "context_map.yaml"
    assert cm.exists()
    data = yaml.safe_load(cm.read_text())
    assert set(data["routes"]) >= {"always", "module_state", "ui_facing", "logic_tier_declared"}
