"""LangGraph orchestration for the five deterministic engineering tools."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict
from operator import add
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from core.intent_explainer import build_task_understanding
from core.intent_translator import build_boundary_diagnostics, enumerate_options
from core.llm_layer import LLMClient
from core.request_parser import parse_voyage_request
from core.report import build_report
from core.value_lock import lock_report_values
from schemas.messages import ToolStepResult
from schemas.types import (
    DataContext, EnergyResult, ManagementPlan, OptimizationResult, Segment,
    Status, ToolResponse, TraceEvent, VoyagePlan, VoyageRequest,
)
from schemas.validate import parse_request as parse_structured_request, validate
from tools.tdata import load_config, tdata
from tools.tenergy import EnergyModel, run_tenergy
from tools.tmanagement import run_tmanagement
from tools.tseg import segment
from tools.tspeed import run_tspeed


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
    plan: dict[str, Any]
    report: dict[str, Any]
    task_understanding: dict[str, Any]
    adjustment_options: list[dict[str, Any]]
    boundary_diagnostics: dict[str, Any]
    value_lock_pass: bool


def _default_tools() -> dict[str, ToolAdapter]:
    root = Path(__file__).resolve().parents[1]
    route_config = load_config(root / "configs/route_facts.yaml")
    aliases_config = load_config(root / "configs/aliases.yaml")
    vessel_config = load_config(root / "configs/vessel_facts.yaml")
    limits_config = load_config(root / "configs/limits.yaml")
    demo_policy = load_config(root / "configs/demo_policy.yaml")

    def request(state: AgentState) -> VoyageRequest:
        return VoyageRequest.from_dict(state["request"])

    def prior(state: AgentState, name: str) -> dict[str, Any]:
        matches = [item for item in state.get("tool_results", []) if item.get("tool") == name]
        if not matches or not isinstance(matches[-1].get("payload"), dict):
            raise ValueError(f"{name} result is unavailable")
        return matches[-1]["payload"]

    def tdata_adapter(state: AgentState) -> ToolResponse:
        return tdata(
            request(state), route_config=route_config, aliases_config=aliases_config,
            vessel_config=vessel_config, limits_config=limits_config,
            demo_policy_config=demo_policy,
        )

    def tseg_adapter(state: AgentState) -> ToolResponse:
        return segment(
            request(state), DataContext.from_dict(prior(state, "Tdata")),
            route_config=route_config, aliases_config=aliases_config,
            demo_policy_config=demo_policy,
        )

    def tenergy_adapter(state: AgentState) -> ToolResponse:
        segments = [Segment.from_dict(item) for item in prior(state, "Tseg")["segments"]]
        configured = demo_policy.get("voyage_demo", {})
        model_config = configured.get("energy_model", {})
        model = EnergyModel(
            model_id=model_config.get("model_id"),
            coefficient_kw_per_kmh3=model_config.get("coefficient_kw_per_kmh3"),
            auxiliary_power_kw=model_config.get("auxiliary_power_kw"),
            energy_scope=model_config.get("energy_scope"),
            usage=model_config.get("usage"),
            approval_ref=model_config.get("approval_ref"),
        )
        return run_tenergy(segments, configured.get("candidate_speeds_kmh", []), model)

    def tspeed_adapter(state: AgentState) -> ToolResponse:
        data = DataContext.from_dict(prior(state, "Tdata"))
        segments = [Segment.from_dict(item) for item in prior(state, "Tseg")["segments"]]
        candidates = [
            EnergyResult.from_dict(item)
            for item in prior(state, "Tenergy")["candidate_results"]
        ]
        return run_tspeed(request(state), segments, candidates, data.vessel)

    def tmanagement_adapter(state: AgentState) -> ToolResponse:
        return run_tmanagement(
            request(state), DataContext.from_dict(prior(state, "Tdata")),
            OptimizationResult.from_dict(prior(state, "Tspeed")),
        )

    return {
        "Tdata": tdata_adapter,
        "Tseg": tseg_adapter,
        "Tenergy": tenergy_adapter,
        "Tspeed": tspeed_adapter,
        "Tmanagement": tmanagement_adapter,
    }


def _status_text(value: Any) -> str:
    return value.value if isinstance(value, Status) else str(value)


def _normalise_tool_result(name: str, result: ToolStepResult | ToolResponse) -> dict[str, Any]:
    if isinstance(result, ToolStepResult):
        return {"tool": result.name, "status": result.status, "detail": result.detail}
    if isinstance(result, ToolResponse):
        checked = validate(result)
        if not checked.valid:
            raise ValueError(f"{name} returned a contract-invalid response")
        return result.to_dict()
    raise TypeError(f"{name} returned an unsupported result type")


def build_workflow(
    tools: Mapping[str, ToolAdapter] | None = None,
    *,
    tool_mode: str = "synthetic_demo",
    llm_client: LLMClient | None = None,
):
    adapters = dict(_default_tools() if tools is None else tools)
    missing = [name for name in TOOL_ORDER if name not in adapters]
    extra = [name for name in adapters if name not in TOOL_ORDER]
    if missing or extra:
        raise ValueError(f"tool adapters must match TOOL_ORDER; missing={missing}, extra={extra}")

    root = Path(__file__).resolve().parents[1]
    demo_policy = load_config(root / "configs/demo_policy.yaml")
    auxiliary_kw = float(demo_policy.get("power_and_energy", {}).get("demo_auxiliary_power_kw", 0.0))

    def _prior_payload(state: AgentState, name: str) -> dict[str, Any] | None:
        matches = [item for item in state.get("tool_results", []) if item.get("tool") == name]
        if not matches or not isinstance(matches[-1].get("payload"), dict):
            return None
        return matches[-1]["payload"]

    def compute_adjustment(
        state: AgentState, speed_payload: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Tspeed 不可行时,用同一候选网格做边界诊断并枚举定量妥协选项。

        数值只来自既有工具结果与演示配置,不产生新工程数值;任何方向都不自动
        降低硬安全下限。异常时返回空选项、空诊断,不阻断主流程。
        """
        try:
            request = VoyageRequest.from_dict(state["request"])
            data_payload = _prior_payload(state, "Tdata")
            seg_payload = _prior_payload(state, "Tseg")
            energy_payload = _prior_payload(state, "Tenergy")
            if not all([data_payload, seg_payload, energy_payload]):
                return [], {}
            data = DataContext.from_dict(data_payload)
            segments = [Segment.from_dict(item) for item in seg_payload["segments"]]
            candidates = [
                EnergyResult.from_dict(item) for item in energy_payload["candidate_results"]
            ]
            optimization = OptimizationResult.from_dict(speed_payload)
            diagnostics = build_boundary_diagnostics(
                segments, candidates, data.vessel, request, auxiliary_kw,
            )
            options = enumerate_options(
                optimization, request, data.vessel, segments, candidates, auxiliary_kw,
            )
            return [asdict(option) for option in options], diagnostics
        except (KeyError, TypeError, ValueError):
            return [], {}

    def parse_node(state: AgentState) -> dict[str, Any]:
        if state.get("request") and not state.get("user_input"):
            decoded, checked = parse_structured_request(state["request"])
            request_record = decoded.to_dict() if decoded is not None else state["request"]
            return {
                "request": request_record,
                "task_understanding": build_task_understanding(
                    request_record,
                    llm_client if checked.status == Status.OK else None,
                ),
                "status": checked.status.value,
                "missing_fields": checked.missing_fields,
                "questions": checked.questions,
                "warnings": [],
                "tool_mode": state.get("tool_mode", tool_mode),
                "trace": [{"node": "parse", "status": checked.status.value}],
            }
        result = parse_voyage_request(state.get("user_input", ""))
        return {
            "request": result.request.to_dict(),
            "task_understanding": build_task_understanding(
                result.request.to_dict(),
                llm_client if result.status == Status.OK else None,
            ),
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
                if status == Status.INFEASIBLE.value and name == "Tspeed":
                    speed_payload = record.get("payload")
                    if isinstance(speed_payload, dict):
                        options, diagnostics = compute_adjustment(state, speed_payload)
                        update["adjustment_options"] = options
                        update["boundary_diagnostics"] = diagnostics
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
        update: dict[str, Any] = {}
        if status == Status.NEED_CLARIFICATION.value:
            message = "需要补充信息后再继续计算。"
        elif status != Status.OK.value:
            failed = state.get("failed_tool") or "parse"
            options = state.get("adjustment_options") or []
            if options:
                message = (
                    f"流程在{failed}停止，未生成航行方案。可选调整（需用户确认，"
                    "不自动修改硬安全下限）：" + "；".join(
                        option["label"] for option in options
                    )
                )
            else:
                message = f"流程在{failed}停止，未生成航行方案。"
        elif state.get("tool_mode", tool_mode) == "synthetic_demo":
            by_name = {item["tool"]: item for item in state.get("tool_results", [])}
            try:
                plan = VoyagePlan(
                    request=VoyageRequest.from_dict(state["request"]),
                    status=Status.OK,
                    data=DataContext.from_dict(by_name["Tdata"]["payload"]),
                    segments=[
                        Segment.from_dict(item)
                        for item in by_name["Tseg"]["payload"]["segments"]
                    ],
                    optimization=OptimizationResult.from_dict(by_name["Tspeed"]["payload"]),
                    management=ManagementPlan.from_dict(by_name["Tmanagement"]["payload"]),
                    assumptions=[
                        "synthetic_demo：历史数据覆盖检查与调研单点锚定的软件仿真；"
                        "不是实船安全、运营批准或模型标定结论。"
                    ],
                    trace=[
                        TraceEvent(
                            item["tool"], Status(item["status"]),
                            "工具结果已通过接口校验。",
                        )
                        for item in state.get("tool_results", [])
                    ],
                )
                checked = validate(plan)
                if not checked.valid:
                    raise ValueError("final VoyagePlan validation failed")
                update["plan"] = plan.to_dict()
                report, locked = lock_report_values(build_report(plan, llm_client), plan)
                update["report"] = report
                update["value_lock_pass"] = locked
                message = (
                    "五工具synthetic_demo流程完成；结果仅用于软件仿真，"
                    "不代表实船安全或运营批准。"
                )
            except (KeyError, TypeError, ValueError) as exc:
                status = Status.FAILED.value
                update["status"] = status
                update["failed_tool"] = "finalize"
                message = f"最终方案校验失败（{type(exc).__name__}），未生成航行方案。"
        else:
            message = "五工具流程运行完成，结果仍需通过最终数值锁定。"
        update.update(final_message=message,
                      trace=[{"node": "finalize", "status": status}])
        return update

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
    tool_mode: str = "synthetic_demo",
    llm_client: LLMClient | None = None,
) -> AgentState:
    graph = build_workflow(tools, tool_mode=tool_mode, llm_client=llm_client)
    return graph.invoke({
        "user_input": user_input,
        "trace": [],
        "tool_results": [],
        "tool_mode": tool_mode,
    })


def run_structured_workflow(
    payload: dict[str, Any],
    tools: Mapping[str, ToolAdapter] | None = None,
    *,
    tool_mode: str = "synthetic_demo",
    llm_client: LLMClient | None = None,
) -> AgentState:
    """Run the same graph from an already structured request payload."""
    graph = build_workflow(tools, tool_mode=tool_mode, llm_client=llm_client)
    return graph.invoke({
        "request": payload,
        "trace": [],
        "tool_results": [],
        "tool_mode": tool_mode,
    })
