"""Task 3: restart recovery, cancellation, retry and single-publish guarantees.

验收标准（docs/协作/三项稳定性改进任务与验收标准_2026-10-17.md 任务 3）：
1. 分别在排队、Tspeed 搜索和 FDP 求解阶段中断服务并重启；同一任务继续或安全
   重试后到达明确终态，事件与工件无重复冲突；不能一律标为 process_interrupted。
2. 中断时刷新页面、恢复登录并重开历史任务，能够看到同一任务的最新状态与最终答复；
   等待补充任务的同一回答只应用一次。
3. 连续提交多任务时服务仍响应 /healthz，任务有排队与超时边界。
"""
import json
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from research.agent.runtime import AgentRuntime, RuntimeLimits, RuntimeQueueFull
from research.agent.store import Store, now
from tests.test_research_agent import ScriptedModel


def _queued_record(task_id, text="从漯河船闸到漯河港"):
    return {"thread_id": task_id, "task_id": task_id, "workflow_mode": "research_agent",
            "status": "queued", "task_text": text, "scenario_id": "route_success",
            "task_type": "auto", "created_at": now(), "revision": 1,
            "queue_entered_at": now(), "recovery_attempts": 0}


class BlockingModel(ScriptedModel):
    """在第一次决策前阻塞，用于模拟“计算中”任务。"""

    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def complete_chat(self, messages, **kwargs):
        self.started.set()
        self.release.wait(30)
        return super().complete_chat(messages, **kwargs)


class CrashModel(ScriptedModel):
    """第 N 次模型调用时抛 BaseException，模拟进程被杀（不写失败终态）。"""

    def __init__(self, crash_at=3):
        super().__init__()
        self.crash_calls = 0
        self.crash_at = crash_at

    def complete_chat(self, messages, **kwargs):
        self.crash_calls += 1
        if self.crash_calls >= self.crash_at:
            raise SystemExit("simulated_process_kill")
        return super().complete_chat(messages, **kwargs)


def test_queued_task_is_restarted_after_restart(tmp_path):
    """排队中断：重启后从原始输入快照安全重试，而不是 process_interrupted。"""
    task = str(uuid4())
    store = Store(tmp_path)
    store.create(task, "local", _queued_record(task))
    store.close()

    runtime = AgentRuntime(tmp_path, ScriptedModel())
    try:
        runtime.futures[task].result(timeout=30)
        record = runtime.store.get(task)
        assert record["status"] == "completed"
        assert record["outcome_code"] == "research_feasible"
        assert record.get("recovery_attempts") == 1
        events = runtime.store.events(task)
        scheduled = [e for e in events if e["type"] == "recovery_scheduled"]
        assert scheduled and scheduled[0]["path"] == "restart_from_input_snapshot"
        assert scheduled[0]["previous_status"] == "queued"
        assert not any(e["type"] == "run_failed" for e in events)
        assert not any(e.get("outcome_code") == "process_interrupted" for e in events)
    finally:
        runtime.close()


def test_running_task_resumes_from_checkpoint_after_kill(tmp_path):
    """计算中断：进程被杀后从 LangGraph 检查点继续，不重新开始也不重复发布。"""
    runtime = AgentRuntime(tmp_path, CrashModel())
    with pytest.raises(SystemExit):
        runtime.start("从漯河船闸到漯河港", background=False)
    task_id = runtime.store.db.execute("SELECT id FROM tasks").fetchone()[0]
    interrupted = runtime.store.get(task_id)
    assert interrupted["status"] == "running", "被杀时不写失败终态，留待恢复"
    runtime.close()

    recovered = AgentRuntime(tmp_path, ScriptedModel())
    try:
        recovered.futures[task_id].result(timeout=60)
        record = recovered.store.get(task_id)
        assert record["status"] == "completed"
        assert record["outcome_code"] == "research_feasible"
        events = recovered.store.events(task_id)
        scheduled = [e for e in events if e["type"] == "recovery_scheduled"]
        assert scheduled and scheduled[0]["path"] == "resume_from_checkpoint"
        assert scheduled[0]["has_checkpoint"] is True
        resumed = [e for e in events if e["type"] == "run_started"]
        assert any(e.get("resumed") for e in resumed), "应从检查点续跑"
        assert sum(1 for e in events if e["type"] == "answer_completed") == 1
        assert sum(1 for e in events if e["type"] == "plan_verified") == 1
    finally:
        recovered.close()


def test_completed_task_never_publishes_second_answer(tmp_path):
    """重复执行保护：已完成的答复只发布一次，重放 finish 节点直接复用。"""
    runtime = AgentRuntime(tmp_path, ScriptedModel())
    try:
        result = runtime.start("从漯河船闸到漯河港", background=False)
        task = result["task_id"]
        state = {"task_id": task, "messages": [{"role": "user", "content": "从漯河船闸到漯河港"}],
                 "observations": [], "refs": {}, "outcomes": {}, "request": {},
                 "task_type": "planning", "scenario_id": "route_success", "metrics": {}}
        assert runtime._finish(dict(state)) == {}, "第二次 finish 必须短路"
        events = runtime.store.events(task)
        assert sum(1 for e in events if e["type"] == "answer_completed") == 1
        assert sum(1 for e in events if e["type"] == "answer_reused") == 1
        assert runtime.store.get(task)["status"] == "completed"
    finally:
        runtime.close()


