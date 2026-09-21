"""绿航智算 · Flask 前端 — Figma 风格船舶决策台
启动: python app.py --port 7860
迁移自 Gradio → Flask + Jinja2 + SSE（2026-08-03）
Arduino 双向联动完整保留
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import threading
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from bridge import load_route_details, load_routes, stream_inference
from arduino_bridge import (
    connect_hardware,
    disconnect_hardware,
    get_hardware_status,
    get_ports,
    lock_hardware_snapshot,
)
from xfyun_asr import XfyunStreamingSession

app = Flask(__name__, template_folder="templates", static_folder="static")
voice_sessions: dict[str, XfyunStreamingSession] = {}
voice_sessions_lock = threading.Lock()
inference_lock = threading.Lock()  # 单飞行锁，防止并发推理


# ═══════════════════════════════════════════════════
# 页面路由
# ═══════════════════════════════════════════════════

@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "adapter": "deterministic_loop.py", "arduino": get_hardware_status()["connected"]})


# ═══════════════════════════════════════════════════
# 航线数据 API
# ═══════════════════════════════════════════════════

@app.get("/api/routes")
def routes():
    return jsonify(load_routes())


@app.get("/api/route-details")
def route_details():
    return jsonify(load_route_details())


# ═══════════════════════════════════════════════════
# SSE 推理 API
# ═══════════════════════════════════════════════════

@app.post("/api/infer")
def infer():
    payload = request.get_json(silent=True) or {}
    message = str(payload.get("message", "")).strip()
    if not message:
        return jsonify({"error": "请输入航行任务或船舶问题。"}), 400

    # 高级参数（从 figma UI 扩展：硬件面板 + 高级参数）
    use_hardware = bool(payload.get("use_hardware", True))
    soc = float(payload.get("soc", 0) or 0)
    load_state = str(payload.get("load_state", "自动") or "自动")
    wind = float(payload.get("wind", 0) or 0)
    current = float(payload.get("current", 0) or 0)
    direction = int(payload.get("direction", 0) or 0)
    time_limit_h = float(payload.get("time_limit_h", 38) or 38)
    origin = str(payload.get("origin", "自动识别") or "自动识别")
    destination = str(payload.get("destination", "自动识别") or "自动识别")
    enable_narration = bool(payload.get("enable_narration", False))

    # 如果 infer payload 里带了硬件快照（由前端 /api/hardware/snapshot 提前锁定），
    # 则直接使用，避免在 SSE 流里再次锁定。
    snapshot = payload.get("snapshot")

    def event_stream():
        acquired = inference_lock.acquire(blocking=False)  # 非阻塞，拒绝并发
        if not acquired:
            error = {"message": "推理正在进行中，请等待当前任务完成。"}
            yield f"event: error\ndata: {json.dumps(error, ensure_ascii=False)}\n\n"
            return
        try:
            yield "event: ready\ndata: {}\n\n"
            for event in stream_inference(
                message=message,
                use_hardware=use_hardware,
                soc=soc,
                load_state=load_state,
                wind=wind,
                current=current,
                direction=direction,
                time_limit_h=time_limit_h,
                origin=origin,
                destination=destination,
                enable_narration=enable_narration,
            ):
                event_type = "state"
                if isinstance(event, dict) and event.get("type") in ("error", "infeasible"):
                    event_type = "error"
                yield f"event: {event_type}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            yield "event: done\ndata: {}\n\n"
        except Exception as exc:
            error = {"message": str(exc)}
            yield f"event: error\ndata: {json.dumps(error, ensure_ascii=False)}\n\n"
        finally:
            inference_lock.release()

    return Response(
        stream_with_context(event_stream()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ═══════════════════════════════════════════════════
# 语音识别 API（科大讯飞，与 figma app.py 完全一致）
# ═══════════════════════════════════════════════════

def _get_voice_session(session_id: str) -> XfyunStreamingSession:
    with voice_sessions_lock:
        session = voice_sessions.get(session_id)
    if session is None:
        raise RuntimeError("语音会话已结束，请重新点击麦克风开始录音。")
    return session


@app.post("/api/voice/start")
def voice_start():
    session = XfyunStreamingSession()
    try:
        session.open()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    session_id = uuid.uuid4().hex
    with voice_sessions_lock:
        voice_sessions[session_id] = session
    return jsonify({"session_id": session_id, "text": ""})


@app.post("/api/voice/chunk")
def voice_chunk():
    payload = request.get_json(silent=True) or {}
    try:
        session_id = str(payload.get("session_id", ""))
        pcm = base64.b64decode(str(payload.get("audio", "")), validate=True)
        if not pcm:
            return jsonify({"text": _get_voice_session(session_id).text})
        return jsonify({"text": _get_voice_session(session_id).send_audio(pcm)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/voice/finish")
def voice_finish():
    payload = request.get_json(silent=True) or {}
    session_id = str(payload.get("session_id", ""))
    with voice_sessions_lock:
        session = voice_sessions.pop(session_id, None)
    if session is None:
        return jsonify({"error": "语音会话已结束，请重新开始录音。"}), 400
    try:
        return jsonify({"text": session.finish()})
    except Exception as exc:
        session.close()
        return jsonify({"error": str(exc)}), 400


# ═══════════════════════════════════════════════════
# Arduino 硬件控制 API
# ═══════════════════════════════════════════════════

@app.get("/api/hardware/status")
def hardware_status():
    return jsonify(get_hardware_status())


@app.get("/api/hardware/ports")
def hardware_ports():
    return jsonify({"ports": get_ports()})


@app.post("/api/hardware/connect")
def hardware_connect():
    payload = request.get_json(silent=True) or {}
    port = str(payload.get("port", "") or "")
    return jsonify({"message": connect_hardware(port)})


@app.post("/api/hardware/disconnect")
def hardware_disconnect():
    return jsonify({"message": disconnect_hardware()})


@app.post("/api/hardware/lock")
def hardware_lock():
    return jsonify({"message": lock_hardware_snapshot()})


# ═══════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="绿航智算 Flask 前端 — Figma 风格船舶决策台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=7860, type=int)
    args = parser.parse_args()

    print("=" * 60)
    print("  绿航智算 · 内河船舶航速优化与能效管理智能决策系统")
    print("  模型: ship-qwen (Qwen2.5-7B + LoRA)")
    print("  框架: Flask + Jinja2 + SSE（Figma 前端）+ Arduino 桥接")
    print(f"  本地访问: http://{args.host}:{args.port}")
    print("=" * 60)

    app.run(host=args.host, port=args.port, debug=False, threaded=True)
