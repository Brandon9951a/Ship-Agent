"""Configurable text-only model adapter; engineering numbers stay in tools."""

import argparse
import json
import os
import re
import socket
import time
from dataclasses import dataclass, field
from http.client import HTTPException
from pathlib import Path
from typing import Any, Callable, Literal, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_RESPONSE_BYTES = 1_048_576
Transport = Callable[[str, dict[str, str], bytes, float], dict[str, Any]]


class LLMConfigError(ValueError):
    """Messages identify variable names only, never their secret values."""


class LLMTransportError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        raise LLMConfigError("Cannot read local .env as UTF-8") from None
    for line_number, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise LLMConfigError(f".env line {line_number}: expected KEY=VALUE")
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise LLMConfigError(f".env line {line_number}: invalid variable name")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise LLMConfigError(f".env line {line_number}: unmatched quote")
            value = value[1:-1]
        values[name] = value
    return values


@dataclass(frozen=True)
class LLMConfig:
    enabled: bool = False
    provider: str = ""
    base_url: str = ""
    model: str = ""
    api_key: str = field(default="", repr=False)
    timeout_s: float = 15.0

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, env_file: Path | None = None,
    ) -> "LLMConfig":
        path = env_file if env_file is not None else Path(__file__).resolve().parents[1] / ".env"
        values = _read_dotenv(path)
        values.update(os.environ if env is None else env)
        enabled = values.get("SHIP_LLM_ENABLED", "false").strip().lower()
        if enabled not in ("true", "false"):
            raise LLMConfigError("SHIP_LLM_ENABLED must be true or false")
        try:
            timeout = float(values.get("SHIP_LLM_TIMEOUT_S", "15"))
        except ValueError:
            raise LLMConfigError("SHIP_LLM_TIMEOUT_S must be a number") from None
        config = cls(
            enabled=enabled == "true",
            provider=values.get("SHIP_LLM_PROVIDER", "").strip(),
            base_url=values.get("SHIP_LLM_BASE_URL", "").strip().rstrip("/"),
            model=values.get("SHIP_LLM_MODEL", "").strip(),
            api_key=values.get("SHIP_LLM_API_KEY", "").strip(),
            timeout_s=timeout,
        )
        config.check()
        return config

    def check(self) -> None:
        if not isinstance(self.enabled, bool):
            raise LLMConfigError("SHIP_LLM_ENABLED must be true or false")
        if isinstance(self.timeout_s, bool) or not isinstance(self.timeout_s, (int, float)):
            raise LLMConfigError("SHIP_LLM_TIMEOUT_S must be a number")
        if not 0 < self.timeout_s <= 60:
            raise LLMConfigError("SHIP_LLM_TIMEOUT_S must be greater than 0 and at most 60")
        for name, value in (
            ("SHIP_LLM_PROVIDER", self.provider), ("SHIP_LLM_BASE_URL", self.base_url),
            ("SHIP_LLM_MODEL", self.model), ("SHIP_LLM_API_KEY", self.api_key),
        ):
            if not isinstance(value, str) or any(char in value for char in ("\r", "\n")):
                raise LLMConfigError(f"{name} must be single-line text")
            if self.enabled and (not value.strip() or value.strip().lower() in (
                "replace_me", "your_api_key", "your_model", "your_provider",
            )):
                raise LLMConfigError(f"{name} is not configured")
        if self.base_url:
            try:
                parsed = urlsplit(self.base_url)
                parsed.port
            except ValueError:
                raise LLMConfigError("SHIP_LLM_BASE_URL is not a valid URL") from None
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise LLMConfigError("SHIP_LLM_BASE_URL must be HTTPS without embedded credentials")
            if parsed.query or parsed.fragment or parsed.path.endswith("/chat/completions"):
                raise LLMConfigError("SHIP_LLM_BASE_URL must be an API base URL without query or fragment")