def test_cancel_running_task_stops_without_publishing(tmp_path):
    """取消运行中任务：到达取消终态、不发布方案、事件留痕。"""
    model = BlockingModel()
    runtime = AgentRuntime(tmp_path, model)
    try:
        result = runtime.start("从漯河船闸到漯河港", background=True)
        task = result["task_id"]
        assert model.started.wait(10), "任务应已进入计算"
        runtime.cancel(task, "local", reason="user_cancelled")
        model.release.set()
        runtime.futures[task].result(timeout=60)
        record = runtime.store.get(task)
        assert record["status"] == "cancelled"
        assert record["outcome_code"] == "task_cancelled"
        assert record.get("answer_ref") is None
        events = runtime.store.events(task)
        assert any(e["type"] == "cancel_requested" for e in events)
        assert not any(e["type"] == "plan_verified" for e in events)
    finally:
        model.release.set()
        runtime.close()


def test_task_timeout_uses_distinct_outcome(tmp_path):
    """超时与用户取消分开报告，且都不发布方案。"""
    model = BlockingModel()
    limits = RuntimeLimits(task_timeout_s=0.2, queue_timeout_s=60.0,
                           watchdog_interval_s=0.05, max_active_tasks=8)
    runtime = AgentRuntime(tmp_path, model, limits=limits)
    try:
        result = runtime.start("从漯河船闸到漯河港", background=True)
        task = result["task_id"]
        assert model.started.wait(10)
        deadline = time.time() + 20
        while time.time() < deadline:
            events = runtime.store.events(task)
            if any(e["type"] == "task_timeout" for e in events):
                break
            time.sleep(0.1)
        else:
            pytest.fail("看门狗未记录 task_timeout")
        model.release.set()
        runtime.futures[task].result(timeout=60)
        record = runtime.store.get(task)
        assert record["status"] == "cancelled"
        assert record["outcome_code"] == "task_timeout"
        assert record.get("answer_ref") is None
    finally:
        model.release.set()
        runtime.close()


def test_active_queue_limit_rejects_new_task(tmp_path):
    """并发提交有上限，返回可重试的队列满错误而不是静默堆积。"""
    model = BlockingModel()
    limits = RuntimeLimits(max_active_tasks=1, task_timeout_s=60.0,
                           watchdog_interval_s=0.05)
    runtime = AgentRuntime(tmp_path, model, limits=limits)
    try:
        first = runtime.start("从漯河船闸到漯河港", background=True)
        assert model.started.wait(10)
        with pytest.raises(RuntimeQueueFull) as excinfo:
            runtime.start("再提交一个任务", background=True)
        assert excinfo.value.code == "research_queue_full"
        assert excinfo.value.limit == 1
        model.release.set()
        runtime.futures[first["task_id"]].result(timeout=60)
    finally:
        model.release.set()
        runtime.close()


def test_retry_after_model_unavailable_reuses_task(tmp_path):
    """模型不可用后重试：同一任务到达终态，答复只发布一次。"""
    runtime = AgentRuntime(tmp_path, None)
    try:
        result = runtime.start("从漯河船闸到漯河港", background=False)
        task = result["task_id"]
        assert result["status"] == "failed" and result["outcome_code"] == "model_unavailable"
    finally:
        runtime.close()

    recovered = AgentRuntime(tmp_path, ScriptedModel())
    try:
        recovered.retry(task, "local", background=True)
        recovered.futures[task].result(timeout=60)
        record = recovered.store.get(task)
        assert record["status"] == "completed"
        assert record["outcome_code"] == "research_feasible"
        assert record["manual_retries"] == 1
        events = recovered.store.events(task)
        assert any(e["type"] == "retry_scheduled" for e in events)
        assert sum(1 for e in events if e["type"] == "answer_completed") == 1
    finally:
        recovered.close()


def test_retry_after_cancel_restarts_from_input(tmp_path):
    """取消后图已到 END，重试必须从输入快照重跑，而不是空转 invoke(None)。"""
    model = BlockingModel()
    runtime = AgentRuntime(tmp_path, model)
    try:
        task = runtime.start("从漯河船闸到漯河港")["task_id"]
        assert model.started.wait(10)
        runtime.cancel(task, "local", reason="user_cancelled")
        model.release.set()
        runtime.futures[task].result(timeout=60)
        assert runtime.store.get(task)["status"] == "cancelled"
        runtime.retry(task, "local")
        runtime.futures[task].result(timeout=60)
        record = runtime.store.get(task)
        assert record["status"] == "completed", record
        assert record["outcome_code"] == "research_feasible"
        events = runtime.store.events(task)
        assert any(e["type"] == "retry_scheduled"
                   and e["path"] == "restart_from_input_snapshot" for e in events)
        assert sum(1 for e in events if e["type"] == "answer_completed") == 1
    finally:
        model.release.set()
        runtime.close()


