"""Task 3 at the web layer: health, queue bounds, cancel/retry and post-restart reads.

模拟“服务关闭再启动”的方式：用同一 research_directory 新建第二个 app，
第二个 AgentRuntime 会执行启动恢复；页面按 thread_id 取回同一任务。
"""
import json
import time
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.test_web_frontend import AuthenticatedClient, _store
from tests.test_research_agent import ScriptedModel
from ui.web_server import create_app


def _app(tmp_path, model, name="research", auth_name=None):
    return create_app(llm_client=model, checkpoint_backend="memory",
                      research_directory=tmp_path / name,
                      auth_store=_store(tmp_path / f"auth-{auth_name or name}.sqlite3"))


def _wait(client, task, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/runs/{task}").json()
        if body.get("status") in ("completed", "failed", "cancelled", "waiting_human"):
            return body
        time.sleep(0.2)
    raise AssertionError(f"task {task} did not reach a terminal state")


class BlockingModel(ScriptedModel):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def complete_chat(self, messages, **kwargs):
        self.started.set()
        self.release.wait(30)
        return super().complete_chat(messages, **kwargs)


def test_healthz_answers_while_task_is_running(tmp_path):
    model = BlockingModel()
    with AuthenticatedClient(_app(tmp_path, model)) as client:
        try:
            response = client.post("/api/run", json={
                "workflow_mode": "research_agent", "task_text": "从漯河船闸到漯河港"})
            assert response.status_code == 202
            task = response.json()["task_id"]
            assert model.started.wait(10)
            assert client.get("/healthz").json() == {"status": "ok", "service": "ship-agent-web"}
            body = client.get(f"/api/runs/{task}").json()
            assert body["status"] == "running"
            assert body["phase"]
        finally:
            model.release.set()
            time.sleep(0.2)


def test_queue_limit_maps_to_429(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIP_AGENT_MAX_ACTIVE_TASKS", "1")
    model = BlockingModel()
    with AuthenticatedClient(_app(tmp_path, model)) as client:
        try:
            first = client.post("/api/run", json={
                "workflow_mode": "research_agent", "task_text": "任务一"})
            assert first.status_code == 202
            assert model.started.wait(10)
            second = client.post("/api/run", json={
                "workflow_mode": "research_agent", "task_text": "任务二"})
            assert second.status_code == 429
            body = second.json()
            assert body["error"] == "research_queue_full"
            assert body["limit"] == 1 and body["retry_after_s"] >= 1
        finally:
            model.release.set()
            time.sleep(0.2)


def test_cancel_api_marks_task_cancelled(tmp_path):
    model = BlockingModel()
    with AuthenticatedClient(_app(tmp_path, model)) as client:
        try:
            task = client.post("/api/run", json={
                "workflow_mode": "research_agent", "task_text": "从漯河船闸到漯河港"}).json()["task_id"]
            assert model.started.wait(10)
            cancelled = client.post(f"/api/runs/{task}/cancel")
            assert cancelled.status_code == 200
            model.release.set()
            body = _wait(client, task)
            assert body["status"] == "cancelled"
            assert body["outcome_code"] == "task_cancelled"
            events = client.get(f"/api/runs/{task}/events").json()["events"]
            assert any(event["type"] == "cancel_requested" for event in events)
            assert not any(event["type"] == "plan_verified" for event in events)
        finally:
            model.release.set()
            time.sleep(0.2)


def test_retry_api_recovers_failed_task(tmp_path):
    with AuthenticatedClient(_app(tmp_path, None)) as client:
        task = client.post("/api/run", json={
            "workflow_mode": "research_agent", "task_text": "从漯河船闸到漯河港"}).json()["task_id"]
        first = _wait(client, task)
        assert first["status"] == "failed"
        assert first["outcome_code"] == "model_unavailable"
    with AuthenticatedClient(_app(tmp_path, ScriptedModel())) as client:
        response = client.post(f"/api/runs/{task}/retry")
        assert response.status_code == 202
        body = _wait(client, task)
        assert body["status"] == "completed"
        assert body["outcome_code"] == "research_feasible"
        events = client.get(f"/api/runs/{task}/events").json()["events"]
        assert sum(1 for event in events if event["type"] == "answer_completed") == 1


def test_restart_returns_same_task_status_events_and_answer(tmp_path):
    directory = "research-shared"
    with AuthenticatedClient(_app(tmp_path, ScriptedModel(), name=directory)) as client:
        task = client.post("/api/run", json={
            "workflow_mode": "research_agent", "task_text": "从漯河船闸到漯河港"}).json()["task_id"]
        first = _wait(client, task)
        assert first["status"] == "completed"
        first_events = client.get(f"/api/runs/{task}/events").json()["events"]
        answer_ref = first["answer_ref"]
    # 第二个 app 用同一 research 目录 = 服务重启；启动恢复不会改动已完成任务。
    with AuthenticatedClient(_app(tmp_path, ScriptedModel(), name=directory)) as client:
        again = client.get(f"/api/runs/{task}").json()
        assert again["status"] == "completed"
        assert again["answer_ref"] == answer_ref
        assert again["final_answer"] == first["final_answer"]
        events = client.get(f"/api/runs/{task}/events").json()["events"]
        assert len(events) == len(first_events), "重启不应追加重复事件"
        artifact = client.get(f"/api/runs/{task}/artifacts/{answer_ref}")
        assert artifact.status_code == 200


def test_relogin_after_restart_sees_same_task(tmp_path):
    """重启后重新登录：同一任务的状态、事件与答复仍可取回。"""
    auth = "shared-auth"
    direct = "research-relogin"
    with AuthenticatedClient(_app(tmp_path, ScriptedModel(), name=direct, auth_name=auth)) as client:
        task = client.post("/api/run", json={
            "workflow_mode": "research_agent", "task_text": "从漯河船闸到漯河港"}).json()["task_id"]
        first = _wait(client, task)
        assert first["status"] == "completed"
        answer_ref = first["answer_ref"]
    with AuthenticatedClient(_app(tmp_path, ScriptedModel(), name=direct, auth_name=auth)) as client:
        assert client.get("/api/auth/me").status_code == 200, "重启后应能重新登录"
        again = client.get(f"/api/runs/{task}").json()
        assert again["status"] == "completed"
        assert again["answer_ref"] == answer_ref
        assert again["final_answer"] == first["final_answer"]


def test_restart_completes_a_task_interrupted_while_running(tmp_path):
    """运行中服务重启：第二个 app 的启动恢复继续同一任务并给出最终答复。"""
    directory = "research-restart"

    class CrashAfterFirstCall(ScriptedModel):
        def __init__(self):
            super().__init__()
            self.count = 0

        def complete_chat(self, messages, **kwargs):
            self.count += 1
            if self.count >= 2:
                raise SystemExit("simulated_process_kill")
            return super().complete_chat(messages, **kwargs)

    with AuthenticatedClient(_app(tmp_path, CrashAfterFirstCall(), name=directory)) as client:
        task = client.post("/api/run", json={
            "workflow_mode": "research_agent", "task_text": "从漯河船闸到漯河港"}).json()["task_id"]
        deadline = time.time() + 20
        while time.time() < deadline:
            body = client.get(f"/api/runs/{task}").json()
            if body["status"] == "running":
                break
            time.sleep(0.2)
        assert body["status"] == "running"
    with AuthenticatedClient(_app(tmp_path, ScriptedModel(), name=directory)) as client:
        final = _wait(client, task, timeout=90)
        assert final["status"] == "completed"
        assert final["outcome_code"] == "research_feasible"
        events = client.get(f"/api/runs/{task}/events").json()["events"]
        recovered = [e for e in events if e["type"] == "recovery_scheduled"]
        assert recovered and recovered[0]["path"] == "resume_from_checkpoint"
        assert sum(1 for e in events if e["type"] == "answer_completed") == 1
