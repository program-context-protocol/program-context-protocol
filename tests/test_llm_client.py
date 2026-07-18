import base64
import json
from unittest.mock import patch, MagicMock

from pcp.llm import client as llm


def _envelope(result="{}"):
    return json.dumps({
        "is_error": False, "result": result, "session_id": "s1",
        "usage": {"input_tokens": 1, "output_tokens": 1}, "total_cost_usd": 0.0,
        "duration_ms": 1,
    })


def _stream_envelope(result="{}"):
    """One `type: result` stream-json event -- what call_with_images() scans
    stdout for. Other event types (assistant/system/etc.) are ignored, so a
    single line is sufficient to exercise the parsing."""
    return json.dumps({
        "type": "result", "is_error": False, "result": result, "session_id": "s1",
        "usage": {"input_tokens": 1, "output_tokens": 1}, "total_cost_usd": 0.0,
        "duration_ms": 1,
    })


_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def test_call_passes_cwd_derived_from_pcp_dir(tmp_path):
    """Regression: call() never passed cwd to subprocess.run, so the `claude`
    subprocess always ran in whatever the CALLING PROCESS's actual OS cwd
    happened to be -- not necessarily the target project. Found via a real
    contamination incident: a test process's own cwd (this repo) leaked
    into a spawned agent invocation that should have run against an
    isolated test project instead."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    with patch("pcp.llm.client.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=_envelope())
        llm.call("system", "user", pcp_dir=pcp_dir)
    assert mock_run.call_args.kwargs["cwd"] == pcp_dir.parent


def test_call_cwd_none_when_no_pcp_dir_given():
    with patch("pcp.llm.client.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=_envelope())
        llm.call("system", "user")
    assert mock_run.call_args.kwargs["cwd"] is None


def test_call_json_also_passes_cwd(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    with patch("pcp.llm.client.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=_envelope('{"a": 1}'))
        result = llm.call_json("system", "user", pcp_dir=pcp_dir)
    assert mock_run.call_args.kwargs["cwd"] == pcp_dir.parent
    assert result == {"a": 1}


def test_call_raises_on_missing_claude_binary(tmp_path):
    with patch("pcp.llm.client.subprocess.run", side_effect=FileNotFoundError):
        try:
            llm.call("system", "user")
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "claude CLI not found" in str(e)


def test_call_logs_usage_to_correct_pcp_dir(tmp_path):
    """The cwd fix and the token-ledger logging both derive from the same
    pcp_dir -- confirms _log_usage still writes to the real target project,
    not wherever cwd ended up pointing."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    with patch("pcp.llm.client.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=_envelope())
        llm.call("system", "user", model="haiku", pcp_dir=pcp_dir, command="test-call")
    ledger = (pcp_dir / "token_ledger.yaml").read_text()
    assert "test-call" in ledger


# ── call_with_images / call_with_image ──

def test_call_with_images_uses_stream_json_flags(tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(_PNG_BYTES)
    with patch("pcp.llm.client.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=_stream_envelope("looks fine"))
        text = llm.call_with_images("system", "user", [img])
    cmd = mock_run.call_args.args[0]
    assert "--input-format" in cmd and "stream-json" in cmd
    assert "--output-format" in cmd and "stream-json" in cmd
    assert "--verbose" in cmd
    assert text == "looks fine"


def test_call_with_images_sends_base64_image_content_block(tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(_PNG_BYTES)
    with patch("pcp.llm.client.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=_stream_envelope())
        llm.call_with_images("system", "user", [img])
    sent = json.loads(mock_run.call_args.kwargs["input"])
    blocks = sent["message"]["content"]
    image_blocks = [b for b in blocks if b["type"] == "image"]
    assert len(image_blocks) == 1
    assert image_blocks[0]["source"]["media_type"] == "image/png"
    assert base64.b64decode(image_blocks[0]["source"]["data"]) == _PNG_BYTES


def test_call_with_images_sends_two_image_blocks_for_two_paths(tmp_path):
    img1 = tmp_path / "a.png"
    img2 = tmp_path / "b.png"
    img1.write_bytes(_PNG_BYTES)
    img2.write_bytes(_PNG_BYTES)
    with patch("pcp.llm.client.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=_stream_envelope())
        llm.call_with_images("system", "user", [img1, img2])
    sent = json.loads(mock_run.call_args.kwargs["input"])
    image_blocks = [b for b in sent["message"]["content"] if b["type"] == "image"]
    assert len(image_blocks) == 2


def test_call_with_image_single_image_wrapper(tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(_PNG_BYTES)
    with patch("pcp.llm.client.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=_stream_envelope("ok"))
        text = llm.call_with_image("system", "user", img)
    assert text == "ok"


def test_call_with_images_raises_when_no_result_event(tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(_PNG_BYTES)
    non_result_line = json.dumps({"type": "system", "subtype": "init"})
    with patch("pcp.llm.client.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=non_result_line)
        try:
            llm.call_with_images("system", "user", [img])
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "no result event" in str(e)


def test_call_json_with_images_parses_json_result(tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(_PNG_BYTES)
    with patch("pcp.llm.client.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=_stream_envelope('{"overall_passed": true}'))
        parsed = llm.call_json_with_images("system", "user", [img])
    assert parsed == {"overall_passed": True}


def test_call_with_images_logs_usage(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    img = tmp_path / "shot.png"
    img.write_bytes(_PNG_BYTES)
    with patch("pcp.llm.client.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=_stream_envelope())
        llm.call_with_images("system", "user", [img], pcp_dir=pcp_dir, command="test-image-call")
    ledger = (pcp_dir / "token_ledger.yaml").read_text()
    assert "test-image-call" in ledger
