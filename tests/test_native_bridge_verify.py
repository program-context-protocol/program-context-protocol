"""CTRL-042 (2026-08-09) -- auto-triggered cross-model adversarial review for
logic_tier:1 criteria touching a native/IPC boundary. Closes the gap named
directly in the Project W dogfood session: a self-testing build agent's own
deployment/execution assumptions can be silently wrong in a way nothing
inside its own loop can reveal (schannel's real incident). See
native_bridge_verify.py's module docstring for the full case."""

from unittest.mock import patch

from pcp.native_bridge_verify import (
    has_native_bridge_pattern, should_auto_verify, run_cross_model_review,
)


def test_has_native_bridge_pattern_true_for_wine_unix_call():
    assert has_native_bridge_pattern("WINE_UNIX_CALL(unix_send, &params)") is True


def test_has_native_bridge_pattern_true_for_dlopen():
    assert has_native_bridge_pattern('void *h = dlopen("lib.so", RTLD_NOW);') is True


def test_has_native_bridge_pattern_true_for_af_unix():
    assert has_native_bridge_pattern("int fd = socket(AF_UNIX, SOCK_STREAM, 0);") is True


def test_has_native_bridge_pattern_false_for_plain_code():
    assert has_native_bridge_pattern("def add(a, b):\n    return a + b\n") is False


def test_should_auto_verify_true_for_tier1_plus_native_bridge():
    criterion = {"id": "A001", "logic_tier": 1}
    content = "#pragma makedep unix\nstatic NTSTATUS unix_send_impl(void *args) { ... }"
    assert should_auto_verify(criterion, content) is True


def test_should_auto_verify_false_for_tier1_without_native_bridge():
    """Rung-1 criteria are the overwhelming majority of a normal project --
    this must not fire on plain deterministic code, only native-bridging."""
    criterion = {"id": "A001", "logic_tier": 1}
    content = "def compute_total(items):\n    return sum(i.price for i in items)\n"
    assert should_auto_verify(criterion, content) is False


def test_should_auto_verify_false_for_higher_tier_even_with_native_bridge():
    """The trigger is specifically tier-1-claims-deterministic-but-touches-
    a-native-boundary -- a rung-6 criterion already gets LLM scrutiny by
    design, this isn't meant to duplicate that."""
    criterion = {"id": "A001", "logic_tier": 6}
    content = "WINE_UNIX_CALL(unix_send, &params)"
    assert should_auto_verify(criterion, content) is False


def test_should_auto_verify_false_when_logic_tier_missing():
    criterion = {"id": "A001"}
    content = "WINE_UNIX_CALL(unix_send, &params)"
    assert should_auto_verify(criterion, content) is False


def test_run_cross_model_review_clean_when_confirmed_real():
    with patch("pcp.native_bridge_verify.llm.call_json_agy") as mock_agy:
        mock_agy.return_value = {"is_real": True, "confidence": 0.9, "red_flags": [], "reasoning": "looks fine"}
        flags = run_cross_model_review(None, "websocket", {"id": "A001", "description": "..."}, "diff text")
    assert flags == []


def test_run_cross_model_review_returns_red_flags_when_disputed():
    with patch("pcp.native_bridge_verify.llm.call_json_agy") as mock_agy:
        mock_agy.return_value = {
            "is_real": False, "confidence": 0.8,
            "red_flags": ["send/recv have no delay, response may arrive in multiple kernel deliveries"],
            "reasoning": "the client does a single read() call expecting the whole response at once",
        }
        flags = run_cross_model_review(None, "websocket", {"id": "A001", "description": "..."}, "diff text")
    assert flags == ["send/recv have no delay, response may arrive in multiple kernel deliveries"]


def test_run_cross_model_review_low_confidence_real_still_treated_as_clean_if_no_flags():
    """is_real=True below the confidence floor with no red_flags listed --
    nothing to report, empty list, not fabricated concern."""
    with patch("pcp.native_bridge_verify.llm.call_json_agy") as mock_agy:
        mock_agy.return_value = {"is_real": True, "confidence": 0.2, "red_flags": [], "reasoning": "uncertain"}
        flags = run_cross_model_review(None, "websocket", {"id": "A001"}, "diff text")
    assert flags == []


def test_run_cross_model_review_fails_open_on_agy_error():
    """agy not installed / timeout / any exception -- fails open (empty
    list), same asymmetry as _verify_block_findings: a missed real problem
    is worse than this check simply not running when the tool is absent."""
    with patch("pcp.native_bridge_verify.llm.call_json_agy", side_effect=RuntimeError("agy CLI not found")):
        flags = run_cross_model_review(None, "websocket", {"id": "A001"}, "diff text")
    assert flags == []


def test_run_cross_model_review_fails_open_on_malformed_response():
    with patch("pcp.native_bridge_verify.llm.call_json_agy") as mock_agy:
        mock_agy.return_value = "not a dict"
        flags = run_cross_model_review(None, "websocket", {"id": "A001"}, "diff text")
    assert flags == []
