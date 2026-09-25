import json

from fastapi.testclient import TestClient

from core.llm_layer import LLMCallResult, LLMConfigError
from core.speech import XfyunASRConfig, XfyunSpeechManager
from ui.web_server import (
    BACKGROUND, MAX_REQUEST_BYTES, ROOT, STYLESHEET,
    _load_llm, _open_browser_when_ready, create_app,
)


NORMAL_TEXT = (
    "从平顶山港到军李船闸，2026-09-18 09:00出发，"
    "SOC85%，半载，6小时内到达"
)


def _client(*, llm_mode="disabled"):
    speech_manager = XfyunSpeechManager(config=XfyunASRConfig())
    return TestClient(
        create_app(
            llm_mode=llm_mode,
            speech_manager=speech_manager,
            checkpoint_backend="memory",
        )
    )


def test_frontend_assets_exist_and_have_core_surfaces():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    css = STYLESHEET.read_text(encoding="utf-8")
    js = (ROOT / "app.js").read_text(encoding="utf-8")
    for label in (
        "智行合一 · 船舶航速优化与能效管理智能决策系统",
        "任务描述", "快捷输入", "工况补充", "航线与分段方案",
        "航段能耗与速度", "推荐航速方案", "安全校验", "能量管理建议",
        "航行执行摘要", "工具调用过程", "选择已验证方案后恢复并重算",
        "安全下限由系统锁定", "确认修改并重新计算",
        "船舶三维运行态势", "五工具计算结果的软件回放",
        "峡谷内河演示环境",
        "开始科大讯飞语音输入", "正在检查语音服务",
        'id="vessel-canvas"', 'id="vessel-segment-select"',
    ):
        assert label in html
    assert "@media" in css
    assert "vessel-ocean-background.jpg" in css
    assert BACKGROUND.is_file()
    assert '$("#infeasible-alert").hidden = true' in js
    assert "if (result.request) syncCorrectionToTask(result.request);" in js
    assert 'setAttribute("aria-hidden", "true")' in js
    for marker in (
        "/api/run", "/api/resume", "/api/runs/", "/healthz", "task_text", "AI 提示已更新",
        "Tdata", "Tseg", "Tenergy", "Tspeed", "Tmanagement",
        "applyAdjustmentOption", "readCorrectionPayload", "payloadOverride",
        "ACTIVE_THREAD_KEY", "restorePendingRun", "decision_id", "option_id",
        "ship3d:update", "updateVesselView", "option.verified && option.modification",
        "startXfyunVoice", "/api/voice/start", "/api/voice/chunk",
        "/api/voice/finish", "pcm16Base64",
    ):
        assert marker in js
    ship_js = (ROOT / "ship-3d.js").read_text(encoding="utf-8")
    for marker in (
        "GLTFLoader", 'loader.load(\n    "/assets/ship.glb"', "updatePlan",
        "segmentPlaybackSeconds", "未知 / 待核实", "createCanyonSide",
        "addForest", "addShoreRocks", "addHillsideRoad", "addDistantMountains",
    ):
        assert marker in ship_js
    assert '<script type="module" src="/static/ship-3d.js"></script>' in html
    assert '<script type="importmap">' in html
    assert "cdn.jsdelivr.net/npm/three" not in html
    assert "const understandingMode = result.task_understanding?.mode" in js
    assert "数据来源" not in html
    for internal_copy in (
        "sourceLabel", "result.dashboard", "当前工具计算结束 SOC",
        "系统仅给出能量管理建议", "软件关注线",
    ):
        assert internal_copy not in js
    for removed in ("Arduino", "/api/hardware", "/api/infer"):
        assert removed not in html
        assert removed not in js


