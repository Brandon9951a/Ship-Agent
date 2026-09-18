"""Model adapter tests use mocked transport and fake keys, never a live API."""

import json
from dataclasses import replace

import pytest

from core.llm_layer import (
    LLMCallResult, LLMClient, LLMConfig, LLMConfigError, LLMTransportError,
)


@pytest.fixture
def config():
    return LLMConfig(True, "deepseek", "https://api.deepseek.com", "deepseek-v4-pro",
                     "fake-test-secret")


def test_missing_config_disables_network(tmp_path):
    config = LLMConfig.from_env({}, tmp_path / "absent.env")
    def forbidden(*args):
        pytest.fail("disabled configuration must never send a request")
    result = LLMClient(config, forbidden).complete_text("测试")
    assert result.status == "unavailable"
    assert result.requires_fallback
    assert result.error_code == "disabled"


def test_config_secret_is_not_in_repr(config):
    assert config.api_key not in repr(config)


def test_dotenv_and_environment_precedence(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# test-only\nSHIP_LLM_ENABLED=false\nSHIP_LLM_MODEL='file-model'\n"
        "SHIP_LLM_API_KEY=\"fake-key-with-#\"\n", encoding="utf-8",
    )
    config = LLMConfig.from_env({"SHIP_LLM_MODEL": "env-model"}, path)
    assert config.model == "env-model"
    assert config.api_key == "fake-key-with-#"
    assert config.enabled is False


@pytest.mark.parametrize("line", ["secret-value-no-equals", "BAD NAME=fake", "KEY='unclosed-fake"])
def test_malformed_dotenv_does_not_echo_values(tmp_path, line):
    path = tmp_path / ".env"
    path.write_text(line, encoding="utf-8")
    with pytest.raises(LLMConfigError) as error:
        LLMConfig.from_env({}, path)
    assert "fake" not in str(error.value)
    assert "secret-value" not in str(error.value)


@pytest.mark.parametrize("field", ["provider", "base_url", "model", "api_key"])
def test_enabled_requires_complete_config(config, field):
    with pytest.raises(LLMConfigError):
        replace(config, **{field: ""}).check()


@pytest.mark.parametrize("url", [
    "http://api.deepseek.com",
    "https://user:fake-secret@api.deepseek.com",
    "https://api.deepseek.com?key=fake-secret",
    "https://api.deepseek.com#fake-secret",
    "https://api.deepseek.com/chat/completions",
    "https://[invalid",
    "https://api.deepseek.com:invalid",
])
def test_invalid_base_url_never_echoes_credentials(config, url):
    with pytest.raises(LLMConfigError) as error:
        replace(config, base_url=url).check()
    assert "fake-secret" not in str(error.value)


@pytest.mark.parametrize("timeout", [0, -1, 61, float("nan"), float("inf"), True, "15"])
def test_invalid_timeout_is_rejected(config, timeout):
    with pytest.raises(LLMConfigError):
        replace(config, timeout_s=timeout).check()


def test_success_builds_one_text_request(config):
    calls = []
    def transport(url, headers, body, timeout):
        calls.append((url, headers, json.loads(body), timeout))
        return {"model": "deepseek-v4-pro", "choices": [
            {"message": {"content": "OK"}, "finish_reason": "stop"}
        ]}
    result = LLMClient(config, transport).complete_text("连通性测试", max_tokens=32)
    assert result.status == "ok"
    assert result.text == "OK"
    assert result.requires_fallback is False
    assert len(calls) == 1
    url, headers, body, timeout = calls[0]
    assert url == "https://api.deepseek.com/chat/completions"
    assert headers["Authorization"] == "Bearer fake-test-secret"
    assert body["model"] == "deepseek-v4-pro"
    assert body["stream"] is False
    assert body["max_tokens"] == 32
    assert body["thinking"] == {"type": "disabled"}
    assert timeout == 15
    log = result.to_log_dict()
    assert "text" not in log
    assert "messages" not in log
    assert "api_key" not in log
    assert config.api_key not in json.dumps(log)


@pytest.mark.parametrize("code", [
    "timeout", "network_error", "http_401", "http_403", "http_429", "http_500",
    "invalid_response", "redirect_refused", "response_too_large",
])
def test_transport_failure_requests_fallback_without_retry(config, code):
    calls = []
    def transport(*args):
        calls.append(args)
        raise LLMTransportError(code)
    result = LLMClient(config, transport).complete_text("测试")
    assert result.status == "failed"
    assert result.requires_fallback
    assert result.error_code == code
    assert len(calls) == 1


def test_unexpected_exception_text_is_not_logged(config):
    def transport(*args):
        raise RuntimeError("response contained fake-test-secret")
    result = LLMClient(config, transport).complete_text("测试")
    assert result.error_code == "unexpected_error"
    assert config.api_key not in json.dumps(result.to_log_dict())


@pytest.mark.parametrize("payload", [
    {}, [], {"choices": []}, {"choices": [None]},
    {"choices": [{"message": {"content": None}}]},
    {"choices": [{"message": {"content": " "}}]},
    {"choices": [{"message": {"content": "unfinished"}, "finish_reason": "length"}]},
    {"choices": [{"message": {"content": "unfinished"}, "finish_reason": "tool_calls"}]},
])
def test_malformed_or_incomplete_response_never_becomes_success(config, payload):
    result = LLMClient(config, lambda *args: payload).complete_text("测试")
    assert result.status == "failed"
    assert result.text is None
    assert result.requires_fallback


@pytest.mark.parametrize("tokens", [0, -1, True, "32"])
def test_invalid_token_limit_makes_no_request(config, tokens):
    with pytest.raises(ValueError):
        LLMClient(config).complete_text("测试", max_tokens=tokens)


def test_empty_prompt_is_rejected(config):
    with pytest.raises(ValueError):
        LLMClient(config).complete_text(" ")


def test_log_omits_completion_content():
    result = LLMCallResult("ok", "deepseek", "deepseek-v4-pro", text="private-test-content")
    assert "private-test-content" not in json.dumps(result.to_log_dict())


def test_echoed_secret_is_redacted_from_result(config):
    client = LLMClient(config, lambda *args: {
        "model": "echo-fake-test-secret",
        "choices": [{"message": {"content": "echo-fake-test-secret"}, "finish_reason": "stop"}],
    })
    result = client.complete_text("test")
    assert config.api_key not in result.text
    assert config.api_key not in json.dumps(result.to_log_dict())
