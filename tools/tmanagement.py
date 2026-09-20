"""SOC trajectory and recharge budget for a selected D3 speed plan."""

from schemas.types import (
    ConstraintCheck, DataContext, InfeasibleType, ManagementPlan,
    OptimizationResult, SocPoint, SourceRef, Status, ToolResponse, VoyageRequest,
)
from schemas.validate import validate
from tools.energy_math import number


def _clarification(missing: list[str], questions: list[str]) -> ToolResponse:
    return ToolResponse("Tmanagement", Status.NEED_CLARIFICATION,
                        missing_fields=missing, questions=questions)


def run_tmanagement(request: VoyageRequest, data: DataContext,
                    optimization: OptimizationResult) -> ToolResponse:
    """Build a no-en-route-charge SOC plan without relaxing the adopted reserve."""
    if not optimization.feasible:
        reason = optimization.reason or "Tspeed未提供可行选定方案。"
        kind = optimization.infeasible_type or InfeasibleType.COMBINED
        return ToolResponse("Tmanagement", Status.INFEASIBLE,
                            reason=reason, infeasible_type=kind)
    checked = validate(optimization)
    if not checked.valid:
        return ToolResponse("Tmanagement", Status.INVALID_INPUT,
                            reason="optimization不符合公共接口。")
    vessel = data.vessel
    required = {
        "capacity_kwh": "请提供A批准的有效容量及来源。",
        "soc_initial": "请提供本次初始SOC及来源。",
        "soc_min": "请提供A批准的规划SOC下限及来源。",
        "auxiliary_power_kw": "请提供A批准的辅助功率及来源。",
    }
    missing, questions = [], []
    for field in data.missing_fields:
        missing.append("data." + field)
        questions.append(f"Tdata仍缺少{field}，不能进入能量管理。")
    for conflict in data.conflicts:
        if not conflict.resolution or not conflict.resolution.strip():
            missing.append("data.conflicts." + conflict.field_name)
            questions.append(f"请先解决参数冲突：{conflict.field_name}。")
    for field, question in required.items():
        if getattr(vessel, field) is None:
            missing.append("data.vessel." + field)
            questions.append(question)
        source = vessel.sources.get(field)
        if not isinstance(source, SourceRef) or not source.source_id.strip():
            missing.append("data.vessel.sources." + field)
            questions.append(f"请记录{field}的采用来源。")
    if request.soc_initial is None:
        missing.append("request.soc_initial")
        questions.append("请提供本次任务初始SOC。")
    if missing:
        unique = list(dict.fromkeys(zip(missing, questions)))
        return _clarification([item[0] for item in unique], [item[1] for item in unique])
    try:
        capacity = number(vessel.capacity_kwh, "capacity_kwh", minimum=0)
        if capacity == 0:
            raise ValueError("capacity_kwh must be positive")
        initial = number(vessel.soc_initial, "soc_initial", minimum=0)
        soc_min = number(vessel.soc_min, "soc_min", minimum=0)
        auxiliary_kw = number(vessel.auxiliary_power_kw, "auxiliary_power_kw", minimum=0)
        if not 0 <= initial <= 1 or not 0 <= soc_min <= 1 or initial < soc_min:
            raise ValueError("SOC must be within 0..1 and initial must not be below soc_min")
        if request.soc_initial != vessel.soc_initial:
            raise ValueError("request.soc_initial must equal vessel.soc_initial")
        if optimization.energy_scope not in ("propulsion", "total"):
            raise ValueError("optimization.energy_scope must be propulsion or total")
    except (TypeError, ValueError, OverflowError) as exc:
        return ToolResponse("Tmanagement", Status.INVALID_INPUT, reason=str(exc))

    try:
        auxiliary = number(auxiliary_kw * optimization.total_duration_h,
                           "auxiliary_energy_kwh", minimum=0)
        required_energy = optimization.total_energy_kwh
        if optimization.energy_scope == "propulsion":
            required_energy += auxiliary
        required_energy = number(required_energy, "required_energy_kwh", minimum=0)
    except (TypeError, ValueError, OverflowError) as exc:
        return ToolResponse("Tmanagement", Status.INVALID_INPUT, reason=str(exc))
    trajectory, current = [], initial
    for result in optimization.energy_results:
        segment_energy = result.energy_kwh
        if optimization.energy_scope == "propulsion":
            segment_energy += auxiliary_kw * result.duration_h
        current -= segment_energy / capacity
        # A negative mathematical remainder means the task depletes the pack
        # before segment end; report 0 as the physical lower bound and retain
        # the energy deficit in charge_required_kwh.
        trajectory.append(SocPoint(result.segment_id, max(current, 0.0)))
    available = capacity * max(initial - soc_min, 0)
    charge = max(required_energy - available, 0)
    safe = charge <= 1e-9 and all(point.soc >= soc_min - 1e-9 for point in trajectory)
    warnings = []
    final_soc = trajectory[-1].soc if trajectory else initial
    warning_value = data.parameters.get("soc_warning")
    critical_value = data.parameters.get("soc_critical")
    warning_soc = warning_value.value if warning_value is not None else None
    critical_soc = critical_value.value if critical_value is not None else None
    if isinstance(critical_soc, (int, float)) and final_soc < critical_soc:
        warnings.append(
            f"预计SOC低于{critical_soc:.0%}临界线；软件停止给出自动处置建议，"
            "交由操作员和BMS处理。"
        )
    elif isinstance(warning_soc, (int, float)) and final_soc < warning_soc:
        warnings.append(f"预计SOC低于{warning_soc:.0%}警告线；原方案不可继续作为安全计划。")
    elif vessel.soc_alarm is not None and final_soc < vessel.soc_alarm:
        warnings.append(
            f"预计到达SOC低于{vessel.soc_alarm:.0%}软件关注线；"
            "即使高于规划下限也需提示。"
        )
    assumptions = list(optimization.assumptions)
    assumptions.append(
        "双电池演示策略：组1推进优先，组2日常负载优先且必要时辅助推进；"
        "允许并联供电，但系统只输出建议，不下发断电或接触器控制命令。"
    )
    parallel_threshold = data.parameters.get("parallel_enter_propulsion_kw")
    threshold = parallel_threshold.value if parallel_threshold is not None else None
    if isinstance(threshold, (int, float)) and optimization.energy_results:
        peak = max(item.peak_power_kw or 0 for item in optimization.energy_results)
        if peak > threshold:
            assumptions.append(
                f"推进峰值{peak:g}kW超过{threshold:g}kW演示进入阈值，列为两组并联协同候选。"
            )
        else:
            assumptions.append(
                f"推进峰值{peak:g}kW未超过{threshold:g}kW演示进入阈值，建议保持常规角色分工。"
            )
    if optimization.energy_scope == "total":
        assumptions.append("辅助能耗已包含在total能耗中；分项由采用辅助功率乘总时长估算，不重复加入总需求")
    else:
        assumptions.append("辅助功率按全程恒定并覆盖航行与等待，已加到推进能耗口径")
    checks = [ConstraintCheck(
        "soc", safe, trajectory[-1].soc if trajectory else initial, soc_min, "fraction",
        "逐段SOC按同一采用有效容量扣减；未模拟途中补能。",
        vessel.sources["soc_min"],
    )]
    plan = ManagementPlan(safe, initial, trajectory[-1].soc if trajectory else initial,
                          soc_min, capacity, available, required_energy, auxiliary,
                          charge, trajectory, checks, warnings, assumptions)
    if not safe:
        reason = f"规划能量不足，至少需补充{charge:.6g}kWh后才能重新评估；未执行补能。"
        if not validate(plan).valid:
            return ToolResponse("Tmanagement", Status.FAILED,
                                reason="Tmanagement不可行预算记录未通过公共接口校验。")
        return ToolResponse("Tmanagement", Status.INFEASIBLE, payload=plan.to_dict(),
                            reason=reason, infeasible_type=InfeasibleType.SOC)
    response = ToolResponse("Tmanagement", Status.OK, plan.to_dict())
    if not validate(response).valid:
        return ToolResponse("Tmanagement", Status.FAILED,
                            reason="Tmanagement内部结果未通过公共接口校验。")
    return response
