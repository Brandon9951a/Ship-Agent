import json

from fastapi.testclient import TestClient

from core.llm_layer import LLMConfigError
from ui.web_server import (
    MAX_REQUEST_BYTES, REFERENCE_BACKGROUND, ROOT, _load_llm, create_app,
)


NORMAL_TEXT = (
    "从平顶山港到军李船闸，2026-09-18 09:00出发，"
    "SOC85%，半载，6小时内到达"
)


def _client(*, llm_mode="disabled"):
    return TestClient(create_app(llm_mode=llm_mode))


def test_frontend_assets_exist_and_have_core_surfaces():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "app.css").read_text(encoding="utf-8")
    cockpit = (ROOT / "cockpit.css").read_text(encoding="utf-8")
    js = (ROOT / "app.js").read_text(encoding="utf-8")
    for label in (
        "任务描述", "按结构化参数运行", "航线与分段方案", "推进能耗",
        "辅助能耗", "ETA", "推荐航速与能耗", "安全校验",
        "MODEL EXPLANATION", "下一步",
    ):
        assert label in html
    assert "@media" in css
    assert "vessel-ocean-background.jpg" in cockpit
    assert REFERENCE_BACKGROUND.is_file()
    for marker in ("/api/run", "/healthz", "task_text", "runStructured"):
        assert marker in js
    assert "const segments = okay ?" in js
    assert "result.task_understanding?.mode === 'llm_qualitative'" in js


def test_index_health_static_and_security_headers():
    with _client(llm_mode="enabled") as client:
        index = client.get("/")
        assert index.status_code == 200
        assert "绿航智算" in index.text
        assert index.headers["x-frame-options"] == "DENY"
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/static/cockpit.css").status_code == 200
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


def test_windows_launcher_contains_required_safe_sequence():
    root = ROOT.parents[1]
    cmd = (root / "start_local_demo.cmd").read_text(encoding="utf-8")
    script = (root / "scripts/start_local_demo.ps1").read_text(encoding="utf-8")
    assert "start_local_demo.ps1" in cmd
    for marker in (
        "Get-NetTCPConnection", "pip check", "--smoke-test", "--llm",
        "127.0.0.1", "8765", "/healthz", "Start-Process", "-WindowStyle Hidden",
    ):
        assert marker in script
