"""Secure server-side bridge for iFLYTEK streaming speech recognition."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import urlencode, urlparse

from core.llm_layer import _read_dotenv


XFYUN_IAT_URL = "wss://iat-api.xfyun.cn/v2/iat"


class SpeechServiceError(RuntimeError):
    """User-safe speech service failure."""


@dataclass(frozen=True)
class XfyunASRConfig:
    """Credentials stay on the server and are never returned to the browser."""

    app_id: str = ""
    api_key: str = ""
    api_secret: str = ""
    host_url: str = XFYUN_IAT_URL

    @classmethod
    def from_env(
        cls,
        env: dict[str, str] | None = None,
        env_file: Path | None = None,
    ) -> "XfyunASRConfig":
        path = env_file or Path(__file__).resolve().parents[1] / ".env"
        values = _read_dotenv(path)
        values.update(os.environ if env is None else env)
        return cls(
            app_id=values.get("XFYUN_APPID", "").strip(),
            api_key=values.get("XFYUN_API_KEY", "").strip(),
            api_secret=values.get("XFYUN_API_SECRET", "").strip(),
            host_url=values.get("XFYUN_IAT_URL", XFYUN_IAT_URL).strip()
            or XFYUN_IAT_URL,
        )

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.api_key and self.api_secret)


class SpeechSession(Protocol):
    text: str

    def open(self) -> None: ...

    def send_audio(self, pcm: bytes) -> str: ...

    def finish(self) -> str: ...

    def close(self) -> None: ...


class XfyunStreamingSession:
    """One authenticated iFLYTEK IAT WebSocket conversation."""

    def __init__(self, config: XfyunASRConfig, connector: Callable | None = None):
        self.config = config
        self.connector = connector
        self.ws = None
        self.parts: dict[int, str] = {}
        self.text = ""
        self.started = False
        self._lock = threading.RLock()

    def open(self) -> None:
        if not self.config.configured:
            raise SpeechServiceError("科大讯飞语音服务尚未配置")
        if urlparse(self.config.host_url).scheme != "wss":
            raise SpeechServiceError("语音服务地址必须使用安全 WebSocket")
        try:
            if self.connector is None:
                import websocket

                connector = websocket.create_connection
            else:
                connector = self.connector
            # Keep normal certificate verification enabled. The legacy code
            # disabled it, which is unsafe for a public deployment.
            self.ws = connector(self._auth_url(), timeout=8)
            self.ws.settimeout(0.12)
        except SpeechServiceError:
            raise
        except Exception as exc:
            raise SpeechServiceError("无法连接科大讯飞语音服务") from exc
        self.parts = {}
        self.text = ""
        self.started = False

    def _auth_url(self) -> str:
        parsed = urlparse(self.config.host_url)
        date = format_datetime(datetime.now(timezone.utc), usegmt=True)
        request_line = f"GET {parsed.path} HTTP/1.1"
        sign_origin = f"host: {parsed.netloc}\ndate: {date}\n{request_line}"
        signature = hmac.new(
            self.config.api_secret.encode("utf-8"),
            sign_origin.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        auth_origin = (
            f'api_key="{self.config.api_key}", algorithm="hmac-sha256", '
            f'headers="host date request-line", '
            f'signature="{base64.b64encode(signature).decode("utf-8")}"'
        )
        query = urlencode(
            {
                "authorization": base64.b64encode(
                    auth_origin.encode("utf-8")
                ).decode("utf-8"),
                "date": date,
                "host": parsed.netloc,
            }
        )
        return f"{self.config.host_url}?{query}"

    def send_audio(self, pcm: bytes, *, final: bool = False) -> str:
        with self._lock:
            if self.ws is None:
                raise SpeechServiceError("语音会话尚未启动")
            status = 0 if not self.started else 2 if final else 1
            frame: dict[str, object] = {
                "data": {
                    "status": status,
                    "format": "audio/L16;rate=16000",
                    "encoding": "raw",
                    "audio": base64.b64encode(pcm).decode("utf-8"),
                }
            }
            if status == 0:
                frame["common"] = {"app_id": self.config.app_id}
                frame["business"] = {
                    "language": "zh_cn",
                    "domain": "iat",
                    "accent": "mandarin",
                    "vad_eos": 3000,
                    "ptt": 1,
                }
                self.started = True
            try:
                self.ws.send(json.dumps(frame, ensure_ascii=False))
                self._drain_messages()
            except SpeechServiceError:
                raise
            except Exception as exc:
                raise SpeechServiceError("语音片段上传失败") from exc
            return self.text

    def finish(self) -> str:
        with self._lock:
            if self.ws is None:
                return self.text
            try:
                if not self.started:
                    self.send_audio(b"\x00" * 1280)
                self.send_audio(b"", final=True)
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    if self._drain_messages(stop_on_final=True):
                        break
            finally:
                self.close()
            return self.text

    def _drain_messages(self, *, stop_on_final: bool = False) -> bool:
        if self.ws is None:
            return False
        final_seen = False
        while True:
            try:
                payload = json.loads(self.ws.recv())
            except Exception:
                break
            if payload.get("code") != 0:
                code = payload.get("code", "unknown")
                raise SpeechServiceError(f"科大讯飞识别失败（代码 {code}）")
            data = payload.get("data") or {}
            self._apply_result(data.get("result") or {})
            if data.get("status") == 2:
                final_seen = True
                break
        return final_seen if stop_on_final else False

    def _apply_result(self, result: dict) -> None:
        words = [
            candidate.get("w", "")
            for item in result.get("ws", [])
            for candidate in item.get("cw", [])
            if candidate.get("w")
        ]
        text = "".join(words)
        if not text:
            return
        try:
            sequence = int(result.get("sn"))
        except (TypeError, ValueError):
            sequence = len(self.parts) + 1
        if result.get("pgs") == "rpl":
            replacement_range = result.get("rg") or []
            if len(replacement_range) == 2:
                start, end = map(int, replacement_range)
                for key in list(self.parts):
                    if start <= key <= end:
                        self.parts.pop(key, None)
        self.parts[sequence] = text
        self.text = "".join(self.parts[key] for key in sorted(self.parts))

    def close(self) -> None:
        with self._lock:
            if self.ws is not None:
                try:
                    self.ws.close()
                except Exception:
                    pass
            self.ws = None
            self.started = False


@dataclass
class _ManagedSession:
    session: SpeechSession
    touched_at: float


class XfyunSpeechManager:
    """Bounded in-memory session registry for the browser HTTP bridge."""

    def __init__(
        self,
        config: XfyunASRConfig | None = None,
        *,
        session_factory: Callable[[XfyunASRConfig], SpeechSession] | None = None,
        max_sessions: int = 8,
        session_ttl_s: float = 90.0,
    ):
        self.config = config or XfyunASRConfig.from_env()
        self.session_factory = session_factory or XfyunStreamingSession
        self.max_sessions = max_sessions
        self.session_ttl_s = session_ttl_s
        self._sessions: dict[str, _ManagedSession] = {}
        self._starting = 0
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self.config.configured

    def _remove_stale_locked(self, now: float) -> list[SpeechSession]:
        stale_ids = [
            session_id
            for session_id, managed in self._sessions.items()
            if now - managed.touched_at > self.session_ttl_s
        ]
        return [self._sessions.pop(session_id).session for session_id in stale_ids]

    @staticmethod
    def _close_all(sessions: list[SpeechSession]) -> None:
        for session in sessions:
            session.close()

    def start(self) -> str:
        if not self.available:
            raise SpeechServiceError("科大讯飞语音服务尚未配置")
        now = time.monotonic()
        with self._lock:
            stale = self._remove_stale_locked(now)
            at_capacity = len(self._sessions) + self._starting >= self.max_sessions
            if not at_capacity:
                self._starting += 1
        self._close_all(stale)
        if at_capacity:
            raise SpeechServiceError("语音服务繁忙，请稍后重试")
        session = self.session_factory(self.config)
        try:
            session.open()
            session_id = uuid.uuid4().hex
            with self._lock:
                self._sessions[session_id] = _ManagedSession(
                    session, time.monotonic()
                )
        finally:
            with self._lock:
                self._starting -= 1
        return session_id

    def _get(self, session_id: str) -> SpeechSession:
        with self._lock:
            managed = self._sessions.get(session_id)
            if managed is not None:
                managed.touched_at = time.monotonic()
        if managed is None:
            raise SpeechServiceError("语音会话已结束，请重新开始录音")
        return managed.session

    def chunk(self, session_id: str, pcm: bytes) -> str:
        return self._get(session_id).send_audio(pcm)

    def finish(self, session_id: str) -> str:
        with self._lock:
            managed = self._sessions.pop(session_id, None)
        if managed is None:
            raise SpeechServiceError("语音会话已结束，请重新开始录音")
        try:
            return managed.session.finish()
        except Exception:
            managed.session.close()
            raise

    def close_all(self) -> None:
        with self._lock:
            sessions = [item.session for item in self._sessions.values()]
            self._sessions.clear()
        self._close_all(sessions)
