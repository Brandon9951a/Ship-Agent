"""Isolated LangGraph research flow for Tspeed -> demand artifact -> FDP.

It accepts named simulation inputs and never emits a formal vessel plan. The
existing v1 production/demo graph in ``core.orchestrator`` remains separate.
"""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict

from langgraph.graph import END, START, StateGraph

from research.contracts import SourceRef, SpeedOptimizationRequest, SpeedOptimizationResult, ToolRun
from research.demand.artifact_store import DemandArtifactStore
from research.demand.fdp_bridge import prepare_fdp_request
from research.tspeed.search import DEFAULT_DE_CONFIG, DEConfig
from research.tspeed.tool import SimulationScenario, run_speed_optimization
from schemas.research_management import BatteryManagementRequest
from tools.tmanagement_fdp import run_management_fdp


class ResearchWorkflowState(TypedDict):
    speed_request: SpeedOptimizationRequest
    scenario: SimulationScenario
    fdp_template: BatteryManagementRequest
    demand_store: DemandArtifactStore
    conversion_source: SourceRef
    auxiliary_source: SourceRef
    de_config: DEConfig
    stage: NotRequired[str]
    outcome_code: NotRequired[str]
    diagnostics: NotRequired[list[dict[str, Any]]]
    speed_run: NotRequired[ToolRun]
    speed_result: NotRequired[SpeedOptimizationResult]
    fdp_request: NotRequired[BatteryManagementRequest]
    fdp_result: NotRequired[dict[str, Any]]
    summary: NotRequired[dict[str, Any]]


def _failure(code: str, stage: str, detail: str) -> dict[str, Any]:
    return {"stage": "stopped", "outcome_code": code,
            "diagnostics": [{"stage": stage, "code": code, "detail": detail}]}


def _preflight(state: ResearchWorkflowState) -> dict[str, Any]:
    request, scenario, template = (state["speed_request"], state["scenario"],
                                   state["fdp_template"])
    spec = scenario.model_spec
    if template.source_mode != "simulation":
        return _failure("not_supported", "preflight", "研究流程只接受具名仿真输入")
    if (request.snapshot_id != template.snapshot_id or
            (request.scenario_id or scenario.scenario_id) != template.scenario_id or
            request.request_revision != template.request_revision):
        return _failure("invalid_input", "preflight", "Tspeed 与 FDP 的快照、情景或修订号不一致")
    if (not request.assumptions or not scenario.assumptions or not template.assumptions):
        return _failure("needs_input", "preflight", "研究流程需要逐环节具名仿真假设")
    demand_spec = spec.demand_spec
    if (demand_spec is None or demand_spec.conversion is None or
            demand_spec.conversion.source_ref is None or
            demand_spec.auxiliary.source_ref is None):
        return _failure("needs_input", "preflight", "缺少有来源的电机到母线转换或辅助负载")
    if (state["conversion_source"].source_id != demand_spec.conversion.source_ref.source_id or
            state["auxiliary_source"].source_id != demand_spec.auxiliary.source_ref.source_id):
        return _failure("invalid_input", "preflight", "FDP 来源与 Tspeed 需求构建配置不一致")
    predictor = spec.predictor
    if getattr(predictor, "tenergy_artifact_api", False):
        from tools.lstm_energy import HORIZON
        if request.task_id != predictor.task_id:
            return _failure("invalid_input", "preflight", "Tspeed 任务 ID 与 Tenergy 工件上下文不一致")
        try:
            if predictor.usage == "planning" and not predictor.planner_approved:
                return _failure("model_unapproved", "Tenergy", "LSTM 清单尚未获正式规划批准")
        except (OSError, ValueError, KeyError) as exc:
            return _failure("tool_error", "Tenergy", f"模型批准清单不可读取：{type(exc).__name__}")
        min_duration = (request.total_distance_km / request.time_grid.max_speed_kmh * 3600
                        + sum(segment.waiting_duration_s for segment in request.segments))
        if min_duration > HORIZON * 6 + 1e-6:
            return _failure("model_out_of_domain", "Tenergy",
                            f"航程最短历时至少 {min_duration:.1f}s，超过模型 {HORIZON}×6s 窗口")
    return {"stage": "ready", "diagnostics": []}


def _route_ready(state: ResearchWorkflowState) -> str:
    return "continue" if state["stage"] == "ready" else "finish"


