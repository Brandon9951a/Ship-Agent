"""独立约束检查器：与 DE 目标函数分离，复核最终候选。

任务书要求"每个可行解必须通过独立检查器，且时间、距离、能量汇总与展开轨迹一致"。
本模块：
* 用核验步长重跑轨迹展开与 Tenergy 预测，报告与优化阶段粗网格的能量差；
* 逐项检查时间、速度、加速度、距离、同边界功率与电量；
* 用共享需求构建器复算母线侧能量守恒（母线侧无依据时不给通过结论）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from research.contracts import (
    CandidateTrajectory,
    ConstraintCheckRecord,
    Diagnostic,
    EnergyBatchRequest,
    EnergyResultEnvelope,
    OutcomeCode,
    PowerDemandSeries,
    SpeedOptimizationRequest,
)
from research.demand import (
    DemandBuildError,
    build_power_demand,
    bus_power_limit_check,
    energy_balance_check,
)
from research.tspeed.eval import EnergyModelSpec
from research.tspeed.trajectory import (
    Violation,
    expand_trajectory,
    fixed_speed_floor,
    segment_handoff_floor,
)

VERIFICATION_TOLERANCE_KM = 1e-6
VERIFICATION_TOLERANCE_S = 1e-6


@dataclass(frozen=True)
class VerificationReport:
    """最终候选的核验结论；``passed`` 只表示在给定来源与假设下通过。"""

    passed: bool
    checks: tuple[ConstraintCheckRecord, ...]
    energy_kwh: float | None
    coarse_energy_kwh: float | None
    resampling_error_kwh: float | None
    peak_motor_power_kw: float | None
    total_duration_s: float
    distance_km: float
    distance_residual_km: float
    demand: PowerDemandSeries | None
    demand_energy_kwh: float | None
    energy_envelope: EnergyResultEnvelope | None
    diagnostics: tuple[Diagnostic, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def failed_checks(self) -> tuple[str, ...]:
        return tuple(check.name for check in self.checks if not check.passed)


def verify_candidate(candidate: CandidateTrajectory, *,
                     request: SpeedOptimizationRequest,
                     spec: EnergyModelSpec,
                     coarse_energy_kwh: float | None = None,
                     environment_ref: str | None = None,
                     history_ref: str | None = None,
                     call_id: str = "verify",
                     parent_call_id: str | None = None) -> VerificationReport:
    """按核验精度重新展开并预测，逐项独立检查。"""
    grid = request.time_grid
    checks: list[ConstraintCheckRecord] = []
    diagnostics: list[Diagnostic] = []
    environment_ref = environment_ref or spec.environment_ref
    history_ref = history_ref or spec.history_ref

    # 1) 用核验精度重新展开：核验网格可以比规划网格更细，用于量化重采样误差。
    # 决策向量按航段复原（一个航段有多个区间，不能直接用区间序列）。
    optimizable_ids = request.optimizable_segment_ids
    by_segment = dict(candidate.speed_by_segment())
    missing = [segment_id for segment_id in optimizable_ids
               if by_segment.get(segment_id) is None]
    if missing:
        raise ValueError(f"候选轨迹缺少可优化航段的计划航速：{missing}")
    decision_vector = tuple(by_segment[segment_id] for segment_id in optimizable_ids)
    expanded = expand_trajectory(decision_vector, request=request,
                                time_grid=grid,
                                max_step_s=grid.verification_step_s)

    # 2) 速度与加速度检查（独立复核展开结果本身，含逐航段限速比对）。
    speed_violations = [item for item in expanded.violations
                        if item.code in ("speed_limit_exceeded_for_distance",
                                         "distance_too_short_for_speed_limit",
                                         "zero_cruise_speed_on_positive_distance")]
    limits_by_segment = {}
    for index, segment in enumerate(request.segments):
        low = max(grid.min_speed_kmh,
                  segment.min_speed_kmh if segment.min_speed_kmh is not None
                  else grid.min_speed_kmh)
        high = min(grid.max_speed_kmh,
                   segment.max_speed_kmh if segment.max_speed_kmh is not None
                   else grid.max_speed_kmh)
        next_segment = (request.segments[index + 1]
                        if index + 1 < len(request.segments) else None)
        # 交接速度下限：两侧减速目标下限取较大者（与展开器同一规则）。
        # 巡航区间必须达到本段限速下限；过渡斜坡在 [全局下限, 本段上限] 内即可。
        handoff_floor = segment_handoff_floor(segment, next_segment, grid)
        cruise_floor = fixed_speed_floor(segment, grid)
        limits_by_segment[segment.segment_id] = (
            max(grid.min_speed_kmh, min(low, handoff_floor)), high,
            segment.fixed_speed_kmh, cruise_floor)
    observed_max = 0.0
    for fact in expanded.segment_facts:
        low, high, fixed_speed, cruise_floor = limits_by_segment[fact.segment_id]
        observed_max = max(observed_max,
                           max((item.planned_speed_kmh for item in fact.sampled),
                               default=0.0))
        for interval in fact.sampled:
            speed = interval.planned_speed_kmh
            if speed > high + 1e-6 or speed < low - 1e-6:
                speed_violations.append(Violation(
                    code="segment_speed_out_of_range", segment_id=fact.segment_id,
                    detail=f"区间航速 {speed:.6f} 超出航段限速 [{low}, {high}]",
                    observed=speed, limit=(high if speed > high else low), unit="km/h"))
            if interval.kind == "cruise":
                if speed < cruise_floor - 1e-6:
                    speed_violations.append(Violation(
                        code="cruise_below_speed_floor", segment_id=fact.segment_id,
                        detail=f"巡航航速 {speed:.6f} 低于该航段限速下限 {cruise_floor}",
                        observed=speed, limit=cruise_floor, unit="km/h"))
                if fixed_speed is not None and abs(speed - fixed_speed) > 1e-6:
                    speed_violations.append(Violation(
                        code="fixed_segment_speed_changed", segment_id=fact.segment_id,
                        detail=(f"固定航段巡航速度被改为 {speed:.6f}"
                                f"（应为 {fixed_speed}）"),
                        observed=speed, limit=fixed_speed, unit="km/h"))
    checks.append(ConstraintCheckRecord(
        name="speed", passed=not speed_violations,
        actual=observed_max, limit=grid.max_speed_kmh, unit="km/h",
        reason="; ".join(item.detail for item in speed_violations) or "逐区间航速在限速范围内"))
    acceleration_violations = expanded.audit_acceleration(grid)
    checks.append(ConstraintCheckRecord(
        name="acceleration", passed=not acceleration_violations,
        actual=max((abs(interval.acceleration_mps2) for interval in expanded.intervals),
                   default=0.0),
        limit=max(grid.max_acceleration_mps2, grid.max_deceleration_mps2),
        unit="m/s^2",
        reason="; ".join(item.detail for item in acceleration_violations)
        or "逐区间平均加速度未越过采用上限"))
    continuity = expanded.audit_continuity()
    checks.append(ConstraintCheckRecord(
        name="trajectory_continuity", passed=not continuity,
        reason="; ".join(item.detail for item in continuity) or "时间轴无重叠与未解释间隙"))

    # 3) 距离一致性：以展开轨迹的速度积分复核航段距离。
    checks.append(ConstraintCheckRecord(
        name="distance", passed=expanded.distance_consistent,
        actual=expanded.declared_distance_km, limit=expanded.requested_distance_km,
        unit="km", tolerance=grid.distance_tolerance_km,
        reason=f"积分残差 {expanded.distance_residual_km:.9f} km"))

    # 4) 时间检查。
    limit_s = request.remaining_time_s
    if limit_s is not None:
        checks.append(ConstraintCheckRecord(
            name="time", passed=expanded.total_duration_s <= limit_s + VERIFICATION_TOLERANCE_S,
            actual=expanded.total_duration_s, limit=limit_s, unit="s",
            tolerance=VERIFICATION_TOLERANCE_S,
            reason="总历时（含等待）与剩余期限比较"))
    else:
        checks.append(ConstraintCheckRecord(
            name="time", passed=True, actual=expanded.total_duration_s, limit=None,
            unit="s", reason="未给出剩余期限，仅报告历时"))

    # 5) 核验网格上重新预测。
    verification_candidate = CandidateTrajectory(
        candidate_id=f"{candidate.candidate_id}-verify",
        trajectory=expanded.to_trajectory_points(environment_ref=environment_ref))
    batch_request = EnergyBatchRequest(
        candidates=(verification_candidate,), history_ref=history_ref,
        environment_scenario_ref=environment_ref,
        model_version=spec.model_version or spec.predictor.model_version,
        integration_rule=spec.integration_rule, task_id=request.task_id,
        request_revision=request.request_revision, snapshot_id=request.snapshot_id,
        scenario_id=request.scenario_id, call_id=call_id,
        parent_call_id=parent_call_id)
    batch_result = spec.predictor.predict(batch_request, grid=spec.grid)
    envelope = batch_result.by_candidate().get(verification_candidate.candidate_id)
    if envelope is None:
        raise RuntimeError("核验预测未返回结果")
    if not envelope.ok:
        diagnostics.extend(envelope.diagnostics)
        checks.append(ConstraintCheckRecord(
            name="prediction", passed=False,
            reason=f"核验预测未成功：{envelope.outcome_code}"))
    else:
        balance = envelope.energies_match()
        checks.append(ConstraintCheckRecord(
            name="energy_integration", passed=balance,
            reason="逐时能量与 P·Δt/3600 一致" if balance else "逐时能量与积分区间不一致"))

    energy = envelope.predicted_motor_energy_kwh
    resampling_error = None
    if energy is not None and coarse_energy_kwh is not None:
        resampling_error = energy - coarse_energy_kwh
        diagnostics.append(Diagnostic(
            stage="verify", code="resampling_difference",
            detail="优化阶段规划网格与核验精度的能量差",
            field_ref="total_energy_kwh", observed=resampling_error, unit="kWh",
            retryable=False))

    # 6) 共享需求构建器（唯一转换位置）与母线侧检查。
    demand: PowerDemandSeries | None = None
    demand_energy: float | None = None
    notes: list[str] = []
    if spec.demand_spec is not None:
        try:
            demand = build_power_demand(
                envelope, spec=spec.demand_spec,
                series_id=f"demand-{verification_candidate.candidate_id}",
                input_energy_ref=(envelope.artifact_ref.artifact_id
                                  if envelope.artifact_ref else None))
            diagnostics.extend(demand.diagnostics)
        except DemandBuildError as exc:
            checks.append(ConstraintCheckRecord(
                name="power_demand", passed=False, reason=str(exc)))
        if demand is not None:
            demand_energy = demand.demand_energy_kwh()
            conservation = energy_balance_check(demand)
            checks.append(ConstraintCheckRecord(
                name="energy_conservation", passed=bool(conservation.get("passed")),
                actual=conservation.get("residual_kwh"),
                limit=conservation.get("tolerance_kwh"), unit="kWh",
                reason=("母线侧能量守恒检查通过（需求 = 推进 + 辅助）"
                        if conservation.get("applicable")
                        and conservation.get("passed")
                        else str(conservation.get("reason")) if not
                        conservation.get("applicable")
                        else "母线侧能量守恒未通过")))
            if spec.demand_limit_kw is not None:
                limit_check = bus_power_limit_check(
                    demand, spec.demand_limit_kw)
                checks.append(ConstraintCheckRecord(
                    name="power", passed=bool(limit_check.get("passed")),
                    actual=limit_check.get("peak_demand_kw"),
                    limit=spec.demand_limit_kw, unit="kW",
                    reason=("同边界母线峰值与上限比较" if limit_check.get("applicable")
                            else str(limit_check.get("reason"))),
                    source_ref=request.power_limit_source_ref))
                if not limit_check.get("applicable"):
                    notes.append("母线侧无依据：未据此判定功率约束通过")
            if spec.energy_budget_kwh is not None:
                applicable = demand_energy is not None
                checks.append(ConstraintCheckRecord(
                    name="energy_budget", passed=(applicable and
                                                  demand_energy <= spec.energy_budget_kwh + 1e-9),
                    actual=demand_energy, limit=spec.energy_budget_kwh, unit="kWh",
                    reason=("母线需求与预算比较" if applicable
                            else "母线需求不可用，预算检查不适用"),
                    source_ref=request.battery_budget_source_ref))
                if not applicable:
                    notes.append("母线侧无依据：能量预算检查不适用")
    else:
        checks.append(ConstraintCheckRecord(
            name="power_demand", passed=True,
            reason="未提供需求构建器：本次仅电机侧对照实验，不检查母线侧约束"))
        notes.append("未提供需求构建器：结果只覆盖电机侧，不构成完整实船安全方案")

    passed = all(check.passed for check in checks)
    for check in checks:
        if not check.passed:
            diagnostics.append(Diagnostic(
                stage="verify", code=f"check_failed.{check.name}",
                detail=check.reason, field_ref=check.name,
                observed=check.actual, limit=check.limit, unit=check.unit,
                source_refs=(check.source_ref,) if check.source_ref else (),
                retryable=False))
    return VerificationReport(
        passed=passed, checks=tuple(checks), energy_kwh=energy,
        coarse_energy_kwh=coarse_energy_kwh, resampling_error_kwh=resampling_error,
        peak_motor_power_kw=envelope.peak_motor_power_kw,
        total_duration_s=expanded.total_duration_s,
        distance_km=expanded.declared_distance_km,
        distance_residual_km=expanded.distance_residual_km,
        demand=demand, demand_energy_kwh=demand_energy, energy_envelope=envelope,
        diagnostics=tuple(diagnostics), notes=tuple(notes),
    )