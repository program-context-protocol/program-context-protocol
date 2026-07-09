import subprocess

from click.testing import CliRunner

from pcp.cli import cli


def _init_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    (tmp_path / ".pcp").mkdir()
    return tmp_path


def _commit(tmp_path, rel_path, content, msg="commit"):
    p = tmp_path / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    subprocess.run(["git", "add", rel_path], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=tmp_path, check=True)


def test_verify_syntax_fix_safe_for_pure_quoting_change(tmp_path):
    _init_repo(tmp_path)
    _commit(tmp_path, "spec.yaml", "criteria:\n  - id: A001\n    description: some text: with a colon\n")
    (tmp_path / "spec.yaml").write_text('criteria:\n  - id: A001\n    description: "some text: with a colon"\n')

    runner = CliRunner()
    result = runner.invoke(cli, ["verify-syntax-fix", "spec.yaml", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "SAFE" in result.output


def test_verify_syntax_fix_unsafe_for_real_content_change(tmp_path):
    _init_repo(tmp_path)
    _commit(tmp_path, "spec.yaml", 'criteria:\n  - id: A001\n    description: "original"\n')
    (tmp_path / "spec.yaml").write_text('criteria:\n  - id: A001\n    description: "a different requirement"\n')

    runner = CliRunner()
    result = runner.invoke(cli, ["verify-syntax-fix", "spec.yaml", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "UNSAFE" in result.output


def test_verify_syntax_fix_unsafe_when_still_broken(tmp_path):
    _init_repo(tmp_path)
    _commit(tmp_path, "spec.yaml", "criteria:\n  - id: A001\n    description: broken: text\n")
    (tmp_path / "spec.yaml").write_text("criteria:\n  - id: A001\n    description: still: broken\n")

    runner = CliRunner()
    result = runner.invoke(cli, ["verify-syntax-fix", "spec.yaml", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "UNSAFE" in result.output
    assert "does not parse" in result.output


def test_verify_syntax_fix_unsafe_for_brand_new_file(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "new_spec.yaml").write_text('criteria:\n  - id: A001\n    description: "new"\n')

    runner = CliRunner()
    result = runner.invoke(cli, ["verify-syntax-fix", "new_spec.yaml", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "UNSAFE" in result.output
    assert "new file" in result.output


def test_verify_syntax_fix_no_pcp_dir_exits_2(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "spec.yaml").write_text("a: b\n")
    runner = CliRunner()
    result = runner.invoke(cli, ["verify-syntax-fix", "spec.yaml", "--path", str(tmp_path)])
    assert result.exit_code == 2