def _speed(state: ResearchWorkflowState) -> dict[str, Any]:
    try:
        run, result = run_speed_optimization(
            state["speed_request"], scenario=state["scenario"],
            config=state["de_config"], demand_store=state["demand_store"])
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        return _failure("tool_error", "Tspeed", f"运行异常：{type(exc).__name__}: {exc}")
    if not run.ok or result.search_status != "candidate_found":
        return {"stage": "stopped", "outcome_code": run.outcome_code,
                "diagnostics": [item.to_dict() for item in run.diagnostics],
                "speed_run": run, "speed_result": result}
    if not result.bus_side_available or result.selected_demand_ref is None:
        return {"stage": "stopped", "outcome_code": "needs_input",
                "diagnostics": [{"stage": "Tspeed", "code": "demand_missing",
                                 "detail": "选定候选缺少母线需求工件"}],
                "speed_run": run, "speed_result": result}
    return {"stage": "speed_ready", "speed_run": run, "speed_result": result}


def _route_speed(state: ResearchWorkflowState) -> str:
    return "continue" if state["stage"] == "speed_ready" else "finish"


def _handoff(state: ResearchWorkflowState) -> dict[str, Any]:
    try:
        request = prepare_fdp_request(
            speed_result=state["speed_result"], speed_run=state["speed_run"],
            demand_store=state["demand_store"], template=state["fdp_template"],
            conversion_source=state["conversion_source"],
            auxiliary_source=state["auxiliary_source"])
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return _failure("invalid_input", "handoff", f"需求工件校验失败：{type(exc).__name__}: {exc}")
    return {"stage": "fdp_ready", "fdp_request": request}


def _route_handoff(state: ResearchWorkflowState) -> str:
    return "continue" if state["stage"] == "fdp_ready" else "finish"


def _management(state: ResearchWorkflowState) -> dict[str, Any]:
    try:
        result = run_management_fdp(state["fdp_request"])
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        return _failure("tool_error", "Tmanagement", f"运行异常：{type(exc).__name__}: {exc}")
    feasible = result.get("outcome_code") == "ok" and (result.get("payload") or {}).get("safe") is True
    return {"stage": "stopped", "fdp_result": result,
            "outcome_code": "research_feasible" if feasible else result.get("outcome_code", "tool_error"),
            "diagnostics": result.get("diagnostics") or []}


def _finish(state: ResearchWorkflowState) -> dict[str, Any]:
    speed = state.get("speed_result")
    fdp = state.get("fdp_result") or {}
    payload = fdp.get("payload") or {}
    summary = {
        "outcome_code": state.get("outcome_code", "tool_error"),
        "research_feasible": state.get("outcome_code") == "research_feasible",
        "selected_candidate_id": speed.selected_candidate_id if speed else None,
        "selected_demand_ref": speed.selected_demand_ref if speed else None,
        "speed_outcome": state["speed_run"].outcome_code if "speed_run" in state else None,
        "fdp_outcome": fdp.get("outcome_code"),
        "fdp_safe_under_assumptions": payload.get("safe") is True,
        "diagnostics": state.get("diagnostics", []),
        "note": "具名研究仿真；不构成实船正式航行规划",
    }
    return {"summary": summary}


def build_research_graph():
    """Compile an in-memory graph; no checkpoint or v1 graph mutation."""
    builder = StateGraph(ResearchWorkflowState)
    builder.add_node("preflight", _preflight)
    builder.add_node("speed", _speed)
    builder.add_node("handoff", _handoff)
    builder.add_node("management", _management)
    builder.add_node("finish", _finish)
    builder.add_edge(START, "preflight")
    builder.add_conditional_edges("preflight", _route_ready,
                                  {"continue": "speed", "finish": "finish"})
    builder.add_conditional_edges("speed", _route_speed,
                                  {"continue": "handoff", "finish": "finish"})
    builder.add_conditional_edges("handoff", _route_handoff,
                                  {"continue": "management", "finish": "finish"})
    builder.add_edge("management", "finish")
    builder.add_edge("finish", END)
    return builder.compile()


def run_research_workflow(*, speed_request: SpeedOptimizationRequest,
                          scenario: SimulationScenario,
                          fdp_template: BatteryManagementRequest,
                          demand_store: DemandArtifactStore,
                          conversion_source: SourceRef,
                          auxiliary_source: SourceRef,
                          de_config: DEConfig = DEFAULT_DE_CONFIG) -> dict[str, Any]:
    """Run the isolated research graph and return a compact acceptance result."""
    state: ResearchWorkflowState = {
        "speed_request": speed_request, "scenario": scenario,
        "fdp_template": fdp_template, "demand_store": demand_store,
        "conversion_source": conversion_source,
        "auxiliary_source": auxiliary_source, "de_config": de_config,
    }
    return build_research_graph().invoke(state)["summary"]
