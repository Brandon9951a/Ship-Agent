"""Text UI for the real five-tool synthetic-demo workflow."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from core.llm_layer import LLMClient
from core.orchestrator import run_structured_workflow, run_workflow
from schemas.types import Status


ROOT = Path(__file__).resolve().parents[1]


def _source_text(source: dict[str, Any] | None) -> str:
    if not source:
        return "未提供来源"
    locator = f"；{source['locator']}" if source.get("locator") else ""
    verified = "已核对" if source.get("confirmed") else "待核实"
    return f"{source.get('source_id', '未知来源')}（{verified}{locator}）"


def _status_text(status: str) -> str:
    return {
        Status.OK.value: "工具完成，仍需以最终方案校验为准",
        Status.NEED_CLARIFICATION.value: "需要补充信息，已停止后续计算",
        Status.INFEASIBLE.value: "当前条件下不可行",
        Status.INVALID_INPUT.value: "输入或接口数据无效",
        Status.FAILED.value: "工具执行失败",
        Status.AWAITING_CHOICE.value: "等待用户选择调整方向",
    }.get(status, f"未知状态：{status}")


def _adjustment_options(record: dict[str, Any]) -> list[str]:
    """Return bounded user choices for an infeasible tool result."""
    if record.get("status") != Status.INFEASIBLE.value:
        return []
    kind = record.get("infeasible_type")
    if kind == "time":
        return [
            "放宽到达截止时间或最长耗时后重算",
            "选择较短的连续子航线后重算",
        ]
    if kind == "soc":
        return [
            "选择较短的连续子航线后重算",
            "补充有来源的充电地点、功率和可用性后重算",
            "调整初始 SOC 或载况后重算",
        ]
    if kind == "power":
        return [
            "选择候选航速范围内的较低功率方案后重算",
            "由 A/B 核对功率边界和模型口径后重算",
        ]
    return ["修改任务约束后重算", "选择连续子航线后重算"]


def render_dashboard(
    tool_results: list[dict[str, Any]], *, report: dict[str, Any] | None = None,
) -> str:
    """Render real tool states without manufacturing an engineering conclusion."""
    lines = ["航行任务文本界面", "=" * 20]
    final_status = Status.OK.value
    for record in tool_results:
        tool = record.get("tool", "未知工具")
        status = record.get("status", Status.FAILED.value)
        if status != Status.OK.value:
            final_status = status
        lines.extend((f"\n[{tool}] {status}", _status_text(status)))
        missing = record.get("missing_fields") or []
        questions = record.get("questions") or []
        if missing:
            lines.append("缺失字段：" + ", ".join(missing))
        for question in questions:
            lines.append("追问：" + question)
        if record.get("reason"):
            lines.append("原因：" + record["reason"])
        options = _adjustment_options(record)
        if options:
            lines.append("可选调整（需用户确认，不自动修改硬安全下限）：")
            lines.extend(f"- {index}. {option}" for index, option in enumerate(options, 1))
        payload = record.get("payload") or {}
        if tool == "Tdata" and payload:
            lines.append("路线：" + str(payload.get("route_id") or "未知"))
            for name, parameter in (payload.get("parameters") or {}).items():
                value, unit = parameter.get("value"), parameter.get("unit")
                lines.append(
                    f"参数 {name}: {value} {unit or ''}；"
                    f"{_source_text(parameter.get('source'))}"
                )
        elif tool == "Tseg" and payload.get("segments"):
            lines.append("航段（距离 km；速度 km/h；等待 h）：")
            for item in payload["segments"]:
                lines.append(
                    f"- {item['segment_id']}: {item['origin']} -> {item['destination']}，"
                    f"{item['distance_km']} km，限速 {item.get('max_speed_kmh')}，"
                    f"等待 {item.get('waiting_h')}；{_source_text(item.get('source'))}"
                )
        elif tool == "Tenergy" and payload.get("candidate_results"):
            lines.append("能耗候选（非最终优化/安全结论）：")
            for item in payload["candidate_results"]:
                lines.append(
                    f"- {item['segment_id']}: {item['speed_kmh']} km/h，"
                    f"{item['duration_h']} h，{item['energy_kwh']} kWh，"
                    f"口径 {item['energy_scope']}；模型 {item['model_id']}"
                )

    if report and report.get("status") == Status.OK.value and final_status == Status.OK.value:
        summary = report.get("summary") or {}
        lines.extend(("", "最终航行方案（synthetic_demo，仅软件仿真）："))
        for item in report.get("segments") or []:
            lines.append(
                f"- {item['origin']} -> {item['destination']}："
                f"{item['distance']['value']} {item['distance']['unit']}，"
                f"{item['speed']['value']} {item['speed']['unit']}，"
                f"{item['duration']['value']} {item['duration']['unit']}，"
                f"推进能耗 {item['propulsion_energy']['value']} "
                f"{item['propulsion_energy']['unit']}，"
                f"末端SOC {item['soc_end']['value']} {item['soc_end']['unit']}。"
            )
        for label, key in (
            ("ETA", "eta"), ("总需求能量", "required_energy"),
            ("初始SOC", "soc_initial"), ("结束SOC", "soc_final"),
        ):
            value = summary.get(key)
            if value:
                lines.append(f"{label}: {value.get('value')} {value.get('unit')}")
        warnings = report.get("warnings") or []
        lines.append("告警：" + ("；".join(warnings) if warnings else "无"))
        lines.append("工程数值来自工具；本结果不代表实船安全或运营批准。")
    elif final_status != Status.OK.value:
        lines.extend(("", "最终显示状态：" + final_status))
        lines.append("未显示航速推荐、ETA、最终能耗或 SOC 成功结论。")
    else:
        lines.extend(("", "尚未形成最终方案。"))
        lines.append("仍需 Tspeed、Tmanagement 和最终方案校验后，才能显示成功航行结果。")
    return "\n".join(lines)


def _result_from_state(state: dict[str, Any]) -> dict[str, Any]:
    """Expose an orchestration result without adding UI-originated values."""
    result = {
        "status": state["status"],
        "missing_fields": state.get("missing_fields", []),
        "questions": state.get("questions", []),
        "task_understanding": state.get("task_understanding", {}),
        "adjustment_options": state.get("adjustment_options", []),
        "value_lock_pass": state.get("value_lock_pass"),
        "final_message": state["final_message"],
        "trace": state.get("trace", []),
    }
    for response in state.get("tool_results", []):
        result[response["tool"].lower()] = response
    if "plan" in state:
        result["plan"] = state["plan"]
        result["report"] = state["report"]
    result["dashboard"] = render_dashboard(
        state.get("tool_results", []), report=state.get("report")
    )
    return result


def run(payload: dict[str, Any], *, llm_client: LLMClient | None = None) -> dict[str, Any]:
    """Run a schema-shaped UI request through the same production workflow."""
    return _result_from_state(run_structured_workflow(payload, llm_client=llm_client))


def run_text(task_text: str, *, llm_client: LLMClient | None = None) -> dict[str, Any]:
    """Run a natural-language task; the parser remains authoritative for values."""
    if not isinstance(task_text, str) or not task_text.strip():
        raise ValueError("task_text must be non-empty text")
    return _result_from_state(run_workflow(task_text.strip(), llm_client=llm_client))


def main() -> None:
    parser = argparse.ArgumentParser(description="绿航智算 D3 文本演示界面")
    parser.add_argument("--input", default=ROOT / "configs/examples/voyage_request.json")
    args = parser.parse_args()
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    result = run(payload)
    print(result["dashboard"])
    print("\n结构化输出：")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
