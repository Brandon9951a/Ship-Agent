"""FastAPI application for the verified five-tool local demonstration."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import hmac
import socket
import string
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from core.llm_layer import LLMClient, LLMConfig, LLMConfigError
from research.agent.guidance import operator_guidance
from core.speech import SpeechServiceError, XfyunSpeechManager
from core.workflow_runtime import (
    InvalidAdjustmentOptionError, WorkflowNotAwaitingChoiceError,
    WorkflowNotFoundError, open_workflow_runtime, validate_thread_id,
)
from ui.app import _result_from_state
from ui.auth_store import AuthStore, COOKIE_NAME


ROOT = Path(__file__).resolve().parent / "frontend"
PROJECT_ROOT = ROOT.parents[1]
STYLESHEET = ROOT / "app.css"
BACKGROUND = ROOT / "vessel-ocean-background.jpg"
RELEASE_PROFILE_PATH = PROJECT_ROOT / "configs" / "release_profile.json"
DEMO_POLICY_PATH = PROJECT_ROOT / "configs" / "demo_policy.yaml"
MAX_REQUEST_BYTES = 64 * 1024
MAX_VOICE_REQUEST_BYTES = 96 * 1024
MAX_VOICE_CHUNK_BYTES = 64 * 1024
STATIC_FILES = {
    "research-bridge.js": (ROOT / "research-bridge.js", "text/javascript; charset=utf-8"),
    "research-bridge.css": (ROOT / "research-bridge.css", "text/css; charset=utf-8"),
    "app.css": (STYLESHEET, "text/css; charset=utf-8"),
    "interaction.css": (ROOT / "interaction.css", "text/css; charset=utf-8"),
    "app.js": (ROOT / "app.js", "text/javascript; charset=utf-8"),
    "ship-3d.js": (ROOT / "ship-3d.js", "text/javascript; charset=utf-8"),
    "three.module.js": (ROOT / "vendor" / "three.module.js", "text/javascript; charset=utf-8"),
    "OrbitControls.js": (ROOT / "vendor" / "OrbitControls.js", "text/javascript; charset=utf-8"),
    "RoomEnvironment.js": (ROOT / "vendor" / "RoomEnvironment.js", "text/javascript; charset=utf-8"),
    "GLTFLoader.js": (ROOT / "vendor" / "GLTFLoader.js", "text/javascript; charset=utf-8"),
    "BufferGeometryUtils.js": (ROOT / "vendor" / "BufferGeometryUtils.js", "text/javascript; charset=utf-8"),
    "THREE-LICENSE.txt": (ROOT / "vendor" / "THREE-LICENSE.txt", "text/plain; charset=utf-8"),
    "vessel-ocean-background.jpg": (BACKGROUND, "image/jpeg"),
}
ASSET_FILES = {
    "ship.glb": (ROOT / "assets" / "ship.glb", "model/gltf-binary"),
}


def _load_release_metadata() -> dict[str, Any]:
    """Load the frozen web baseline and its adopted demo parameter profile."""
    profile = json.loads(RELEASE_PROFILE_PATH.read_text(encoding="utf-8"))
    policy = json.loads(DEMO_POLICY_PATH.read_text(encoding="utf-8"))
    return {
        **profile,
        "parameter_approved_at": policy.get("approved_at"),
        "effective_capacity_kwh": policy.get("battery", {}).get(
            "total_effective_capacity_kwh"
        ),
        "soc_planning_min": policy.get("soc", {}).get("planning_min"),
    }


RELEASE_METADATA = _load_release_metadata()


def _error(status_code: int, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"status": "invalid_input", "error": code},
    )


def _workflow_error(status_code: int, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"status": "failed", "error": code},
    )


AUTH_MESSAGES = {
    "invalid_auth_request": "请检查登录输入", "invalid_credentials": "账号或密码错误",
    "auth_required": "请登录后继续", "forbidden": "无权限或请求已失效",
    "too_many_attempts": "尝试次数过多，请稍后再试",
    "auth_unavailable": "认证服务暂不可用",
}


def _auth_error(status_code: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={
        "status": "failed", "error": code, "message": AUTH_MESSAGES[code],
    })


def _session_body(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "user": {key: session[key] for key in ("id", "username", "display_name", "role")},
        "csrf_token": session["csrf_token"],
        "session_expires_at": datetime.fromtimestamp(
            session["expires_at"], timezone.utc,
        ).isoformat().replace("+00:00", "Z"),
    }


def _request_origin(request: Request) -> tuple[str, bool]:
    configured = os.getenv("SHIP_PUBLIC_ORIGIN", "").rstrip("/")
    if configured:
        return configured, configured.startswith("https://")
    host = request.headers.get("host", "")
    hostname = urlsplit(f"http://{host}").hostname
    local = hostname in ("127.0.0.1", "localhost", "::1", "testserver")
    scheme = request.url.scheme if local else "https"
    return f"{scheme}://{host}", scheme == "https"


def _public_result(result: dict[str, Any], role: str) -> dict[str, Any]:
    if role == "admin":
        return result
    # Keep numerical model provenance and simulation limits. Remove only
    # service-model diagnostics, including fields nested in report/understanding.
    private_keys = {"llm_call_status", "explanation_mode", "provider",
                    "requested_model", "response_model", "raw_error", "error_detail"}
    def clean(value):
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items() if key not in private_keys}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value
    public = clean(result)
    if isinstance(public.get("task_understanding"), dict):
        public["task_understanding"].pop("mode", None)
    return public


def create_app(
    *,
    llm_client: LLMClient | None = None,
    llm_mode: str = "disabled",
    speech_manager: XfyunSpeechManager | None = None,
    checkpoint_backend: str | None = None,
    checkpoint_database_url: str | None = None,
    checkpoint_sqlite_path: str | Path | None = None,
    auth_store: AuthStore | None = None,
    research_directory: str | Path | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        try:
            await run_in_threadpool(application.state.auth_store.ensure_schema)
        except Exception:
            # The health endpoint remains available; protected APIs return 503.
            pass
        async with open_workflow_runtime(
            llm_client=llm_client,
            backend=checkpoint_backend,
            database_url=checkpoint_database_url,
            sqlite_path=checkpoint_sqlite_path,
        ) as runtime:
            application.state.workflow_runtime = runtime
            from research.agent.runtime import AgentRuntime
            directory = research_directory or os.getenv("SHIP_RESEARCH_STORAGE") or (
                Path(checkpoint_sqlite_path).parent / "research-agent" if checkpoint_sqlite_path
                else PROJECT_ROOT / "var" / "research-agent")
            application.state.research_runtime = AgentRuntime(directory, llm_client)
            try:
                yield
            finally:
                await run_in_threadpool(application.state.research_runtime.close)

    app = FastAPI(
        title="智行合一本地演示",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.llm_client = llm_client
    app.state.llm_mode = llm_mode
    app.state.speech_manager = speech_manager or XfyunSpeechManager()
    auth_backend = os.getenv("SHIP_AUTH_BACKEND") or (
        "postgres" if (checkpoint_backend or os.getenv("SHIP_CHECKPOINT_BACKEND")) == "postgres" else "sqlite"
    )
    app.state.auth_store = auth_store or AuthStore(backend=auth_backend,
        database_url=checkpoint_database_url,
        sqlite_path=os.getenv("SHIP_AUTH_SQLITE_PATH"))

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        path = request.url.path
        is_api = path.startswith("/api/")
        is_write = request.method not in ("GET", "HEAD", "OPTIONS")
        response = None
        if is_api and is_write:
            origin = request.headers.get("origin")
            parsed = urlsplit(origin or "")
            expected, _secure = _request_origin(request)
            if not origin or f"{parsed.scheme}://{parsed.netloc}" != expected or parsed.path:
                response = _auth_error(403, "forbidden")
        protected = is_api and path not in ("/api/auth/login",)
        if response is None and protected:
            try:
                request.state.session = await run_in_threadpool(
                    app.state.auth_store.get_session, request.cookies.get(COOKIE_NAME),
                )
            except Exception:
                response = _auth_error(503, "auth_unavailable")
            else:
                if request.state.session is None and path != "/api/auth/logout":
                    response = _auth_error(401, "auth_required")
                elif is_write and request.state.session is not None:
                    csrf = request.headers.get("x-csrf-token", "")
                    if not hmac.compare_digest(csrf, request.state.session["csrf_token"]):
                        response = _auth_error(403, "forbidden")
        if response is None:
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(ROOT / "index.html", media_type="text/html")

    @app.get("/static/{name}", include_in_schema=False)
    async def static_file(name: str):
        item = STATIC_FILES.get(name)
        if item is None or not item[0].is_file():
            return _error(404, "not_found")
        return FileResponse(item[0], media_type=item[1])

    @app.get("/assets/{name}", include_in_schema=False)
    async def asset_file(name: str):
        item = ASSET_FILES.get(name)
        if item is None or not item[0].is_file():
            return _error(404, "not_found")
        return FileResponse(item[0], media_type=item[1])

    @app.get("/healthz", include_in_schema=False)
    async def health() -> dict[str, Any]:
        return {"status": "ok", "service": "ship-agent-web"}

    @app.get("/api/admin/system-status", include_in_schema=False)
    async def admin_status(request: Request):
        if request.state.session["role"] != "admin":
            return _auth_error(403, "forbidden")
        return {
            "status": "ok",
            "release": RELEASE_METADATA,
            "llm_mode": app.state.llm_mode,
            "checkpoint_backend": app.state.workflow_runtime.checkpoint_backend,
            "checkpoint_ready": app.state.workflow_runtime.checkpoint_ready,
            "speech_mode": (
                "iflytek_iat" if app.state.speech_manager.available else "unconfigured"
            ),
        }

    @app.get("/api/admin/research-runtime", include_in_schema=False)
    async def research_runtime_status(request: Request):
        """队列、恢复与超时边界；供管理员定位“任务卡住/服务重启”类问题。"""
        if request.state.session["role"] != "admin":
            return _auth_error(403, "forbidden")
        research = app.state.research_runtime
        try:
            queue = {
                "queued": research.store.count_status("queued"),
                "running": research.store.count_status("running"),
                "waiting_human": research.store.count_status("waiting_human"),
                "failed": research.store.count_status("failed"),
            }
        except Exception:
            queue = None
        return {
            "status": "ok",
            "queue": queue,
            "limits": {
                "max_active_tasks": research.limits.max_active_tasks,
                "task_timeout_s": research.limits.task_timeout_s,
                "queue_timeout_s": research.limits.queue_timeout_s,
                "max_recovery_attempts": research.limits.max_recovery_attempts,
            },
            "last_recovery": {
                "recovered_at": research.recovery.get("recovered_at"),
                "resumed_from_checkpoint": len(research.recovery.get("resumed_from_checkpoint") or []),
                "restarted_from_input": len(research.recovery.get("restarted_from_input") or []),
                "exhausted": len(research.recovery.get("exhausted") or []),
            },
        }

    @app.post("/api/auth/login", include_in_schema=False)
    async def login(request: Request):
        body = await request.body()
        if not body or len(body) > 4096:
            return _auth_error(400, "invalid_auth_request")
        try:
            payload = json.loads(body)
            username, password = payload["username"], payload["password"]
            if (not isinstance(payload, dict) or not isinstance(username, str) or
                not isinstance(password, str) or not 1 <= len(username) <= 64 or
                not username.isascii() or not 1 <= len(password) <= 1024):
                raise ValueError
        except (ValueError, TypeError, KeyError, UnicodeDecodeError):
            return _auth_error(400, "invalid_auth_request")
        key = f"{request.client.host if request.client else 'unknown'}:{username.lower()}"
        try:
            await run_in_threadpool(app.state.auth_store.revoke_session,
                                    request.cookies.get(COOKIE_NAME))
            user, throttled = await run_in_threadpool(
                app.state.auth_store.authenticate, username, password, key,
            )
            if throttled:
                return _auth_error(429, "too_many_attempts")
            if not user:
                return _auth_error(401, "invalid_credentials")
            token, csrf, expires = await run_in_threadpool(
                app.state.auth_store.create_session, user["id"], request.cookies.get(COOKIE_NAME),
            )
        except Exception:
            return _auth_error(503, "auth_unavailable")
        response = JSONResponse(content=_session_body({**user, "csrf_token": csrf, "expires_at": expires}))
        _origin, secure = _request_origin(request)
        response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", path="/",
                            secure=secure, max_age=24 * 60 * 60)
        return response

    @app.get("/api/auth/me", include_in_schema=False)
    async def me(request: Request):
        return _session_body(request.state.session)

    @app.post("/api/auth/logout", include_in_schema=False)
    async def logout(request: Request):
        try:
            await run_in_threadpool(app.state.auth_store.revoke_session,
                                    request.cookies.get(COOKIE_NAME))
        except Exception:
            return _auth_error(503, "auth_unavailable")
        response = Response(status_code=204)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @app.get("/api/voice/status", include_in_schema=False)
    async def voice_status() -> dict[str, Any]:
        return {
            "available": app.state.speech_manager.available,
            "provider": "iflytek_iat",
        }

    @app.post("/api/voice/start", include_in_schema=False)
    async def voice_start(request: Request):
        if not app.state.speech_manager.available:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "unavailable",
                    "error": "科大讯飞语音服务尚未配置",
                },
            )
        try:
            session_id = await run_in_threadpool(app.state.speech_manager.start)
        except SpeechServiceError as exc:
            return JSONResponse(status_code=503, content={
                "status": "unavailable", "error": str(exc),
            })
        except Exception:
            return JSONResponse(status_code=502, content={
                "status": "failed", "error": "语音服务连接失败",
            })
        try:
            await run_in_threadpool(app.state.auth_store.bind_voice, session_id,
                                    request.state.session["id"])
        except Exception:
            if hasattr(app.state.speech_manager, "cancel"):
                await run_in_threadpool(app.state.speech_manager.cancel, session_id)
            return _auth_error(503, "auth_unavailable")
        return {"session_id": session_id, "text": ""}

    @app.post("/api/voice/chunk", include_in_schema=False)
    async def voice_chunk(request: Request):
        body = await request.body()
        if not body or len(body) > MAX_VOICE_REQUEST_BYTES:
            return _error(413 if body else 400, "invalid_voice_request")
        try:
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
            session_id = str(payload.get("session_id", ""))
            encoded_audio = str(payload.get("audio", ""))
            if (
                len(session_id) != 32
                or any(character not in string.hexdigits for character in session_id)
            ):
                raise ValueError
            try:
                owned = await run_in_threadpool(app.state.auth_store.owns_voice,
                                                session_id, request.state.session["id"])
            except Exception:
                return _auth_error(503, "auth_unavailable")
            if not owned:
                return _workflow_error(404, "workflow_not_found")
            pcm = base64.b64decode(encoded_audio, validate=True)
            if not pcm or len(pcm) > MAX_VOICE_CHUNK_BYTES:
                raise ValueError
            text = await run_in_threadpool(
                app.state.speech_manager.chunk, session_id, pcm,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, binascii.Error, ValueError):
            return _error(400, "invalid_voice_request")
        except SpeechServiceError as exc:
            return JSONResponse(
                status_code=400,
                content={"status": "failed", "error": str(exc)},
            )
        except Exception:
            return JSONResponse(
                status_code=502,
                content={"status": "failed", "error": "语音识别失败"},
            )
        return {"text": text}

    @app.post("/api/voice/finish", include_in_schema=False)
    async def voice_finish(request: Request):
        body = await request.body()
        if not body or len(body) > MAX_VOICE_REQUEST_BYTES:
            return _error(413 if body else 400, "invalid_voice_request")
        try:
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
            session_id = str(payload.get("session_id", ""))
            if (
                len(session_id) != 32
                or any(character not in string.hexdigits for character in session_id)
            ):
                raise ValueError
            try:
                owned = await run_in_threadpool(app.state.auth_store.owns_voice,
                                                session_id, request.state.session["id"])
            except Exception:
                return _auth_error(503, "auth_unavailable")
            if not owned:
                return _workflow_error(404, "workflow_not_found")
            text = await run_in_threadpool(
                app.state.speech_manager.finish, session_id,
            )
            await run_in_threadpool(app.state.auth_store.remove_voice, session_id)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return _error(400, "invalid_voice_request")
        except SpeechServiceError as exc:
            return JSONResponse(
                status_code=400,
                content={"status": "failed", "error": str(exc)},
            )
        except Exception:
            return JSONResponse(
                status_code=502,
                content={"status": "failed", "error": "语音识别结束失败"},
            )
        return {"text": text}

    @app.post("/api/run", include_in_schema=False)
    async def run_api(request: Request):
        length_header = request.headers.get("content-length")
        if length_header:
            try:
                if int(length_header) > MAX_REQUEST_BYTES:
                    return _error(413, "request_too_large")
            except ValueError:
                return _error(400, "invalid_content_length")
        body = await request.body()
        if not body:
            return _error(400, "empty_request")
        if len(body) > MAX_REQUEST_BYTES:
            return _error(413, "request_too_large")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _error(400, "invalid_json")
        if not isinstance(payload, dict):
            return _error(400, "request_must_be_object")
        if payload.get("workflow_mode") == "research_agent":
            from research.agent.runtime import RuntimeQueueFull
            try:
                result = await run_in_threadpool(app.state.research_runtime.start, payload.get("task_text"),
                    owner=request.state.session["id"], scenario_id=payload.get("scenario_id", "route_success"),
                    task_type=payload.get("task_type", "auto"))
                return JSONResponse(result, status_code=202)
            except RuntimeQueueFull as exc:
                return JSONResponse(status_code=429, content={
                    "error": "research_queue_full", "limit": exc.limit,
                    "active": exc.active, "retry_after_s": 5,
                    "detail": "活跃任务已达上限，请等待已有任务结束或取消后重试。",
                })
            except (KeyError, TypeError, ValueError):
                return _error(400, "invalid_research_request")
        # Bind before invoking the graph so a created checkpoint is never
        # observable without an owner record, including interrupted runs.
        thread_id = str(uuid4())
        try:
            await run_in_threadpool(app.state.auth_store.bind_workflow,
                                    thread_id, request.state.session["id"])
        except Exception:
            return _auth_error(503, "auth_unavailable")
        try:
            if "task_text" in payload:
                state = await app.state.workflow_runtime.start_text(payload["task_text"], thread_id=thread_id)
            else:
                state = await app.state.workflow_runtime.start_structured(payload, thread_id=thread_id)
            result = _result_from_state(state)
        except (TypeError, ValueError, KeyError):
            return _error(400, "invalid_request")
        except Exception:
            return JSONResponse(
                status_code=500,
                content={"status": "failed", "error": "workflow_failed"},
            )
        return JSONResponse(content=_public_result(result, request.state.session["role"]))

    @app.post("/api/resume", include_in_schema=False)
    async def resume_api(request: Request):
        body = await request.body()
        if not body or len(body) > MAX_REQUEST_BYTES:
            return _workflow_error(
                413 if body else 400,
                "request_too_large" if body else "invalid_resume_request",
            )
        try:
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
            thread_id = validate_thread_id(str(payload.get("thread_id", "")))
            if payload.get("workflow_mode") == "research_agent":
                try:
                    result = await run_in_threadpool(app.state.research_runtime.resume, thread_id,
                        request.state.session["id"], pending_id=payload.get("pending_id"),
                        revision=payload.get("revision"), response_id=payload.get("response_id"),
                        reply_text=payload.get("reply_text"))
                    return JSONResponse(result, status_code=202)
                except LookupError:
                    return _workflow_error(404, "workflow_not_found")
                except (TypeError, ValueError):
                    return _workflow_error(409, "invalid_or_stale_response")
            decision_id = str(payload.get("decision_id", ""))
            option_id = str(payload.get("option_id", ""))
            if (
                not decision_id
                or len(decision_id) > 300
                or not option_id
                or len(option_id) > 100
            ):
                raise ValueError
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return _workflow_error(400, "invalid_resume_request")
        try:
            owned = await run_in_threadpool(app.state.auth_store.owns_workflow,
                                            thread_id, request.state.session["id"])
        except Exception:
            return _auth_error(503, "auth_unavailable")
        if not owned:
            return _workflow_error(404, "workflow_not_found")
        try:
            state = await app.state.workflow_runtime.resume(
                thread_id, decision_id, option_id,
            )
        except WorkflowNotFoundError:
            return _workflow_error(404, "workflow_not_found")
        except WorkflowNotAwaitingChoiceError:
            return _workflow_error(409, "workflow_not_awaiting_choice")
        except InvalidAdjustmentOptionError:
            return _workflow_error(409, "invalid_adjustment_option")
        except Exception:
            return _workflow_error(500, "workflow_failed")
        return JSONResponse(content=_public_result(_result_from_state(state), request.state.session["role"]))

    @app.get("/api/runs/{thread_id}", include_in_schema=False)
    async def get_run_api(thread_id: str, request: Request):
        try:
            thread_id = validate_thread_id(thread_id)
        except ValueError:
            return _workflow_error(400, "invalid_resume_request")
        try:
            store = app.state.research_runtime.store
            record = store.get(thread_id, request.state.session["id"])
            if record.get("status") == "completed":
                guidance = operator_guidance(store, thread_id, record.get("outcome_code"),
                                             record.get("request") or {}, record.get("result_refs") or {})
                if guidance:
                    record = {**record, "operator_guidance": guidance}
            return record
        except LookupError:
            pass
        try:
            owned = await run_in_threadpool(app.state.auth_store.owns_workflow,
                                            thread_id, request.state.session["id"])
        except Exception:
            return _auth_error(503, "auth_unavailable")
        if not owned:
            return _workflow_error(404, "workflow_not_found")
        try:
            state = await app.state.workflow_runtime.get_state(thread_id)
        except WorkflowNotFoundError:
            return _workflow_error(404, "workflow_not_found")
        except Exception:
            return _workflow_error(500, "workflow_failed")
        return JSONResponse(content=_public_result(_result_from_state(state), request.state.session["role"]))

    @app.get("/api/research/config", include_in_schema=False)
    async def research_config(request: Request):
        config = json.loads((PROJECT_ROOT / "configs/research_agent.json").read_text(encoding="utf-8"))
        client = app.state.llm_client
        return {"scenarios": config["scenarios"], "model_configured": bool(client and client.config.enabled),
                "requested_model": client.config.model if client else None,
                "connection_status": "验收以任务中的真实 model_completed 事件为准"}

    @app.get("/api/runs/{thread_id}/events", include_in_schema=False)
    async def research_events(thread_id: str, request: Request, after_seq: int = 0):
        runtime = app.state.research_runtime
        try:
            runtime.store.get(thread_id, request.state.session["id"])
            return {"events": runtime.store.events(thread_id, max(0, after_seq))}
        except LookupError:
            return _workflow_error(404, "workflow_not_found")

    @app.get("/api/runs/{thread_id}/artifacts/{artifact_ref}", include_in_schema=False)
    async def research_artifact(thread_id: str, artifact_ref: str, request: Request):
        runtime = app.state.research_runtime
        try:
            runtime.store.get(thread_id, request.state.session["id"])
            return runtime.store.read(thread_id, artifact_ref)
        except LookupError:
            return _workflow_error(404, "artifact_not_found")
        except (ValueError, OSError):
            return _workflow_error(409, "artifact_integrity_failed")

    @app.post("/api/runs/{thread_id}/cancel", include_in_schema=False)
    async def research_cancel(thread_id: str, request: Request):
        """取消排队/运行中/等待补充的任务；不发布未复核方案。"""
        try:
            thread_id = validate_thread_id(thread_id)
        except ValueError:
            return _workflow_error(400, "invalid_resume_request")
        try:
            task = await run_in_threadpool(app.state.research_runtime.cancel, thread_id,
                                           request.state.session["id"])
        except LookupError:
            return _workflow_error(404, "workflow_not_found")
        return task

    @app.post("/api/runs/{thread_id}/retry", include_in_schema=False)
    async def research_retry(thread_id: str, request: Request):
        """从检查点续跑或从输入快照重试；已完成的答复不会被重复发布。"""
        try:
            thread_id = validate_thread_id(thread_id)
        except ValueError:
            return _workflow_error(400, "invalid_resume_request")
        try:
            task = await run_in_threadpool(app.state.research_runtime.retry, thread_id,
                                           request.state.session["id"])
            return JSONResponse(task, status_code=202)
        except LookupError:
            return _workflow_error(404, "workflow_not_found")
        except ValueError as exc:
            return _workflow_error(409, str(exc) or "task_not_retryable")
        except Exception:
            return _workflow_error(503, "retry_unavailable")

    return app


def _load_llm(enabled: bool) -> tuple[LLMClient | None, str]:
    if not enabled:
        return None, "disabled"
    try:
        config = LLMConfig.from_env()
    except LLMConfigError:
        return None, "config_unavailable_template_fallback"
    if not config.enabled:
        return None, "disabled_template_fallback"
    return LLMClient(config), "enabled"


app = create_app()


def _open_browser_when_ready(
    host: str,
    port: int,
    *,
    browser_open=None,
    connector=None,
) -> None:
    """Open the page after Uvicorn starts; failure never stops the service."""
    open_page = browser_open or webbrowser.open
    connect = connector or socket.create_connection
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{display_host}:{port}"
    for _attempt in range(40):
        try:
            connection = connect((display_host, port), timeout=0.25)
            connection.close()
        except OSError:
            time.sleep(0.25)
            continue
        try:
            if not open_page(url):
                print(f"Browser did not open automatically. Open manually: {url}")
        except Exception:
            print(f"Browser did not open automatically. Open manually: {url}")
        return
    print(f"Service startup is taking longer than expected. Check manually: {url}")


def main() -> None:
    parser = argparse.ArgumentParser(description="运行智行合一本地驾驶舱")
    parser.add_argument("--host", default=os.environ.get("SHIP_WEB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    parser.add_argument("--env-file", type=Path, help="显式加载已有模型配置文件")
    parser.add_argument(
        "--llm", action="store_true", help="启用云端定性理解和解释；失败自动回退模板",
    )
    parser.add_argument(
        "--open-browser", action="store_true", help="服务就绪后尝试打开本地页面",
    )
    args = parser.parse_args()
    if args.env_file:
        os.environ["SHIP_ENV_FILE"] = str(args.env_file.resolve())
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    client, mode = _load_llm(args.llm)
    runtime_app = create_app(llm_client=client, llm_mode=mode)
    print(f"UI: http://{args.host}:{args.port} (llm={mode})")
    if args.open_browser:
        threading.Thread(
            target=_open_browser_when_ready,
            args=(args.host, args.port),
            daemon=True,
        ).start()
    uvicorn.run(
        runtime_app,
        host=args.host,
        port=args.port,
        access_log=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
