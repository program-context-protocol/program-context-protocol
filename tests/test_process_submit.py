import sys
from unittest.mock import patch, MagicMock

from click.testing import CliRunner

from pcp.cli import cli

# This repo's optional `process` extra (temporalio) isn't installed under the
# interpreter the test suite normally runs under -- only in .venv. These tests
# exercise process_submit.py's CLI wiring, not real temporalio behavior (that
# was verified live: a real `temporal server start-dev` + `pcp process-worker`
# + `pcp process-submit` round trip), so a stand-in module is enough to get
# past the `import temporalio` availability check.
_FAKE_TEMPORALIO = patch.dict(sys.modules, {"temporalio": MagicMock()})


def _init_pcp(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    return pcp_dir


def test_no_pcp_dir_exits_2(tmp_path):
    runner = CliRunner()
    result = runner.invoke(cli, ["process-submit", "add", "--path", str(tmp_path)])
    assert result.exit_code == 2


def test_successful_submission_prints_result(tmp_path):
    _init_pcp(tmp_path)
    with _FAKE_TEMPORALIO, patch("pcp.commands.process_submit._submit") as mock_submit:
        async def fake_submit(*a, **kw):
            return {"module": "add", "stdout_tail": "done"}
        mock_submit.side_effect = fake_submit
        runner = CliRunner()
        result = runner.invoke(cli, ["process-submit", "add", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "done" in result.output
    mock_submit.assert_called_once_with("localhost:7233", str(tmp_path), "add")


def test_workflow_failure_exits_1(tmp_path):
    _init_pcp(tmp_path)
    with _FAKE_TEMPORALIO, patch("pcp.commands.process_submit._submit") as mock_submit:
        async def fake_submit(*a, **kw):
            raise RuntimeError("activity failed: exit 1")
        mock_submit.side_effect = fake_submit
        runner = CliRunner()
        result = runner.invoke(cli, ["process-submit", "add", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "Workflow failed" in result.output


def test_custom_target_host_passed_through(tmp_path):
    _init_pcp(tmp_path)
    with _FAKE_TEMPORALIO, patch("pcp.commands.process_submit._submit") as mock_submit:
        async def fake_submit(*a, **kw):
            return {"module": "add"}
        mock_submit.side_effect = fake_submit
        runner = CliRunner()
        runner.invoke(cli, ["process-submit", "add", "--path", str(tmp_path), "--target-host", "example.com:7233"])
    mock_submit.assert_called_once_with("example.com:7233", str(tmp_path), "add")
