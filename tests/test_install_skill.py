import re

from click.testing import CliRunner

from pcp.cli import cli
from pcp.commands.install_skill import BUNDLED_SKILL_PATH

# Names/details from the personal environment this skill was extracted from --
# must never appear in the bundled, publicly-distributed copy. Regression test
# for a real leak found and fixed: example content in the original skill named
# real private projects and real internal product architecture.
FORBIDDEN_PATTERNS = [
    r"postcar", r"win2mac", r"slicepay", r"the-ledger",
    r"tier25a", r"winemac-drv", r"combase-intercept", r"mounterd",
    r"NEFilterDataProvider", r"Apple Developer", r"codesigning",
    r"ganeshnallasivam",
    # 2026-07-31 addition: a separate leak found in this same file (line
    # ~603 at the time) named a dogfood project directly in a cost-measurement
    # example. The original list predates that project becoming the dominant
    # dogfood reference, which is exactly how it went uncaught.
    r"ontology-foundry", r"agentberg", r"atacamaMDM", r"signtool",
    r"geek-squad", r"LinkBox",
]


def test_bundled_skill_exists():
    assert BUNDLED_SKILL_PATH.exists()


def test_bundled_skill_has_no_private_references():
    content = BUNDLED_SKILL_PATH.read_text()
    for pattern in FORBIDDEN_PATTERNS:
        assert not re.search(pattern, content, re.IGNORECASE), f"found forbidden reference: {pattern}"


def test_bundled_skill_has_valid_frontmatter():
    content = BUNDLED_SKILL_PATH.read_text()
    assert content.startswith("---\n")
    assert "name: pcp" in content
    assert "Invoke with /pcp" in content


def test_install_skill_fresh_install(tmp_path, monkeypatch):
    dest = tmp_path / ".claude" / "skills" / "pcp" / "SKILL.md"
    runner = CliRunner()
    result = runner.invoke(cli, ["install-skill", "--path", str(dest)])
    assert result.exit_code == 0
    assert dest.exists()
    assert dest.read_text() == BUNDLED_SKILL_PATH.read_text()


def test_install_skill_refuses_overwrite_without_force(tmp_path):
    dest = tmp_path / "SKILL.md"
    dest.write_text("existing content")
    runner = CliRunner()
    result = runner.invoke(cli, ["install-skill", "--path", str(dest)])
    assert result.exit_code == 1
    assert "Already installed" in result.output
    assert dest.read_text() == "existing content"


def test_install_skill_force_overwrites(tmp_path):
    dest = tmp_path / "SKILL.md"
    dest.write_text("stale content")
    runner = CliRunner()
    result = runner.invoke(cli, ["install-skill", "--path", str(dest), "--force"])
    assert result.exit_code == 0
    assert dest.read_text() == BUNDLED_SKILL_PATH.read_text()