def test_index_health_static_and_security_headers():
    with _client(llm_mode="enabled") as client:
        index = client.get("/")
        assert index.status_code == 200
        assert "智行合一" in index.text
        assert index.headers["x-frame-options"] == "DENY"
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/static/interaction.css").status_code == 200
        assert client.get("/static/ship-3d.js").status_code == 200
        for name in (
            "three.module.js", "OrbitControls.js", "RoomEnvironment.js",
            "GLTFLoader.js", "BufferGeometryUtils.js", "THREE-LICENSE.txt",
        ):
            assert client.get(f"/static/{name}").status_code == 200
        stylesheet = client.get("/static/app.css")
        assert stylesheet.status_code == 200
        assert stylesheet.content == STYLESHEET.read_bytes()
        assert client.get("/static/vessel-ocean-background.jpg").status_code == 200
        assert client.get("/static/unknown.txt").status_code == 404
        ship = client.get("/assets/ship.glb")
        assert ship.status_code == 200
        assert ship.headers["content-type"].startswith("model/gltf-binary")
        assert ship.content[:4] == b"glTF"
        assert len(ship.content) > 1_000_000
        assert client.get("/assets/unknown.glb").status_code == 404
        health = client.get("/healthz").json()
    assert health == {
        "status": "ok",
        "service": "ship-agent-web",
        "scope": "synthetic_demo",
        "real_ship_validation": False,
        "llm_mode": "enabled",
        "speech_mode": "unconfigured",
        "checkpoint_backend": "memory",
        "checkpoint_ready": True,
    }


def test_voice_routes_bridge_audio_without_exposing_credentials():
    class FakeSpeechManager:
        available = True

        def __init__(self):
            self.calls = []

        def start(self):
            self.calls.append(("start",))
            return "a" * 32

        def chunk(self, session_id, pcm):
            self.calls.append(("chunk", session_id, pcm))
            return "从平顶山港到军李船闸"

        def finish(self, session_id):
            self.calls.append(("finish", session_id))
            return "从平顶山港到军李船闸，六小时内到达"

    manager = FakeSpeechManager()
    with TestClient(create_app(speech_manager=manager, checkpoint_backend="memory")) as client:
        status = client.get("/api/voice/status")
        started = client.post("/api/voice/start")
        chunk = client.post(
            "/api/voice/chunk",
            json={"session_id": "a" * 32, "audio": "AAECAw=="},
        )
        finished = client.post(
            "/api/voice/finish", json={"session_id": "a" * 32},
        )
    assert status.json() == {"available": True, "provider": "iflytek_iat"}
    assert started.json() == {"session_id": "a" * 32, "text": ""}
    assert chunk.json()["text"] == "从平顶山港到军李船闸"
    assert finished.json()["text"].endswith("六小时内到达")
    assert manager.calls == [
        ("start",),
        ("chunk", "a" * 32, b"\x00\x01\x02\x03"),
        ("finish", "a" * 32),
    ]
    response_text = status.text + started.text + chunk.text + finished.text
    assert "api_key" not in response_text.lower()
    assert "api_secret" not in response_text.lower()


def test_voice_routes_reject_missing_configuration_and_invalid_audio():
    class UnconfiguredSpeechManager:
        available = False

    with TestClient(create_app(speech_manager=UnconfiguredSpeechManager(), checkpoint_backend="memory")) as client:
        assert client.get("/api/voice/status").json()["available"] is False
        unavailable = client.post("/api/voice/start")
        malformed = client.post(
            "/api/voice/chunk",
            json={"session_id": "not-a-session", "audio": "not-base64"},
        )
    assert unavailable.status_code == 503
    assert unavailable.json()["status"] == "unavailable"
    assert malformed.status_code == 400
    assert malformed.json()["error"] == "invalid_voice_request"


def test_web_request_really_uses_configured_llm_client():
    class FakeClient:
        def __init__(self):
            self.calls = 0

        def complete_text(self, *_args, **_kwargs):
            self.calls += 1
            return LLMCallResult(
                "ok", "deepseek", "deepseek-v4-pro",
                text="建议按推荐航速执行，并持续关注电量变化和现场通航条件。",
                response_model="deepseek-v4-pro",
            )

    llm = FakeClient()
    with TestClient(create_app(llm_client=llm, llm_mode="enabled")) as client:
        result = client.post("/api/run", json={"task_text": NORMAL_TEXT}).json()
    assert result["task_understanding"]["mode"] == "llm_qualitative"
    assert result["report"]["explanation_mode"] == "llm_qualitative"
    assert llm.calls == 2


