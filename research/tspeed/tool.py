"""Tspeed 工具的公开入口：请求 → DE 搜索 → 独立核验 → v2 结果。

对外只暴露 ``run_speed_optimization``：返回 ``(ToolRun, SpeedOptimizationResult)``。
失败语义严格按接口 v2 第 3、6 节区分：
``candidate_found / search_unresolved / input_invalid / model_out_of_domain / tool_error``。
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass

from research.contracts import (
    CONTRACT_SCHEMA_VERSION,
    Diagnostic,
    ManagementFeedback,
    OutcomeCode,
    SearchTrace,
    SourceRef,
    SpeedOptimizationRequest,
    SpeedOptimizationResult,
    ToolRun,
    finish_run,
    iso_from_offset,
    start_run,
    stable_hash,
)
from research.contracts.interfaces import search_status_for
from research.tspeed.eval import EnergyModelSpec, infeasibility_diagnostic
from research.tspeed.objective import objective_terms
from research.tspeed.search import (
    ALGORITHM_VERSION,
    DEFAULT_DE_CONFIG,
    DEConfig,
    DESearchResult,
    candidate_trajectory,
    run_differential_evolution,
)
from research.tspeed.verification import VerificationReport, verify_candidate
from research.demand.artifact_store import DemandArtifactStore

TOOL_NAME = "Tspeed"
TOOL_VERSION = f"{ALGORITHM_VERSION}+contract-{CONTRACT_SCHEMA_VERSION}"


@dataclass(frozen=True)
class SimulationScenario:
    """一次搜索所采用的情景与来源；母线侧假设必须具名。"""

    scenario_id: str
    environment_ref: str
    history_ref: str
    model_spec: EnergyModelSpec
    source_refs: tuple[SourceRef, ...] = ()
    assumptions: tuple[str, ...] = ()
    note: str | None = None
    diagnostics: tuple[Diagnostic, ...] = ()

    def config_hash(self) -> str:
        return stable_hash({
            "scenario_id": self.scenario_id,
            "environment_ref": self.environment_ref,
            "history_ref": self.history_ref,
            "model_version": self.model_spec.model_version
            or self.model_spec.predictor.model_version,
            "integration_rule": self.model_spec.integration_rule,
            "prediction_step_s": self.model_spec.grid.prediction_step_s,
            "verification_step_s": self.model_spec.grid.verification_step_s,
            "max_step_s": self.model_spec.max_step_s,
            "demand_config_hash": (self.model_spec.demand_spec.config_hash()
                                   if self.model_spec.demand_spec else None),
            "assumptions": list(self.assumptions),
        })


def _segment_summaries(result: DESearchResult, verification: VerificationReport
                       ) -> tuple[dict[str, object], ...]:
    """逐航段汇总；母线侧无依据时对应字段保持 null。"""
    best = result.best
    if best is None:
        return ()
    energy_by_segment = {summary.scope_id: summary.predicted_motor_energy_kwh
                         for summary in best.outcome.segment_summaries}
    demand_by_segment: dict[str, float] = {}
    bus_available = bool(verification.demand and verification.demand.bus_side_available)
    if verification.demand is not None:
        for point in verification.demand.points:
            if point.segment_id is None or point.demand_bus_kw is None:
                continue
            demand_by_segment[point.segment_id] = (
                demand_by_segment.get(point.segment_id, 0.0)
                + point.demand_bus_kw * point.duration_s / 3600.0)
    summaries: list[dict[str, object]] = []
    for fact in best.expanded.segment_facts:
        summaries.append({
            "segment_id": fact.segment_id,
            "distance_km": fact.requested_distance_km,
            "sailing_duration_s": fact.sailing_duration_s,
            "waiting_duration_s": fact.waiting_duration_s,
            "total_duration_s": fact.duration_s,
            "cruise_speed_kmh": fact.cruise_speed_kmh,
            "entry_speed_kmh": fact.entry_speed_kmh,
            "exit_speed_kmh": fact.exit_speed_kmh,
            "motor_energy_kwh": energy_by_segment.get(fact.segment_id),
            "demand_bus_energy_kwh": (demand_by_segment.get(fact.segment_id)
                                      if bus_available else None),
            "distance_residual_km": fact.distance_residual_km,
        })
    return tuple(summaries)


def _failure_outcome(result: DESearchResult) -> OutcomeCode:
    """区分"搜索预算内未解决"与"模型域外"；不把启发式失败称为数学无解。"""
    codes = set(result.context.violations)
    if any(code.startswith("prediction_model_out_of_domain") for code in codes):
        return OutcomeCode.MODEL_OUT_OF_DOMAIN
    if codes and codes <= {"prediction_needs_input"}:
        return OutcomeCode.NEEDS_INPUT
    return OutcomeCode.SEARCH_UNRESOLVED


def _empty_result(*, status: str, seed: int, population: int, generations: int,
                  evaluations: int, runtime_ms: float, trace: SearchTrace | None,
                  diagnostics: tuple[Diagnostic, ...],
                  assumptions: tuple[str, ...]) -> SpeedOptimizationResult:
    return SpeedOptimizationResult(
        search_status=status,
        selected_candidate_id=None,
        selected_trajectory_ref=None,
        selected_energy_ref=None,
        selected_demand_ref=None,
        segment_summary=(),
        total_duration_s=None,
        arrival_at=None,
        objective_terms=None,
        constraint_checks=(),
        search_trace_ref=trace.trace_id if trace else None,
        seed=seed,
        population=population,
        generations=generations,
        evaluations=evaluations,
        runtime_ms=runtime_ms,
        search_trace=trace,
        diagnostics=diagnostics,
        assumptions=assumptions,
    )


def apply_management_feedback(request: SpeedOptimizationRequest,
                              feedback: ManagementFeedback,
                              *, spec: EnergyModelSpec) -> tuple[EnergyModelSpec,
                                                                 list[Diagnostic],
                                                                 list[str]]:
    """消费 FDP 反馈。

    只有对候选族可证明的母线功率上限才转成新的硬约束；SOC 路径相关失败只记录为
    候选拒绝与条件化提示，下一次候选仍须再次运行 FDP。
    """
    diagnostics: list[Diagnostic] = []
    notes: list[str] = []
    if not feedback.becomes_hard_constraint:
        diagnostics.append(Diagnostic(
            stage="feedback", code="feedback_not_hard_constraint",
            detail=(f"failure_scope={feedback.failure_scope}："
                    "该失败依赖原候选的 SOC 演化，不简化为对所有新轨迹成立的功率上限"),
            field_ref="failure_scope", retryable=False))
        notes.append(f"FDP 反馈（{feedback.failure_scope}）：仅作为候选拒绝记录")
        return spec, diagnostics, notes
    applied = min(spec.demand_limit_kw, feedback.deliverable_kw) \
        if spec.demand_limit_kw is not None and feedback.deliverable_kw is not None \
        else feedback.deliverable_kw
    if applied is None:
        diagnostics.append(Diagnostic(
            stage="feedback", code="feedback_missing_limit",
            detail="反馈声称存在可证明的功率上限，但未给出 deliverable_kw",
            field_ref="deliverable_kw", retryable=False))
        notes.append("FDP 反馈缺少可用上限，未生成新硬约束")
        return spec, diagnostics, notes
    diagnostics.append(Diagnostic(
        stage="feedback", code="hard_power_limit_applied",
        detail=f"对候选族可证明的母线功率上限 {applied}kW 已加入硬约束",
        field_ref="power_demand_limit_kw", observed=applied, unit="kW",
        source_refs=(feedback.limit_source_ref,) if feedback.limit_source_ref else (),
        retryable=False))
    notes.append(f"FDP 反馈生成硬约束：母线功率上限 {applied}kW")
    return dataclasses.replace(spec, demand_limit_kw=applied), diagnostics, notes


def run_speed_optimization(request: SpeedOptimizationRequest, *,
                           scenario: SimulationScenario,
                           config: DEConfig = DEFAULT_DE_CONFIG,
                           call_id: str | None = None,
                           parent_call_id: str | None = None,
                           fdp_feedback: ManagementFeedback | None = None,
                           demand_store: DemandArtifactStore | None = None,
                           ) -> tuple[ToolRun, SpeedOptimizationResult]:
    """执行一次 Tspeed 求解；返回 v2 信封与结果工件。"""
    started = time.perf_counter()
    envelope = start_run(
        TOOL_NAME,
        call_id=call_id or f"tspeed-{stable_hash(request.input_hash())[:12]}",
        parent_call_id=parent_call_id,
        input_hash=request.input_hash(),
        request_revision=request.request_revision,
        snapshot_id=request.snapshot_id,
        scenario_id=request.scenario_id or scenario.scenario_id,
        tool_version=TOOL_VERSION,
        config_hash=scenario.config_hash())
    base_assumptions = tuple(request.assumptions) + tuple(scenario.assumptions)
    diagnostics: list[Diagnostic] = list(scenario.diagnostics)
    spec = scenario.model_spec

    def bail(outcome: OutcomeCode, *, extra_assumptions: tuple[str, ...] = (),
             evaluations: int = 0, generations: int = 0,
             trace: SearchTrace | None = None) -> tuple[ToolRun, SpeedOptimizationResult]:
        assumptions = tuple(dict.fromkeys(base_assumptions + extra_assumptions))
        mapped = search_status_for(outcome)
        result = _empty_result(
            status=(mapped.value if mapped is not None else "search_unresolved"),
            seed=config.seeds[0], population=config.population,
            generations=generations, evaluations=evaluations,
            runtime_ms=(time.perf_counter() - started) * 1000.0, trace=trace,
            diagnostics=tuple(diagnostics), assumptions=assumptions)
        return finish_run(envelope, outcome=outcome, diagnostics=diagnostics,
                          payload=result.to_dict()), result

    if request.objective != "min_energy":
        diagnostics.append(Diagnostic(
            stage="input", code="objective_not_supported",
            detail=f"目标类型 {request.objective} 未实现，不能悄悄改用其他目标",
            field_ref="objective", observed=request.objective, retryable=False))
        return bail(OutcomeCode.NOT_SUPPORTED)

    feedback_assumptions: tuple[str, ...] = ()
    if fdp_feedback is not None:
        if request.demand_builder_ref is not None \
                and fdp_feedback.selected_demand_ref != request.demand_builder_ref:
            diagnostics.append(Diagnostic(
                stage="input", code="feedback_version_mismatch",
                detail="FDP 反馈引用的需求工件与本次请求不一致，拒绝消费该反馈",
                field_ref="prior_feedback_ref",
                observed=fdp_feedback.selected_demand_ref,
                limit=request.demand_builder_ref, retryable=False))
            return bail(OutcomeCode.INVALID_INPUT)
        spec, feedback_diagnostics, feedback_notes = apply_management_feedback(
            request, fdp_feedback, spec=spec)
        diagnostics.extend(feedback_diagnostics)
        feedback_assumptions = tuple(feedback_notes)

    search = run_differential_evolution(
        request, spec=spec, environment_ref=scenario.environment_ref,
        history_ref=scenario.history_ref, config=config,
        parent_call_id=envelope["call_id"])

    if search.best is None:
        outcome = _failure_outcome(search)
        for item in (search.runs[0].diagnostic_candidates[:3] if search.runs else ()):
            diagnostics.append(infeasibility_diagnostic(item))
        diagnostics.append(Diagnostic(
            stage="search", code=outcome.value,
            detail=(f"DE 在 {search.evaluations} 次评估内未找到可行候选；"
                    "不得表述为数学无解"),
            field_ref="search_status", retryable=True))
        return bail(outcome, extra_assumptions=feedback_assumptions,
                    evaluations=search.evaluations,
                    generations=search.trace.generations_run, trace=search.trace)

    best = search.best
    candidate = candidate_trajectory(best)
    verification = verify_candidate(
        candidate, request=request, spec=spec,
        coarse_energy_kwh=best.energy_kwh,
        call_id=f"{envelope['call_id']}-verify",
        parent_call_id=envelope["call_id"])
    diagnostics.extend(verification.diagnostics)

    if not verification.passed:
        diagnostics.append(Diagnostic(
            stage="verify", code="verification_failed",
            detail="最终候选未通过独立约束检查：" + ", ".join(verification.failed_checks),
            field_ref="constraint_checks", retryable=True))
        return bail(OutcomeCode.SEARCH_UNRESOLVED,
                    extra_assumptions=feedback_assumptions,
                    evaluations=search.evaluations,
                    generations=search.trace.generations_run, trace=search.trace)

    bus_available = bool(verification.demand and verification.demand.bus_side_available)
    selected_demand_ref = verification.demand.series_id if verification.demand else None
    if verification.demand is not None and demand_store is not None:
        try:
            selected_demand_ref = demand_store.put(verification.demand).artifact_id
        except (OSError, ValueError, TypeError) as exc:
            diagnostics.append(Diagnostic(
                stage="artifact", code="demand_artifact_write_failed",
                detail=f"选定需求序列未能持久化：{type(exc).__name__}",
                field_ref="selected_demand_ref", retryable=True))
            return bail(OutcomeCode.TOOL_ERROR,
                        extra_assumptions=feedback_assumptions,
                        evaluations=search.evaluations,
                        generations=search.trace.generations_run, trace=search.trace)
    terms = objective_terms(
        speed_vector=best.speed_vector,
        energy_kwh=(verification.demand_energy_kwh if bus_available
                    else verification.energy_kwh),
        request=request, energy_scope="bus" if bus_available else "motor")
    collected = list(base_assumptions) + list(feedback_assumptions)
    if not bus_available:
        collected.append("母线侧无依据：本次仅为电机侧对照实验，不构成 SOC 安全结论")
    if verification.demand is not None and demand_store is None:
        collected.append("未提供需求工件库：selected_demand_ref 仅为本次内存标识，不能跨进程取回")
    assumptions = tuple(dict.fromkeys(collected))
    result = SpeedOptimizationResult(
        search_status="candidate_found",
        selected_candidate_id=best.candidate_id,
        selected_trajectory_ref=f"trajectory-{best.candidate_id}",
        selected_energy_ref=(verification.energy_envelope.artifact_ref.artifact_id
                             if verification.energy_envelope
                             and verification.energy_envelope.artifact_ref else None),
        selected_demand_ref=selected_demand_ref,
        segment_summary=_segment_summaries(search, verification),
        total_duration_s=verification.total_duration_s,
        arrival_at=iso_from_offset(request.planning_origin_at,
                                   verification.total_duration_s),
        objective_terms=terms,
        constraint_checks=verification.checks,
        search_trace_ref=search.trace.trace_id,
        seed=search.trace.seed,
        population=config.population,
        generations=search.trace.generations_run,
        evaluations=search.evaluations,
        runtime_ms=(time.perf_counter() - started) * 1000.0,
        selected_trajectory=tuple(point.to_dict() for point in candidate.trajectory),
        selected_speed_vector=tuple(best.speed_vector),
        total_energy_kwh=verification.energy_kwh,
        total_distance_km=verification.distance_km,
        search_trace=search.trace,
        demand_config_hash=(verification.demand.conversion_config_hash
                            if verification.demand else None),
        bus_side_available=bus_available,
        prior_feedback_ref=request.prior_feedback_ref,
        rerun_count=1 if fdp_feedback is not None else 0,
        diagnostics=tuple(diagnostics),
        assumptions=assumptions,
        note=scenario.note,
    )
    return finish_run(envelope, outcome=OutcomeCode.OK, diagnostics=diagnostics,
                      payload=result.to_dict()), result


__all__ = [
    "SimulationScenario",
    "TOOL_NAME",
    "TOOL_VERSION",
    "apply_management_feedback",
    "run_speed_optimization",
]
