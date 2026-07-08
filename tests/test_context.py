import json

from click.testing import CliRunner

from pcp.cli import cli
from pcp.commands.context import CLAUDE_MD_MARKER_START, CLAUDE_MD_MARKER_END


def _init_pcp(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("Build a calculator app.")
    (pcp_dir / "current_state.md").write_text("50% complete.")
    return pcp_dir


def test_context_stdout_markdown(tmp_path):
    _init_pcp(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["context", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "## Objective" in result.output
    assert "Build a calculator app." in result.output


def test_context_json(tmp_path):
    _init_pcp(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["context", "--path", str(tmp_path), "--json"])
    data = json.loads(result.output)
    assert data["objective"] == "Build a calculator app."


def test_context_missing_current_state_shows_placeholder(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("Objective text.")
    runner = CliRunner()
    result = runner.invoke(cli, ["context", "--path", str(tmp_path)])
    assert "Not generated yet" in result.output


def test_context_inject_creates_claude_md(tmp_path):
    _init_pcp(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["context", "--path", str(tmp_path), "--inject"])
    assert result.exit_code == 0
    claude_md = (tmp_path / "CLAUDE.md").read_text()
    assert CLAUDE_MD_MARKER_START in claude_md
    assert CLAUDE_MD_MARKER_END in claude_md
    assert "Build a calculator app." in claude_md


def test_context_inject_replaces_existing_block_only(tmp_path):
    _init_pcp(tmp_path)
    claude_md_path = tmp_path / "CLAUDE.md"
    claude_md_path.write_text(
        f"# My Project Rules\nDon't touch this.\n\n"
        f"{CLAUDE_MD_MARKER_START}\nold stale context\n{CLAUDE_MD_MARKER_END}\n"
    )
    runner = CliRunner()
    runner.invoke(cli, ["context", "--path", str(tmp_path), "--inject"])
    updated = claude_md_path.read_text()
    assert "Don't touch this." in updated
    assert "old stale context" not in updated
    assert "Build a calculator app." in updated


def test_context_inject_appends_when_no_existing_block(tmp_path):
    _init_pcp(tmp_path)
    claude_md_path = tmp_path / "CLAUDE.md"
    claude_md_path.write_text("# My Project Rules\n")
    runner = CliRunner()
    runner.invoke(cli, ["context", "--path", str(tmp_path), "--inject"])
    updated = claude_md_path.read_text()
    assert "# My Project Rules" in updated
    assert "Build a calculator app." in updated