def test_natural_language_and_structured_requests_share_workflow():
    structured = {
        "origin": "平顶山港",
        "destination": "军李船闸",
        "departure_at": "2026-09-18T09:00:00+08:00",
        "max_duration_h": 6,
        "soc_initial": 0.85,
        "load_state": "半载",
        "environment": {},
    }
    with _client() as client:
        natural = client.post("/api/run", json={"task_text": NORMAL_TEXT})
        explicit = client.post("/api/run", json=structured)
    for response in (natural, explicit):
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "ok"
        assert [step["node"] for step in result["trace"] if step["node"].startswith("T")] == [
            "Tdata", "Tseg", "Tenergy", "Tspeed", "Tmanagement",
        ]
        assert result["report"]["scope"] == "synthetic_demo"
        assert result["report"]["summary"]["eta"]["unit"] == "ISO 8601"


def test_invalid_empty_and_oversized_requests_are_rejected():
    with _client() as client:
        assert client.post("/api/run", content=b"{").json()["error"] == "invalid_json"
        assert client.post("/api/run", content=b"").json()["error"] == "empty_request"
        assert client.post("/api/run", json=[]).json()["error"] == "request_must_be_object"
        assert client.post("/api/run", json={"task_text": "  "}).status_code == 400
        oversized = json.dumps({"task_text": "x" * MAX_REQUEST_BYTES}).encode()
        response = client.post(
            "/api/run", content=oversized, headers={"content-type": "application/json"},
        )
    assert response.status_code == 413
    assert response.json()["error"] == "request_too_large"


def test_infeasible_responses_do_not_include_success_report():
    cases = (
        (NORMAL_TEXT.replace("6小时", "1小时"), "time"),
        (NORMAL_TEXT.replace("SOC85%", "SOC31%"), "soc"),
    )
    with _client() as client:
        results = [client.post("/api/run", json={"task_text": text}).json() for text, _ in cases]
    for result, (_, expected_type) in zip(results, cases):
        assert result["status"] == "awaiting_choice"
        assert result["tspeed"]["infeasible_type"] == expected_type
        assert "report" not in result
        assert result["adjustment_options"]
        assert result["request"]["origin"] == "平顶山港"
        assert result["failed_tool"] == "Tspeed"
        assert result["boundary_diagnostics"]["diagnostic_only"] is True
        assert result["decision"]["type"] == "adjustment_choice"
        assert result["thread_id"] in result["decision"]["decision_id"]
        assert "未显示航速推荐、ETA、最终能耗或 SOC 成功结论" in result["dashboard"]


def test_operator_time_adjustment_reenters_same_workflow():
    with _client() as client:
        blocked = client.post(
            "/api/run", json={"task_text": NORMAL_TEXT.replace("6小时", "1小时")},
        ).json()
        option = next(
            item for item in blocked["adjustment_options"]
            if item["direction"] == "accept_late"
        )
        rerun = client.post("/api/resume", json={
            "thread_id": blocked["thread_id"],
            "decision_id": blocked["decision"]["decision_id"],
            "option_id": option["option_id"],
        }).json()
    assert rerun["status"] == "ok"
    assert rerun["request"]["max_duration_h"] > blocked["request"]["max_duration_h"]
    tool_trace = [item for item in rerun["trace"] if item["node"].startswith("T")]
    assert [item["node"] for item in tool_trace] == [
        "Tdata", "Tseg", "Tenergy", "Tspeed",
        "Tdata", "Tseg", "Tenergy", "Tspeed", "Tmanagement",
    ]
    assert [item["iteration"] for item in tool_trace] == [0, 0, 0, 0, 1, 1, 1, 1, 1]


def test_operator_soc_adjustment_reenters_same_workflow():
    with _client() as client:
        blocked = client.post(
            "/api/run", json={"task_text": NORMAL_TEXT.replace("SOC85%", "SOC31%")},
        ).json()
        recharge = next(
            item for item in blocked["adjustment_options"]
            if item["direction"] == "recharge"
        )
        rerun = client.post("/api/resume", json={
            "thread_id": blocked["thread_id"],
            "decision_id": blocked["decision"]["decision_id"],
            "option_id": recharge["option_id"],
        }).json()
    assert blocked["status"] == "awaiting_choice"
    assert blocked["tspeed"]["infeasible_type"] == "soc"
    assert "accept_lower_soc" not in {
        option["direction"] for option in blocked["adjustment_options"]
    }
    assert [option["direction"] for option in blocked["adjustment_options"]] == [
        "recharge", "give_up",
    ]
    assert recharge["verified"] is True
    assert recharge["modification"]["soc_initial"] == 0.325
    assert recharge["preview"]["soc_final"] >= 0.20
    assert "需 A 批准" not in json.dumps(blocked, ensure_ascii=False)
    assert rerun["status"] == "ok"
    assert rerun["request"]["soc_initial"] == 0.325
    assert rerun["report"]["summary"]["soc_initial"]["value"] == 0.325
    assert rerun["replan_count"] == 1
    assert any(
        item["node"] == "apply_adjustment" and item["iteration"] == 1
        for item in rerun["trace"]
    )


