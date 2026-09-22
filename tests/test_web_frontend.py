import json

from fastapi.testclient import TestClient

from core.llm_layer import LLMCallResult, LLMConfigError
from ui.web_server import (
    MAX_REQUEST_BYTES, REFERENCE_BACKGROUND, REFERENCE_STYLESHEET, ROOT,
    _load_llm, _open_browser_when_ready, create_app,
)


NORMAL_TEXT = (
    "从平顶山港到军李船闸，2026-09-18 09:00出发，"
    "SOC85%，半载，6小时内到达"
)


def _client(*, llm_mode="disabled"):
    return TestClient(create_app(llm_mode=llm_mode))


def test_frontend_assets_exist_and_have_core_surfaces():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    css = REFERENCE_STYLESHEET.read_text(encoding="utf-8")
    js = (ROOT / "app.js").read_text(encoding="utf-8")
    for label in (
        "智行合一 · 船舶航速优化与能效管理智能决策系统",
        "任务描述", "快捷输入", "工况补充", "航线与分段方案",
        "航段能耗与速度", "推荐航速方案", "安全校验", "能量管理建议",
        "完整详细回复", "工具调用过程",
    ):
        assert label in html
    assert "@media" in css
    assert "vessel-ocean-background.jpg" in css
    assert REFERENCE_BACKGROUND.is_file()
    for marker in (
        "/api/run", "/healthz", "task_text", "DeepSeek 已真实调用",
        "Tdata", "Tseg", "Tenergy", "Tspeed", "Tmanagement",
    ):
        assert marker in js
    assert "const understandingMode = result.task_understanding?.mode" in js
    for removed in ("Arduino", "语音输入", "/api/hardware", "/api/voice", "/api/infer"):
        assert removed not in html
        assert removed not in js


def test_index_health_static_and_security_headers():
    with _client(llm_mode="enabled") as client:
        index = client.get("/")
        assert index.status_code == 200
        assert "智行合一" in index.text
        assert index.headers["x-frame-options"] == "DENY"
        assert client.get("/static/app.js").status_code == 200
        stylesheet = client.get("/static/app.css")
        assert stylesheet.status_code == 200
        assert stylesheet.content == REFERENCE_STYLESHEET.read_bytes()
        assert client.get("/static/vessel-ocean-background.jpg").status_code == 200
        assert client.get("/static/unknown.txt").status_code == 404
        health = client.get("/healthz").json()
    assert health == {
        "status": "ok",
        "service": "ship-agent-web",
        "scope": "synthetic_demo",
        "real_ship_validation": False,
        "llm_mode": "enabled",
    }


def test_web_request_really_uses_configured_llm_client():
    class FakeClient:
        def __init__(self):
            self.calls = 0

        def complete_text(self, *_args, **_kwargs):
            self.calls += 1
            return LLMCallResult(
                "ok", "deepseek", "deepseek-v4-pro",
                text="该结果仅用于软件仿真，工程数值以工具计算为准。",
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
        assert result["status"] == "infeasible"
        assert result["tspeed"]["infeasible_type"] == expected_type
        assert "report" not in result
        assert result["adjustment_options"]
        assert "未显示航速推荐、ETA、最终能耗或 SOC 成功结论" in result["dashboard"]


def test_missing_llm_config_and_unexpected_workflow_error_are_safe(monkeypatch):
    def missing_config():
        raise LLMConfigError("SHIP_LLM_API_KEY is not configured")

    monkeypatch.setattr("ui.web_server.LLMConfig.from_env", missing_config)
    client, mode = _load_llm(True)
    assert client is None
    assert mode == "config_unavailable_template_fallback"

    def fail_workflow(*_args, **_kwargs):
        raise RuntimeError("private internal detail")

    monkeypatch.setattr("ui.web_server.run_text", fail_workflow)
    with _client() as web_client:
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
