"""pcp inspiration-art -- human-authorized write path for
.pcp/strategy/inspiration_art.md. Same propose/diff/approve/write mechanic
as `pcp amend`/`pcp correct-objective` (spec_write.py), see test_amend.py
for the pattern this mirrors.
"""

from unittest.mock import patch

from click.testing import CliRunner

from pcp.cli import cli


def _project(tmp_path, inspiration_art_content=None):
    pcp = tmp_path / ".pcp"
    (pcp / "strategy" / "modules").mkdir(parents=True)
    (pcp / "objective.md").write_text("# Objective\n\nMigrate Windows apps to macOS.\n")
    if inspiration_art_content is not None:
        (pcp / "strategy" / "inspiration_art.md").write_text(inspiration_art_content)
    return pcp


def test_inspiration_art_requires_description_or_gap(tmp_path):
    _project(tmp_path)
    result = CliRunner().invoke(cli, ["inspiration-art", "--path", str(tmp_path)])
    assert result.exit_code == 2
    assert "description" in result.output.lower()


def test_inspiration_art_writes_on_approval(tmp_path):
    pcp = _project(tmp_path)
    proposal = {
        "inspiration_art_md": "# Inspiration Art\n\n## Compatibility Layer\n\nTypical modules: ...\n",
        "summary": "researched compat-layer category",
    }
    with patch("pcp.llm.client.call_json", return_value=proposal):
        result = CliRunner().invoke(
            cli, ["inspiration-art", "A Windows-app compatibility tool for macOS", "--path", str(tmp_path)],
            input="y\n",
        )
    assert result.exit_code == 0, result.output
    assert "Compatibility Layer" in (pcp / "strategy" / "inspiration_art.md").read_text()


def test_inspiration_art_declined_leaves_file_unchanged(tmp_path):
    pcp = _project(tmp_path, inspiration_art_content="# Inspiration Art\n\n[None researched yet.]\n")
    proposal = {
        "inspiration_art_md": "# Inspiration Art\n\n## Migration Tool\n\nTypical modules: ...\n",
        "summary": "researched migration category",
    }
    with patch("pcp.llm.client.call_json", return_value=proposal):
        result = CliRunner().invoke(
            cli, ["inspiration-art", "A Windows-to-Mac migration tool", "--path", str(tmp_path)],
            input="n\n",
        )
    assert result.exit_code == 0
    assert (pcp / "strategy" / "inspiration_art.md").read_text() == "# Inspiration Art\n\n[None researched yet.]\n"
    assert "Migration Tool" not in (pcp / "strategy" / "inspiration_art.md").read_text()


def test_inspiration_art_gap_mode_uses_gap_intent(tmp_path):
    pcp = _project(tmp_path)
    proposal = {
        "inspiration_art_md": "# Inspiration Art\n\n## Release Orchestration\n\nCovers: rollout gates\n",
        "summary": "researched category for the gap",
    }
    with patch("pcp.llm.client.call_json", return_value=proposal) as mock_call_json:
        result = CliRunner().invoke(
            cli, ["inspiration-art", "--gap", "canary rollout with auto-rollback", "--path", str(tmp_path)],
            input="y\n",
        )
    assert result.exit_code == 0, result.output
    user_prompt = mock_call_json.call_args[0][1]
    assert "canary rollout with auto-rollback" in user_prompt
    assert "Release Orchestration" in (pcp / "strategy" / "inspiration_art.md").read_text()


def test_inspiration_art_records_a_decision_log_entry(tmp_path):
    pcp = _project(tmp_path)
    proposal = {
        "inspiration_art_md": "# Inspiration Art\n\n## Compatibility Layer\n\n...\n",
        "summary": "researched compat-layer category",
    }
    with patch("pcp.llm.client.call_json", return_value=proposal):
        CliRunner().invoke(
            cli, ["inspiration-art", "A Windows-app compatibility tool for macOS", "--path", str(tmp_path)],
            input="y\n",
        )
    log = (pcp / "decision_log.jsonl").read_text()
    assert "inspiration_art.md amended" in log


def test_inspiration_art_no_change_is_not_an_error(tmp_path):
    existing = "# Inspiration Art\n\n## Compatibility Layer\n\n...\n"
    _project(tmp_path, inspiration_art_content=existing)
    proposal = {"inspiration_art_md": existing, "summary": "nothing new"}
    with patch("pcp.llm.client.call_json", return_value=proposal):
        result = CliRunner().invoke(
            cli, ["inspiration-art", "A Windows-app compatibility tool for macOS", "--path", str(tmp_path)],
        )
    assert result.exit_code == 0
    assert "No changes" in result.output


def test_inspiration_art_reads_objective_into_prompt(tmp_path):
    _project(tmp_path)
    proposal = {"inspiration_art_md": "# Inspiration Art\n\n## X\n\n...\n", "summary": "s"}
    with patch("pcp.llm.client.call_json", return_value=proposal) as mock_call_json:
        CliRunner().invoke(
            cli, ["inspiration-art", "Something", "--path", str(tmp_path)], input="y\n",
        )
    user_prompt = mock_call_json.call_args[0][1]
    assert "Migrate Windows apps to macOS" in user_prompt


def test_inspiration_art_system_prompt_constrains_screen_archetypes():
    from pcp.commands.inspiration_art import SYSTEM_PROMPT

    for archetype in ("dashboard", "list_table", "wizard", "canvas_editor"):
        assert archetype in SYSTEM_PROMPT
    assert "training-data recall, unverified" in SYSTEM_PROMPT