def test_resume_rejects_invalid_and_repeated_decisions():
    with _client() as client:
        blocked = client.post(
            "/api/run", json={"task_text": NORMAL_TEXT.replace("6小时", "1小时")},
        ).json()
        base = {
            "thread_id": blocked["thread_id"],
            "decision_id": blocked["decision"]["decision_id"],
        }
        invalid = client.post("/api/resume", json={**base, "option_id": "unsafe"})
        accepted = client.post("/api/resume", json={**base, "option_id": "accept_late"})
        repeated = client.post("/api/resume", json={**base, "option_id": "accept_late"})
    assert invalid.status_code == 409
    assert invalid.json()["error"] == "invalid_adjustment_option"
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "ok"
    assert repeated.status_code == 409
    assert repeated.json()["error"] == "workflow_not_awaiting_choice"


def test_sqlite_checkpoint_survives_app_recreation(tmp_path):
    database = tmp_path / "checkpoints.sqlite3"
    first_app = create_app(checkpoint_backend="sqlite", checkpoint_sqlite_path=database)
    with TestClient(first_app) as client:
        blocked = client.post(
            "/api/run", json={"task_text": NORMAL_TEXT.replace("6小时", "1小时")},
        ).json()
    second_app = create_app(checkpoint_backend="sqlite", checkpoint_sqlite_path=database)
    with TestClient(second_app) as client:
        restored = client.get(f"/api/runs/{blocked['thread_id']}")
        resumed = client.post("/api/resume", json={
            "thread_id": blocked["thread_id"],
            "decision_id": blocked["decision"]["decision_id"],
            "option_id": "accept_late",
        })
    assert restored.status_code == 200
    assert restored.json()["status"] == "awaiting_choice"
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "ok"


def test_missing_llm_config_and_unexpected_workflow_error_are_safe(monkeypatch):
    def missing_config():
        raise LLMConfigError("SHIP_LLM_API_KEY is not configured")

    monkeypatch.setattr("ui.web_server.LLMConfig.from_env", missing_config)
    client, mode = _load_llm(True)
    assert client is None
    assert mode == "config_unavailable_template_fallback"

    async def fail_workflow(*_args, **_kwargs):
        raise RuntimeError("private internal detail")

    with _client() as web_client:
        monkeypatch.setattr(web_client.app.state.workflow_runtime, "start_text", fail_workflow)
        response = web_client.post("/api/run", json={"task_text": NORMAL_TEXT})
    assert response.status_code == 500
    assert response.json() == {"status": "failed", "error": "workflow_failed"}
    assert "private internal detail" not in response.text


def test_browser_opens_only_after_service_accepts_connections():
    calls = []

    class Connection:
        def close(self):
            calls.append("closed")

    def connect(address, timeout):
        calls.append((address, timeout))
        return Connection()

    def open_page(url):
        calls.append(url)
        return True

    _open_browser_when_ready(
        "127.0.0.1", 8765, browser_open=open_page, connector=connect,
    )
    assert calls == [(("127.0.0.1", 8765), 0.25), "closed", "http://127.0.0.1:8765"]


def test_windows_launcher_contains_required_safe_sequence():
    root = ROOT.parents[1]
    cmd = (root / "start_local_demo.cmd").read_text(encoding="utf-8")
    script = (root / "scripts/start_local_demo.ps1").read_text(encoding="utf-8")
    script.encode("ascii")  # Windows PowerShell 5.1 misreads UTF-8 without a BOM.
    assert "start_local_demo.ps1" in cmd
    for marker in (
        "TcpClient", "pip check", "--smoke-test", "--llm", "--open-browser",
        "127.0.0.1", "8765", "Keep this window open", "Ctrl+C",
    ):
        assert marker in script
    assert "-WindowStyle Hidden" not in script
