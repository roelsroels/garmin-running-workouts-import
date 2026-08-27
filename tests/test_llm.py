import io
import json
import urllib.error
from unittest.mock import MagicMock

import pytest

from garminworkouts.llm import ClaudeAdvisor, LLMConfig, OpenAICompatibleAdvisor, _NoRedirects, create_advisor
from garminworkouts.state import AppState
from garminworkouts.web_secrets import SessionAPIKeys


def _transport(monkeypatch, result=None, error=None, raw=None):
    opener = MagicMock()
    if error:
        opener.open.side_effect = error
    else:
        opener.open.return_value.__enter__.return_value.read.return_value = (
            raw if raw is not None else json.dumps(result).encode()
        )
    monkeypatch.setattr("garminworkouts.llm.urllib.request.build_opener", lambda *args: opener)
    return opener


def test_claude_native_request_and_text_blocks(monkeypatch):
    opener = _transport(
        monkeypatch,
        {
            "content": [
                {"type": "text", "text": "Evidence."},
                {"type": "thinking", "thinking": "internal"},
                {"type": "text", "text": "Uncertainty."},
            ],
            "stop_reason": "end_turn",
        },
    )
    config = LLMConfig(provider="anthropic", base_url="https://other.invalid/v1")
    advisor = create_advisor(config, "test-claude-key")

    assert isinstance(advisor, ClaudeAdvisor)
    assert advisor.explain({"confidence": "moderate"}) == "Evidence.\n\nUncertainty."
    request = opener.open.call_args.args[0]
    payload = json.loads(request.data)
    assert request.full_url == "https://api.anthropic.com/v1/messages"
    assert request.get_header("X-api-key") == "test-claude-key"
    assert request.get_header("Anthropic-version") == "2023-06-01"
    assert request.get_header("Authorization") is None
    assert payload["max_tokens"] == 1500
    assert payload["system"]
    assert [message["role"] for message in payload["messages"]] == ["user"]
    assert "moderate" in payload["messages"][0]["content"]
    assert "test-claude-key" not in request.data.decode()
    assert opener.open.call_args.kwargs["timeout"] == 30


def test_openai_compatible_request_is_preserved(monkeypatch):
    opener = _transport(monkeypatch, {"choices": [{"message": {"content": " Explanation "}}]})
    advisor = create_advisor(LLMConfig(provider="openai-compatible", model="custom-model"), "test-key")

    assert isinstance(advisor, OpenAICompatibleAdvisor)
    assert advisor.explain({}) == "Explanation"
    request = opener.open.call_args.args[0]
    assert request.full_url == "https://api.openai.com/v1/chat/completions"
    assert request.get_header("Authorization") == "Bearer test-key"
    assert json.loads(request.data)["messages"][0]["role"] == "system"


@pytest.mark.parametrize("code", [301, 307, 400, 401, 403, 404, 429, 500, 529])
def test_http_errors_hide_response_body_and_do_not_retry(monkeypatch, code):
    secret = "never-display-this-key"
    error = urllib.error.HTTPError(
        "https://api.anthropic.com/v1/messages", code, "error", {}, io.BytesIO(secret.encode())
    )
    opener = _transport(monkeypatch, error=error)

    with pytest.raises(RuntimeError, match=f"HTTP {code}") as exc:
        create_advisor(LLMConfig(provider="anthropic"), secret).explain({})

    assert secret not in str(exc.value)
    assert opener.open.call_count == 1


@pytest.mark.parametrize("error", [TimeoutError(), urllib.error.URLError("secret-key"), OSError("secret-key")])
def test_network_failures_are_sanitized(monkeypatch, error):
    _transport(monkeypatch, error=error)
    with pytest.raises(RuntimeError, match="connection failed") as exc:
        create_advisor(LLMConfig(provider="anthropic"), "secret-key").explain({})
    assert "secret-key" not in str(exc.value)


@pytest.mark.parametrize("result", [{}, None, {"content": []}, {"content": [{"type": "text", "text": None}]}])
def test_claude_rejects_empty_or_malformed_content(monkeypatch, result):
    _transport(monkeypatch, result=result)
    with pytest.raises(RuntimeError, match="unsupported or empty"):
        create_advisor(LLMConfig(provider="anthropic"), "test-key").explain({})


def test_invalid_json_and_truncated_responses_are_not_accepted(monkeypatch):
    _transport(monkeypatch, raw=b"not-json")
    with pytest.raises(RuntimeError, match="invalid JSON"):
        create_advisor(LLMConfig(provider="anthropic"), "test-key").explain({})
    _transport(monkeypatch, {"content": [{"type": "text", "text": "partial"}], "stop_reason": "max_tokens"})
    with pytest.raises(RuntimeError, match="output limit"):
        create_advisor(LLMConfig(provider="anthropic"), "test-key").explain({})


def test_response_cannot_echo_key_into_saved_explanation(monkeypatch):
    _transport(monkeypatch, {"content": [{"type": "text", "text": "Echo: secret-key"}]})
    assert "secret-key" not in create_advisor(LLMConfig(provider="anthropic"), "secret-key").explain({})


def test_redirects_are_never_followed():
    assert _NoRedirects().redirect_request(None, None, 302, "", {}, "https://other.invalid") is None


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.invalid",
        "file:///etc/passwd",
        "https://user:pass@host/v1",
        "https://host/v1?key=secret",
        "https://host/v1#fragment",
    ],
)
def test_unsafe_custom_endpoints_are_rejected(url):
    with pytest.raises(ValueError, match="base URL"):
        LLMConfig(provider="openai-compatible", model="custom", base_url=url).validate()


def test_config_defaults_validation_and_existing_settings(tmp_path):
    with AppState(tmp_path) as state:
        assert not LLMConfig.from_state(state).enabled
        with pytest.raises(ValueError, match="Unsupported"):
            LLMConfig(provider="unknown")
        with pytest.raises(ValueError, match="model name"):
            LLMConfig(provider="openai-compatible").validate()
        with pytest.raises(ValueError, match="variable name"):
            LLMConfig(provider="anthropic", api_key_env="not a name").validate()
        config = LLMConfig(provider="anthropic")
        config.save(state)
        assert LLMConfig.from_state(state) == config
        assert config.api_key_env == "ANTHROPIC_API_KEY"
        for key in ("", "with\nwhitespace"):
            with pytest.raises(ValueError):
                create_advisor(config, key)
        legacy = LLMConfig(provider="openai-compatible", model="my-model", base_url="http://localhost:1234/v1")
        legacy.save(state)
        assert LLMConfig.from_state(state) == legacy


def test_session_keys_expire_are_config_bound_and_bounded(monkeypatch):
    clock = [100]
    monkeypatch.setattr("garminworkouts.web_secrets.time.monotonic", lambda: clock[0])
    vault = SessionAPIKeys(ttl=10, capacity=2)
    config = LLMConfig(provider="anthropic")
    first = vault.put(config, "first-key")
    assert vault.get(first, config) == "first-key"
    assert vault.get("another-browser", config) is None
    second = vault.put(config, "second-key")
    third = vault.put(config, "third-key")
    assert vault.get(first, config) is None
    assert vault.get(second, LLMConfig(provider="openai-compatible", model="other")) is None
    assert vault.get(second, config) is None
    clock[0] = 110
    assert vault.get(third, config) is None
    token = vault.put(config, "fourth-key")
    vault.remove(token)
    assert vault.get(token, config) is None
