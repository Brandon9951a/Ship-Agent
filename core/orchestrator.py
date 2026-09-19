"""LangGraph D2 orchestration skeleton with deterministic safety routing.

The graph is real LangGraph code, but the default five tool adapters are still
explicit placeholders.  A successful skeleton run therefore proves graph order
and stop conditions only; it is not a voyage plan or engineering result.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from operator import add
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from core.request_parser import parse_voyage_request
from schemas.messages import ToolStepResult
from schemas.types import Status, ToolResponse
from tools.placeholders import run_tdata, run_tenergy, run_tmanagement, run_tseg, run_tspeed


TOOL_ORDER = ("Tdata", "Tseg", "Tenergy", "Tspeed", "Tmanagement")
ToolAdapter = Callable[["AgentState"], ToolStepResult | ToolResponse]


class AgentState(TypedDict, total=False):
    user_input: str
    request: dict[str, Any]
    status: str
    missing_fields: list[str]
    questions: list[str]
    warnings: list[str]
    trace: Annotated[list[dict[str, Any]], add]
    tool_results: Annotated[list[dict[str, Any]], add]
    tool_mode: str
    failed_tool: str | None
    final_message: str


def _default_tools() -> dict[str, ToolAdapter]:
    functions = {
        "Tdata": run_tdata,
        "Tseg": run_tseg,
        "Tenergy": run_tenergy,
        "Tspeed": run_tspeed,
        "Tmanagement": run_tmanagement,
    }
    return {name: (lambda state, fn=fn: fn()) for name, fn in functions.items()}


def _status_text(value: Any) -> str:
    return value.value if isinstance(value, Status) else str(value)


def _normalise_tool_result(name: str, result: ToolStepResult | ToolResponse) -> dict[str, Any]:
    if isinstance(result, ToolStepResult):
        return {"tool": result.name, "status": result.status, "detail": result.detail}
    if isinstance(result, ToolResponse):
        return result.to_dict()
    raise TypeError(f"{name} returned an unsupported result type")


def build_workflow(
    tools: Mapping[str, ToolAdapter] | None = None,
    *,
    tool_mode: str = "placeholder",
):
    adapters = dict(_default_tools() if tools is None else tools)
    missing = [name for name in TOOL_ORDER if name not in adapters]
    extra = [name for name in adapters if name not in TOOL_ORDER]
    if missing or extra:
        raise ValueError(f"tool adapters must match TOOL_ORDER; missing={missing}, extra={extra}")

    def parse_node(state: AgentState) -> dict[str, Any]:
        result = parse_voyage_request(state.get("user_input", ""))
        return {
            "request": result.request.to_dict(),
            "status": result.status.value,
            "missing_fields": result.missing_fields,
            "questions": result.questions,
            "warnings": result.warnings,
            "tool_mode": state.get("tool_mode", tool_mode),
            "trace": [{"node": "parse", "status": result.status.value}],
        }

    def parse_route(state: AgentState) -> str:
        return "tools" if state.get("status") == Status.OK.value else "finish"

    def make_tool_node(name: str):
        def node(state: AgentState) -> dict[str, Any]:
            try:
                record = _normalise_tool_result(name, adapters[name](state))
                status = _status_text(record.get("status"))
                update: dict[str, Any] = {
                    "status": status,
                    "tool_results": [record],
                    "trace": [{"node": name, "status": status}],
                }
                if status == Status.NEED_CLARIFICATION.value:
                    update["missing_fields"] = list(record.get("missing_fields") or [])
                    update["questions"] = list(record.get("questions") or [])
                if status != Status.OK.value:
                    update["failed_tool"] = name
                return update
            except Exception as exc:
                return {
                    "status": Status.FAILED.value,
                    "failed_tool": name,
                    "tool_results": [{"tool": name, "status": Status.FAILED.value,
                                      "reason": type(exc).__name__}],
                    "trace": [{"node": name, "status": Status.FAILED.value}],
                }
        return node

    def tool_route(state: AgentState) -> str:
        return "continue" if state.get("status") == Status.OK.value else "finish"

    def finalize_node(state: AgentState) -> dict[str, Any]:
        status = state.get("status", Status.FAILED.value)
        if status == Status.NEED_CLARIFICATION.value:
            message = "需要补充信息后再继续计算。"
        elif status != Status.OK.value:
            failed = state.get("failed_tool") or "parse"
            message = f"流程在{failed}停止，未生成航行方案。"
        elif state.get("tool_mode", tool_mode) == "placeholder":
            message = "LangGraph结构运行完成；五工具仍为占位实现，不代表航行方案可用。"
        else:
            message = "五工具流程运行完成，结果仍需通过最终数值锁定。"
        return {"final_message": message,
                "trace": [{"node": "finalize", "status": status}]}

    builder = StateGraph(AgentState)
    builder.add_node("parse", parse_node)
    for name in TOOL_ORDER:
        builder.add_node(name, make_tool_node(name))
    builder.add_node("finalize", finalize_node)
    builder.add_edge(START, "parse")
    builder.add_conditional_edges("parse", parse_route, {"tools": "Tdata", "finish": "finalize"})
    for index, name in enumerate(TOOL_ORDER):
        next_node = TOOL_ORDER[index + 1] if index + 1 < len(TOOL_ORDER) else "finalize"
        if name == TOOL_ORDER[-1]:
            builder.add_edge(name, "finalize")
        else:
            builder.add_conditional_edges(
                name, tool_route, {"continue": next_node, "finish": "finalize"}
            )
    builder.add_edge("finalize", END)
    return builder.compile()


def run_workflow(
    user_input: str,
    tools: Mapping[str, ToolAdapter] | None = None,
    *,
    tool_mode: str = "placeholder",
) -> AgentState:
    graph = build_workflow(tools, tool_mode=tool_mode)
    return graph.invoke({
        "user_input": user_input,
        "trace": [],
        "tool_results": [],
        "tool_mode": tool_mode,
    })