@dataclass(frozen=True)
class LLMCallResult:
    status: Literal["ok", "unavailable", "failed"]
    provider: str
    requested_model: str
    text: str | None = field(default=None, repr=False)
    response_model: str | None = None
    error_code: str | None = None
    latency_ms: int | None = None

    @property
    def requires_fallback(self) -> bool:
        return self.status != "ok"

    def to_log_dict(self) -> dict[str, Any]:
        """No key, prompt, completion, header, or raw service error is logged."""
        return {
            "status": self.status, "provider": self.provider,
            "requested_model": self.requested_model, "response_model": self.response_model,
            "requires_fallback": self.requires_fallback, "error_code": self.error_code,
            "latency_ms": self.latency_ms,
        }


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_transport(url: str, headers: dict[str, str], body: bytes, timeout: float) -> dict[str, Any]:
    request = Request(url, data=body, headers=headers, method="POST")
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        status = error.code
        error.close()
        raise LLMTransportError(
            "redirect_refused" if 300 <= status < 400 else f"http_{status}"
        ) from None
    except (TimeoutError, socket.timeout):
        raise LLMTransportError("timeout") from None
    except URLError as error:
        code = "timeout" if isinstance(error.reason, (TimeoutError, socket.timeout)) else "network_error"
        raise LLMTransportError(code) from None
    except (OSError, HTTPException):
        raise LLMTransportError("network_error") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise LLMTransportError("response_too_large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise LLMTransportError("invalid_response") from None
    if not isinstance(payload, dict):
        raise LLMTransportError("invalid_response")
    return payload


class LLMClient:
    def __init__(self, config: LLMConfig, transport: Transport | None = None):
        config.check()
        self.config = config
        self.transport = transport if transport is not None else _http_transport

    def _result(self, status, **kwargs) -> LLMCallResult:
        def redact(value):
            if isinstance(value, str) and self.config.api_key:
                return value.replace(self.config.api_key, "[REDACTED]")
            return value
        return LLMCallResult(
            status, redact(self.config.provider), redact(self.config.model),
            **{key: redact(value) for key, value in kwargs.items()},
        )

    def complete_text(
        self, user_text: str, *,
        system_text: str = "你负责文本理解与说明。工程数值必须来自计算工具；不要编造数值。",
        max_tokens: int = 256,
    ) -> LLMCallResult:
        if not isinstance(user_text, str) or not user_text.strip():
            raise ValueError("user_text must be non-empty text")
        if not isinstance(system_text, str) or not system_text.strip():
            raise ValueError("system_text must be non-empty text")
        if type(max_tokens) is not int or max_tokens <= 0:
            raise ValueError("max_tokens must be a positive integer")
        config = self.config
        if not config.enabled:
            return self._result("unavailable", error_code="disabled")
        request_body = {
            "model": config.model, "messages": [
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_text},
            ], "stream": False, "max_tokens": max_tokens,
        }
        if config.provider.lower() == "deepseek":
            # A selected non-thinking mode for this week's text workflow.
            request_body["thinking"] = {"type": "disabled"}
        body = json.dumps(request_body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {config.api_key}"}
        started = time.monotonic()
        try:
            payload = self.transport(
                config.base_url.rstrip("/") + "/chat/completions", headers, body, config.timeout_s,
            )
            if not isinstance(payload, dict):
                raise LLMTransportError("invalid_response")
            choices = payload.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise LLMTransportError("invalid_response")
            message = choices[0].get("message")
            text = message.get("content") if isinstance(message, dict) else None
            if not isinstance(text, str) or not text.strip():
                raise LLMTransportError("empty_response")
            # A truncated output is not usable as a complete semantic response.
            if choices[0].get("finish_reason") not in (None, "stop"):
                raise LLMTransportError("incomplete_response")
            response_model = payload.get("model")
            if not isinstance(response_model, str):
                response_model = None
            return self._result(
                "ok", text=text, response_model=response_model,
                latency_ms=round((time.monotonic() - started) * 1000),
            )
        except LLMTransportError as error:
            return self._result(
                "failed", error_code=error.code,
                latency_ms=round((time.monotonic() - started) * 1000),
            )
        except Exception:
            # Do not expose arbitrary transport/service exception strings.
            return self._result(
                "failed", error_code="unexpected_error",
                latency_ms=round((time.monotonic() - started) * 1000),
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="检查模型配置或执行非敏感连接测试")
    parser.add_argument("--smoke-test", action="store_true", help="发送一次短文本请求")
    args = parser.parse_args()
    try:
        config = LLMConfig.from_env()
    except LLMConfigError as error:
        print(json.dumps({"status": "config_error", "message": str(error)}, ensure_ascii=False))
        return 2
    if not args.smoke_test:
        print(json.dumps({
            "status": "configured" if config.enabled else "disabled",
            "provider": config.provider, "model": config.model,
            "note": "仅检查配置，尚未发出网络请求。",
        }, ensure_ascii=False))
        return 0
    result = LLMClient(config).complete_text(
        "请只回复大写字母 OK。",
        system_text="这是 API 连通性测试，不涉及任何业务数据。",
        max_tokens=32,
    )
    print(json.dumps(result.to_log_dict(), ensure_ascii=False))
    return 0 if result.status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