@pytest.mark.parametrize("method,tool", [("speed", "Tspeed"), ("management", "Tmanagement")])
def test_crash_during_tool_execution_replays_from_checkpoint(tmp_path, monkeypatch, method, tool):
    """在 Tspeed 搜索 / FDP 求解阶段中断：重启后重放该节点，不重算已完成工具。"""
    from research.agent.professional import ProfessionalTools
    original = getattr(ProfessionalTools, method)
    calls = {"count": 0}

    def crashing(self, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise SystemExit(f"simulated_kill_during_{method}")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ProfessionalTools, method, crashing)
    runtime = AgentRuntime(tmp_path, ScriptedModel())
    with pytest.raises(SystemExit):
        runtime.start("从漯河船闸到漯河港", background=False)
    task = runtime.store.db.execute("SELECT id FROM tasks").fetchone()[0]
    assert runtime.store.get(task)["status"] == "running"
    runtime.close()
    monkeypatch.undo()

    recovered = AgentRuntime(tmp_path, ScriptedModel())
    try:
        recovered.futures[task].result(timeout=120)
        record = recovered.store.get(task)
        assert record["status"] == "completed", record
        assert record["outcome_code"] == "research_feasible"
        events = recovered.store.events(task)
        assert any(e["type"] == "recovery_scheduled"
                   and e["path"] == "resume_from_checkpoint" for e in events)
        completed = [e for e in events
                     if e["type"] == "tool_completed" and e.get("tool") == tool]
        assert len(completed) == 1, "崩溃点所在工具应只完成一次"
        assert sum(1 for e in events if e["type"] == "answer_completed") == 1
    finally:
        recovered.close()


def test_two_runtimes_never_execute_same_task_twice(tmp_path):
    """两个进程同时恢复同一任务：原子领取只允许一个执行，答复只发布一次。"""
    task = str(uuid4())
    store = Store(tmp_path)
    store.create(task, "local", _queued_record(task))
    store.close()

    first_model, second_model = BlockingModel(), BlockingModel()
    first = AgentRuntime(tmp_path, first_model)
    second = AgentRuntime(tmp_path, second_model)
    try:
        assert first_model.started.wait(10) or second_model.started.wait(10)
        first_model.release.set()
        second_model.release.set()
        for runtime in (first, second):
            future = runtime.futures.get(task)
            if future is not None:
                future.result(timeout=90)
        record = first.store.get(task)
        assert record["status"] == "completed"
        events = first.store.events(task)
        assert sum(1 for e in events if e["type"] == "answer_completed") == 1
        assert any(e["type"] == "recovery_skipped" and e.get("reason") == "worker_still_running"
                   for e in events), "在跑的 worker 不应被另一个运行时接管"
    finally:
        first_model.release.set()
        second_model.release.set()
        first.close()
        second.close()


def test_queued_timeout_marks_failed_without_execution(tmp_path):
    """排队超时：未开始计算就落 queue_timeout，且不发布方案。"""
    limits = RuntimeLimits(queue_timeout_s=0.2, task_timeout_s=60.0,
                           watchdog_interval_s=0.05, max_active_tasks=8)
    runtime = AgentRuntime(tmp_path, ScriptedModel(), limits=limits)
    try:
        task = str(uuid4())
        record = _queued_record(task)
        record["queue_entered_at"] = "2026-01-01T00:00:00+00:00"
        runtime.store.create(task, "local", record)
        deadline = time.time() + 20
        current = None
        while time.time() < deadline:
            current = runtime.store.get(task)
            if current["status"] == "failed":
                break
            time.sleep(0.1)
        assert current and current["status"] == "failed", current
        assert current["outcome_code"] == "queue_timeout"
        assert current.get("answer_ref") is None
        events = runtime.store.events(task)
        assert any(e["type"] == "queue_timeout" for e in events)
        assert not any(e["type"] == "run_started" for e in events)
    finally:
        runtime.close()


def test_recovery_attempts_are_bounded(tmp_path):
    """反复中断不会无限重试：超过上限落为 recovery_exhausted。"""
    task = str(uuid4())
    store = Store(tmp_path)
    record = _queued_record(task)
    record.update(recovery_attempts=3)
    store.create(task, "local", record)
    store.close()
    runtime = AgentRuntime(tmp_path, ScriptedModel())
    try:
        record = runtime.store.get(task)
        assert record["status"] == "failed"
        assert record["outcome_code"] == "recovery_exhausted"
        events = runtime.store.events(task)
        assert any(e["type"] == "recovery_exhausted" for e in events)
    finally:
        runtime.close()
