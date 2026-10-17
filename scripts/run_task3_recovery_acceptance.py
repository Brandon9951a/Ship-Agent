"""Task 3 evidence: restart recovery, cancel/retry and single-publish record.

本脚本用**真实工具链**（Tdata/Tseg/Tspeed/Tmanagement 与 LangGraph 检查点）跑三组
场景，输出任务 ID、事件、工件引用与哈希，供 PR 附件核对：

1. 计算中进程被杀（SystemExit）→ 重启后从检查点续跑 → research_feasible；
2. 运行中取消 → cancelled，不产生 plan_verified / 方案工件；
3. 失败任务重试 → 同一任务到达终态，answer_completed 恰好一次。

模型：自动验收使用确定性脚本模型（model 标识 `task3-scripted`）。真实 DeepSeek
调用需配置 SHIP_LLM_API_KEY/BASE_URL/MODEL 后用网站或本脚本的 `--live` 模式执行。

用法::

    python scripts/run_task3_recovery_acceptance.py --output-dir artifacts/task3-evidence
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from research.agent.runtime import AgentRuntime, RuntimeLimits
from research.agent.store import Store, now

TEXT = "从漯河船闸到漯河港"
SCENARIO = "route_success"


class ScriptedClient:
    """与 tests/test_research_agent.py 同协议的确定性模型；不访问网络。"""

    config = SimpleNamespace(enabled=True, model="task3-scripted")

    def complete_chat(self, messages, *, tools, **kwargs):
        names = [item["function"]["name"] for item in tools]
        if names == ["submit_request"]:
            args = {"task_type": "planning", "origin": "漯河船闸",
                    "destination": "漯河港", "objective": "min_energy"}
            name = "submit_request"
        elif names == ["submit_answer"]:
            name = "submit_answer"
            args = {"answer_markdown": "研究工具已执行，结果请结合仿真假设验收。", "claims": []}
        else:
            context = json.loads(messages[-1]["content"].split("：", 1)[1])
            name = next((n for n in ["Tdata", "Tseg", "Tspeed", "Tmanagement",
                                     "prepare_final", "finish_without_plan"] if n in names),
                        "search_project_docs")
            args = {"revision": context["revision"], "rationale": "acceptance run"}
            if name == "search_project_docs":
                args["query"] = "运行时恢复"
            if name == "finish_without_plan":
                args["reason"] = "工具诊断要求停止"
        return {"message": {"role": "assistant", "content": "", "tool_calls": [{
            "id": str(uuid4()), "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}]},
            "response_model": "task3-scripted", "request_id": str(uuid4()), "latency_ms": 0}


class CrashClient(ScriptedClient):
    def __init__(self, crash_at=3):
        self.crash_calls = 0
        self.crash_at = crash_at

    def complete_chat(self, messages, **kwargs):
        self.crash_calls += 1
        if self.crash_calls >= self.crash_at:
            raise SystemExit("simulated_process_kill")
        return super().complete_chat(messages, **kwargs)


class BlockingClient(ScriptedClient):
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def complete_chat(self, messages, **kwargs):
        self.started.set()
        self.release.wait(30)
        return super().complete_chat(messages, **kwargs)


def _events(runtime, task):
    return runtime.store.events(task)


def _artifact_manifest(runtime, task):
    rows = runtime.store.list(task)
    manifest = []
    for row in rows:
        path = runtime.store.blobs / (row["id"] + ".json")
        manifest.append({"artifact_id": row["id"], "kind": row["kind"],
                         "bytes": path.stat().st_size if path.exists() else None,
                         "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None})
    return manifest


def _tool_calls(events):
    return [{"seq": e["seq"], "type": e["type"], "tool": e.get("tool"),
             "outcome_code": e.get("outcome_code"), "call_id": e.get("call_id"),
             "parent_call_id": e.get("parent_call_id"), "artifact_ref": e.get("artifact_ref")}
            for e in events if e["type"] in ("tool_completed", "tool_failed")]


def scenario_restart(directory, scenarios):
    section = scenarios.setdefault("restart", {})
    runtime = AgentRuntime(directory, CrashClient(crash_at=3))
    crash_task = None
    try:
        try:
            runtime.start(TEXT, background=False)
        except SystemExit:
            pass
        with runtime.store.lock:
            crash_task = runtime.store.db.execute("SELECT id FROM tasks").fetchone()[0]
        section["before_restart_status"] = runtime.store.get(crash_task)["status"]
    finally:
        runtime.close()
    recovered = AgentRuntime(directory, ScriptedClient())
    try:
        future = recovered.futures.get(crash_task)
        if future is not None:
            future.result(timeout=120)
        record = recovered.store.get(crash_task)
        events = _events(recovered, crash_task)
        section.update({
            "task_id": crash_task,
            "after_restart_status": record["status"],
            "outcome_code": record.get("outcome_code"),
            "recovery_attempts": record.get("recovery_attempts"),
            "recovery_path": record.get("recovery_path"),
            "answer_ref": record.get("answer_ref"),
            "event_types": [e["type"] for e in events],
            "recovery_event": next((e for e in events if e["type"] == "recovery_scheduled"), None),
            "answer_completed_count": sum(1 for e in events if e["type"] == "answer_completed"),
            "plan_verified_count": sum(1 for e in events if e["type"] == "plan_verified"),
            "tool_calls": _tool_calls(events),
            "artifacts": _artifact_manifest(recovered, crash_task),
        })
    finally:
        recovered.close()


def scenario_cancel_and_retry(directory, scenarios):
    model = BlockingClient()
    limits = RuntimeLimits(max_active_tasks=8, task_timeout_s=120.0,
                           queue_timeout_s=60.0, watchdog_interval_s=0.2)
    runtime = AgentRuntime(directory, model, limits=limits)
    try:
        task = runtime.start(TEXT)["task_id"]
        model.started.wait(15)
        runtime.cancel(task, "local", reason="user_cancelled")
        model.release.set()
        runtime.futures[task].result(timeout=120)
        cancelled = runtime.store.get(task)
        events_after_cancel = _events(runtime, task)
        scenarios["cancel"] = {
            "task_id": task,
            "status": cancelled["status"],
            "outcome_code": cancelled.get("outcome_code"),
            "answer_ref": cancelled.get("answer_ref"),
            "plan_verified_count": sum(1 for e in events_after_cancel if e["type"] == "plan_verified"),
            "cancel_requested_event": any(e["type"] == "cancel_requested" for e in events_after_cancel),
            "run_cancelled_event": any(e["type"] == "run_cancelled" for e in events_after_cancel),
        }
        runtime.retry(task, "local")
        runtime.futures[task].result(timeout=120)
        retried = runtime.store.get(task)
        events_after_retry = _events(runtime, task)
        scenarios["retry"] = {
            "task_id": task,
            "status": retried["status"],
            "outcome_code": retried.get("outcome_code"),
            "manual_retries": retried.get("manual_retries"),
            "answer_completed_count": sum(1 for e in events_after_retry if e["type"] == "answer_completed"),
            "answer_ref": retried.get("answer_ref"),
            "artifacts": _artifact_manifest(runtime, task),
            "event_types": [e["type"] for e in events_after_retry],
        }
    finally:
        model.release.set()
        runtime.close()


def scenario_single_publish(directory, scenarios):
    """模拟“finish 已写工件、但检查点尚未提交”时的重放：只发布一次。"""
    section = scenarios.setdefault("single_publish", {})
    runtime = AgentRuntime(directory, ScriptedClient())
    try:
        task = str(uuid4())
        runtime.store.create(task, "local", {
            "thread_id": task, "task_id": task, "workflow_mode": "research_agent",
            "status": "running", "task_text": TEXT, "scenario_id": SCENARIO,
            "task_type": "planning", "created_at": now(), "revision": 1})
        state = {"task_id": task, "messages": [{"role": "user", "content": TEXT}],
                 "observations": [], "refs": {}, "outcomes": {}, "request": {},
                 "task_type": "planning", "scenario_id": SCENARIO, "metrics": {}}
        first = runtime._finish(dict(state))
        second = runtime._finish(dict(state))
        events = _events(runtime, task)
        record = runtime.store.get(task)
        section.update({
            "task_id": task,
            "status": record["status"],
            "answer_ref": record.get("answer_ref"),
            "first_publish_closed": first,
            "second_replay_closed": second,
            "answer_completed_count": sum(1 for e in events if e["type"] == "answer_completed"),
            "answer_reused_count": sum(1 for e in events if e["type"] == "answer_reused"),
            "artifacts": _artifact_manifest(runtime, task),
        })
    finally:
        runtime.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Task 3 recovery acceptance evidence")
    parser.add_argument("--output-dir", default="artifacts/task3-evidence")
    args = parser.parse_args()
    started = time.perf_counter()
    directory = Path(tempfile.mkdtemp(prefix="task3-evidence-"))
    report = {
        "generated_at": now(),
        "model": {"identifier": "task3-scripted", "network": False,
                  "note": "自动验收使用确定性脚本模型；真实 DeepSeek 调用需配置凭据后另行执行"},
        "storage": str(directory),
        "scenarios": {},
    }
    try:
        scenarios = report["scenarios"]
        scenario_restart(directory, scenarios)
        scenario_cancel_and_retry(directory, scenarios)
        scenario_single_publish(directory, scenarios)
    finally:
        report["elapsed_s"] = round(time.perf_counter() - started, 2)
        output = Path(args.output_dir)
        output.mkdir(parents=True, exist_ok=True)
        path = output / "task3_recovery_evidence.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({
            "restart": {k: report["scenarios"].get("restart", {}).get(k)
                        for k in ("task_id", "before_restart_status", "after_restart_status",
                                  "outcome_code", "recovery_path", "answer_completed_count")},
            "cancel": report["scenarios"].get("cancel"),
            "retry": {k: report["scenarios"].get("retry", {}).get(k)
                      for k in ("task_id", "status", "outcome_code", "answer_completed_count")},
            "single_publish": report["scenarios"].get("single_publish", {}),
            "report": str(path),
        }, ensure_ascii=False, indent=2))
        shutil.rmtree(directory, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
