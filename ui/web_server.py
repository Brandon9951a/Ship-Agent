"""FastAPI application for the verified five-tool local demonstration."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import socket
import string
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from core.llm_layer import LLMClient, LLMConfig, LLMConfigError
from core.speech import SpeechServiceError, XfyunSpeechManager
from ui.app import run, run_text


ROOT = Path(__file__).resolve().parent / "frontend"
REFERENCE_UI_ROOT = Path(__file__).resolve().parents[1] / "docs/参考/原版UI前端"
REFERENCE_STYLESHEET = REFERENCE_UI_ROOT / "static/app.css"
REFERENCE_BACKGROUND = REFERENCE_UI_ROOT / "static/vessel-ocean-background.jpg"
MAX_REQUEST_BYTES = 64 * 1024
MAX_VOICE_REQUEST_BYTES = 96 * 1024
MAX_VOICE_CHUNK_BYTES = 64 * 1024
STATIC_FILES = {
    "app.css": (REFERENCE_STYLESHEET, "text/css; charset=utf-8"),
    "interaction.css": (ROOT / "interaction.css", "text/css; charset=utf-8"),
    "app.js": (ROOT / "app.js", "text/javascript; charset=utf-8"),
    "ship-3d.js": (ROOT / "ship-3d.js", "text/javascript; charset=utf-8"),
    "three.module.js": (ROOT / "vendor" / "three.module.js", "text/javascript; charset=utf-8"),
    "OrbitControls.js": (ROOT / "vendor" / "OrbitControls.js", "text/javascript; charset=utf-8"),
    "RoomEnvironment.js": (ROOT / "vendor" / "RoomEnvironment.js", "text/javascript; charset=utf-8"),
    "GLTFLoader.js": (ROOT / "vendor" / "GLTFLoader.js", "text/javascript; charset=utf-8"),
    "BufferGeometryUtils.js": (ROOT / "vendor" / "BufferGeometryUtils.js", "text/javascript; charset=utf-8"),
    "THREE-LICENSE.txt": (ROOT / "vendor" / "THREE-LICENSE.txt", "text/plain; charset=utf-8"),
    "vessel-ocean-background.jpg": (REFERENCE_BACKGROUND, "image/jpeg"),
}
ASSET_FILES = {
    "ship.glb": (ROOT / "assets" / "ship.glb", "model/gltf-binary"),
}


def _error(status_code: int, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"status": "invalid_input", "error": code},
    )


def create_app(
    *,
    llm_client: LLMClient | None = None,
    llm_mode: str = "disabled",
    speech_manager: XfyunSpeechManager | None = None,
) -> FastAPI:
    app = FastAPI(
        title="绿航智算本地演示",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.llm_client = llm_client
    app.state.llm_mode = llm_mode
    app.state.speech_manager = speech_manager or XfyunSpeechManager()

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
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
        return {
            "status": "ok",
            "service": "ship-agent-web",
            "scope": "synthetic_demo",
            "real_ship_validation": False,
            "llm_mode": app.state.llm_mode,
            "speech_mode": (
                "iflytek_iat" if app.state.speech_manager.available else "unconfigured"
            ),
        }

    @app.get("/api/voice/status", include_in_schema=False)
    async def voice_status() -> dict[str, Any]:
        return {
            "available": app.state.speech_manager.available,
            "provider": "iflytek_iat",
        }

    @app.post("/api/voice/start", include_in_schema=False)
    async def voice_start():
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
            return JSONResponse(
                status_code=503,
                content={"status": "unavailable", "error": str(exc)},
            )
        except Exception:
            return JSONResponse(
                status_code=502,
                content={"status": "failed", "error": "语音服务连接失败"},
            )
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
            text = await run_in_threadpool(
                app.state.speech_manager.finish, session_id,
            )
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
        try:
            if "task_text" in payload:
                result = await run_in_threadpool(
                    run_text, payload["task_text"], llm_client=app.state.llm_client,
                )
            else:
                result = await run_in_threadpool(
                    run, payload, llm_client=app.state.llm_client,
                )
        except (TypeError, ValueError, KeyError):
            return _error(400, "invalid_request")
        except Exception:
            return JSONResponse(
                status_code=500,
                content={"status": "failed", "error": "workflow_failed"},
            )
        return JSONResponse(content=result)

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
    parser = argparse.ArgumentParser(description="运行绿航智算本地驾驶舱")
    parser.add_argument("--host", default=os.environ.get("SHIP_WEB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    parser.add_argument(
        "--llm", action="store_true", help="启用云端定性理解和解释；失败自动回退模板",
    )
    parser.add_argument(
        "--open-browser", action="store_true", help="服务就绪后尝试打开本地页面",
    )
    args = parser.parse_args()
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
