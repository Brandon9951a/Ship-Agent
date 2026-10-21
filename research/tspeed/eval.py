"""候选评估：展开轨迹 → 批量预测 → 共享需求构建 → 独立约束检查 → 目标值。

DE 只在这里接触预测器与需求构建器；目标函数与约束检查分开，硬约束以
"可行优先"处理，最终候选必须再经独立检查器复核（见 ``verification.py``）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from research.contracts import (
    Diagnostic,
    OutcomeCode,
    PowerDemandSeries,
    SpeedOptimizationRequest,
)
from research.demand import DemandBuildError, DemandBuilderSpec, build_power_demand
from research.demand.builder import DemandProfileSummary, summarize_analytic_profile
from research.tspeed.objective import objective_value, speed_difference_penalty
from research.tspeed.profile import TrajectoryProfile
from research.tspeed.predictor import (
    AnalyticPowerPredictor,
    DEFAULT_INTEGRATION_RULE,
    EnergyPredictor,
    EvaluationGrid,
    PredictionOutcome,
)
from research.tspeed.trajectory import ExpandedTrajectory, expand_trajectory


@dataclass(frozen=True)
class EnergyModelSpec:
    """一次搜索采用的预测器与时间网格（模型版本、积分规则随请求提交）。"""

    predictor: EnergyPredictor
    grid: EvaluationGrid
    environment_ref: str
    history_ref: str
    max_step_s: float = 60.0
    demand_spec: DemandBuilderSpec | None = None
    demand_limit_kw: float | None = None
    energy_budget_kwh: float | None = None
    model_version: str | None = None
    integration_rule: str = DEFAULT_INTEGRATION_RULE


@dataclass(frozen=True)
class CandidateEvaluation:
    """一个候选的完整评估结果。"""

    candidate_id: str
    speed_vector: tuple[float, ...]
    expanded: ExpandedTrajectory
    outcome: PredictionOutcome
    objective_value: float | None
    penalty: float
    energy_kwh: float | None
    peak_motor_power_kw: float | None
    total_duration_s: float
    demand: PowerDemandSeries | DemandProfileSummary | None
    violations: tuple[str, ...]
    constraint_notes: tuple[str, ...]
    from_cache: bool = False

    @property
    def feasible(self) -> bool:
        return bool(self.violations) is False and self.energy_kwh is not None \
            and self.objective_value is not None

    @property
    def demand_energy_kwh(self) -> float | None:
        return self.demand.demand_energy_kwh() if self.demand else None

    def as_diagnostic_candidate(self) -> dict[str, object]:
        """不可执行的诊断候选：只含诊断字段，不放入选定方案字段。"""
        return {
            "candidate_id": self.candidate_id,
            "speed_vector": list(self.speed_vector),
            "violations": list(self.violations),
            "objective_value": self.objective_value,
            "energy_kwh": self.energy_kwh,
            "total_duration_s": self.total_duration_s,
        }


@dataclass
class DEEvaluationContext:
    """一次搜索共享的评估上下文；同时保存缓存与统计。"""

    request: SpeedOptimizationRequest
    spec: EnergyModelSpec
    environment_ref: str
    history_ref: str
    config_hash: str
    parent_call_id: str | None = None
    cache: dict[tuple[float, ...], CandidateEvaluation] = field(default_factory=dict)
    cache_hits: int = 0
    predictions: int = 0
    batches: int = 0
    violations: dict[str, int] = field(default_factory=dict)

    @property
    def grid(self) -> EvaluationGrid:
        return self.spec.grid

    @property
    def predictor(self) -> EnergyPredictor:
        return self.spec.predictor

    def record_violations(self, codes) -> None:
        for code in codes:
            self.violations[code] = self.violations.get(code, 0) + 1


def _candidate_id(speed_vector) -> str:
    from research.contracts import stable_hash

    return "c-" + stable_hash([round(float(v), 9) for v in speed_vector])[:12]


def evaluate_candidate(speed_vector, *, context: DEEvaluationContext,
                       batch_id: str = "batch") -> CandidateEvaluation:
    """展开、预测、构建需求并给出独立约束结论。"""
    vector = tuple(float(value) for value in speed_vector)
    cached = context.cache.get(vector)
    if cached is not None:
        context.cache_hits += 1
        return CandidateEvaluation(
            candidate_id=cached.candidate_id, speed_vector=cached.speed_vector,
            expanded=cached.expanded, outcome=cached.outcome,
            objective_value=cached.objective_value, penalty=cached.penalty,
            energy_kwh=cached.energy_kwh, peak_motor_power_kw=cached.peak_motor_power_kw,
            total_duration_s=cached.total_duration_s, demand=cached.demand,
            violations=cached.violations, constraint_notes=cached.constraint_notes,
            from_cache=True)

    request = context.request
    candidate_id = _candidate_id(vector)
    expanded = expand_trajectory(vector, request=request, max_step_s=context.spec.max_step_s)
    violations: dict[str, str] = {}
    for violation in expanded.violations:
        violations.setdefault(violation.code, violation.detail)

    # 需要检查母线约束时必须保留逐时功率；否则需求构建器会得到空序列，
    # 使搜索期的功率/电量上限失效。无需求构建器时仍释放逐时点以节省内存。
    profile = TrajectoryProfile.from_expansion(expanded)
    fast_analytic_demand = (context.spec.demand_spec is not None
                            and isinstance(context.predictor, AnalyticPowerPredictor))
    outcome = context.predictor.predict_profile(profile, candidate_id=candidate_id,
                                                grid=context.grid,
                                                keep_power_points=(context.spec.demand_spec is not None
                                                                   and not fast_analytic_demand))
    context.batches += 1
    context.predictions += 1
    if not outcome.ok:
        code = outcome.diagnostics[0].code if outcome.diagnostics else outcome.outcome_code
        violations.setdefault(f"prediction_{outcome.outcome_code}", code)
    energy = outcome.motor_energy_kwh
    peak_motor_power = outcome.peak_motor_power_kw
    duration = expanded.total_duration_s
    notes: list[str] = []

    # --- 硬约束 ---------------------------------------------------------------
    limit_s = request.remaining_time_s
    if limit_s is None and request.absolute_deadline_at and request.planning_origin_at:
        from research.contracts import plan_offset_seconds

        offset = plan_offset_seconds(request.planning_origin_at, request.absolute_deadline_at)
        if offset is not None:
            limit_s = offset
    if limit_s is not None and duration > limit_s + 1e-6:
        violations.setdefault(
            "arrival_deadline_missed",
            f"总历时 {duration:.1f}s 超过剩余期限 {limit_s:.1f}s")
    if energy is not None and request.battery_budget_kwh is not None:
        if energy > request.battery_budget_kwh + 1e-9:
            violations.setdefault(
                "battery_budget_exceeded",
                f"预测电机侧 {energy:.3f}kWh 超过可用电量 {request.battery_budget_kwh}kWh")

    demand_series: PowerDemandSeries | DemandProfileSummary | None = None
    if context.spec.demand_spec is not None:
        try:
            if fast_analytic_demand and outcome.ok:
                demand_series = summarize_analytic_profile(
                    profile, power_at_speed=context.predictor.power_kw,
                    spec=context.spec.demand_spec,
                    power_for_interval=getattr(context.predictor, "interval_power_kw", None))
            else:
                demand_series = build_power_demand(
                    outcome.to_envelope(), spec=context.spec.demand_spec,
                    series_id=f"demand-{candidate_id}",
                    input_energy_ref=(outcome.artifact_ref.artifact_id
                                      if outcome.artifact_ref else None))
        except DemandBuildError as exc:
            violations.setdefault(f"demand_{exc.code}", str(exc))
        if demand_series is not None:
            if context.spec.demand_limit_kw is not None:
                peak = demand_series.peak_demand_kw()
                if not demand_series.bus_side_available:
                    notes.append("母线侧无依据：功率上限检查不适用，未据此判定可行")
                elif peak is None:
                    violations.setdefault(
                        "power_peak_unknown", "存在未预测区间，母线峰值功率不确定")
                elif peak > context.spec.demand_limit_kw + 1e-6:
                    violations.setdefault(
                        "power_limit_exceeded",
                        f"母线峰值 {peak:.3f}kW 超过同边界上限 "
                        f"{context.spec.demand_limit_kw}kW")
            if context.spec.energy_budget_kwh is not None:
                demand_energy = demand_series.demand_energy_kwh()
                if demand_energy is not None and \
                        demand_energy > context.spec.energy_budget_kwh + 1e-9:
                    violations.setdefault(
                        "demand_budget_exceeded",
                        f"母线需求 {demand_energy:.3f}kWh 超过预算 "
                        f"{context.spec.energy_budget_kwh}kWh")

    penalty = speed_difference_penalty(vector)
    objective_energy = (demand_series.demand_energy_kwh()
                        if demand_series is not None and demand_series.bus_side_available
                        else energy)
    objective = objective_value(energy_kwh=objective_energy,
                                penalty=penalty,
                                lambda_value=float(request.speed_penalty_lambda),
                                objective=request.objective, duration_s=duration)
    evaluation = CandidateEvaluation(
        candidate_id=candidate_id,
        speed_vector=vector,
        expanded=expanded,
        outcome=outcome,
        objective_value=objective,
        penalty=penalty,
        energy_kwh=energy,
        peak_motor_power_kw=peak_motor_power,
        total_duration_s=duration,
        demand=demand_series,
        violations=tuple(sorted(violations)),
        constraint_notes=tuple(notes),
        from_cache=False,
    )
    context.cache[vector] = evaluation
    context.record_violations(evaluation.violations)
    return evaluation


def infeasibility_diagnostic(evaluation: CandidateEvaluation) -> Diagnostic:
    """把候选违规转成可上报的 v2 诊断项（供 search_unresolved 时说明原因）。"""
    code = evaluation.violations[0] if evaluation.violations else "unknown_violation"
    detail = "; ".join(evaluation.violations[:3]) if evaluation.violations else None
    return Diagnostic(
        stage="evaluate",
        code=code,
        detail=detail,
        field_ref="constraint_checks",
        observed=evaluation.energy_kwh,
        unit="kWh",
        retryable=False,
    )
