"""Finite-grid speed feasibility search for D3.

The search only chooses among supplied Tenergy candidates.  It does not invent
vessel limits, interpolate a continuous optimum, or relax safety constraints.
"""

from datetime import datetime, timedelta
from itertools import product
from math import isclose, prod

from schemas.types import (
    ConstraintCheck, EnergyResult, InfeasibleType, OptimizationResult, Segment,
    SourceRef, Status, ToolResponse, VesselState, VoyageRequest,
)
from schemas.validate import validate
from tools.energy_math import number


def _clarification(missing: list[str], questions: list[str]) -> ToolResponse:
    return ToolResponse("Tspeed", Status.NEED_CLARIFICATION,
                        missing_fields=missing, questions=questions)


def _timestamp(value: str | None, name: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include timezone")
    return parsed


def _infeasible(kind: InfeasibleType, reason: str, scope: str,
                description: str, assumptions: list[str]) -> ToolResponse:
    result = OptimizationResult(False, scope, infeasible_type=kind, reason=reason,
                                search_description=description, assumptions=assumptions)
    return ToolResponse("Tspeed", Status.INFEASIBLE, payload=result.to_dict(),
                        reason=reason, infeasible_type=kind)


def run_tspeed(request: VoyageRequest, segments: list[Segment],
               candidates: list[EnergyResult], vessel: VesselState,
               *, max_combinations: int = 100_000) -> ToolResponse:
    """Select minimum-energy feasible choices from an explicit finite grid."""
    missing, questions = [], []
    if not segments:
        missing.append("segments")
        questions.append("请先提供至少一个完整航段。")
    if not candidates:
        missing.append("candidate_results")
        questions.append("请先由Tenergy提供候选能耗结果。")
    required_vessel = {
        "capacity_kwh": "请提供A批准的有效容量及来源。",
        "soc_initial": "请提供本次任务初始SOC及来源。",
        "soc_min": "请提供A批准的规划SOC下限及来源。",
        "max_power_kw": "请提供与模型同一边界的A批准功率上限及来源。",
        "auxiliary_power_kw": "请提供A批准的辅助功率及来源。",
    }
    for field, question in required_vessel.items():
        if getattr(vessel, field) is None:
            missing.append("vessel." + field)
            questions.append(question)
        source = vessel.sources.get(field)
        if not isinstance(source, SourceRef) or not source.source_id.strip():
            missing.append("vessel.sources." + field)
            questions.append(f"请记录{field}的采用来源。")
    if request.soc_initial is None:
        missing.append("request.soc_initial")
        questions.append("请提供本次任务初始SOC。")
    if request.max_duration_h is None and request.arrival_deadline is None:
        missing.append("request.arrival_deadline_or_max_duration_h")
        questions.append("请提供到达截止时间或最长耗时。")
    if request.arrival_deadline is not None and request.departure_at is None:
        missing.append("request.departure_at")
        questions.append("使用到达截止时间时必须提供带时区出发时间。")
    for index, segment in enumerate(segments):
        checked = validate(segment)
        if checked.status == Status.NEED_CLARIFICATION:
            for issue in checked.issues:
                missing.append(f"segments[{index}].{issue.field}")
                questions.append(f"请补充航段{segment.segment_id}的{issue.field}。")
        elif not checked.valid:
            return ToolResponse("Tspeed", Status.INVALID_INPUT,
                                reason=f"segments[{index}]不符合公共接口。")
    if missing:
        # Preserve first occurrence order while avoiding duplicate questions.
        unique = list(dict.fromkeys(zip(missing, questions)))
        return _clarification([item[0] for item in unique], [item[1] for item in unique])

    try:
        capacity = number(vessel.capacity_kwh, "capacity_kwh", minimum=0)
        if capacity == 0:
            raise ValueError("capacity_kwh must be positive")
        initial = number(vessel.soc_initial, "soc_initial", minimum=0)
        soc_min = number(vessel.soc_min, "soc_min", minimum=0)
        power_limit = number(vessel.max_power_kw, "max_power_kw", minimum=0)
        auxiliary = number(vessel.auxiliary_power_kw, "auxiliary_power_kw", minimum=0)
        if power_limit == 0:
            raise ValueError("max_power_kw must be positive")
        if not 0 <= initial <= 1 or not 0 <= soc_min <= 1 or initial < soc_min:
            raise ValueError("SOC must be within 0..1 and initial must not be below soc_min")
        if request.soc_initial != vessel.soc_initial:
            raise ValueError("request.soc_initial must equal vessel.soc_initial")
        departure = _timestamp(request.departure_at, "departure_at")
        deadline = _timestamp(request.arrival_deadline, "arrival_deadline")
        limits = []
        if request.max_duration_h is not None:
            duration_limit = number(request.max_duration_h, "max_duration_h", minimum=0)
            if duration_limit == 0:
                raise ValueError("max_duration_h must be positive")
            limits.append(duration_limit)
        if deadline is not None:
            if deadline <= departure:
                raise ValueError("arrival_deadline must be later than departure_at")
            limits.append((deadline - departure).total_seconds() / 3600)
        time_limit = min(limits)
        if type(max_combinations) is not int or max_combinations <= 0:
            raise ValueError("max_combinations must be a positive integer")
    except (TypeError, ValueError, OverflowError) as exc:
        return ToolResponse("Tspeed", Status.INVALID_INPUT, reason=str(exc))

    by_segment = {segment.segment_id: [] for segment in segments}
    segment_lookup = {segment.segment_id: segment for segment in segments}
    if len(segment_lookup) != len(segments):
        return ToolResponse("Tspeed", Status.INVALID_INPUT, reason="航段ID不能重复。")
    scope = None
    assumptions = []
    for index, candidate in enumerate(candidates):
        checked = validate(candidate)
        if not checked.valid:
            return ToolResponse("Tspeed", Status.INVALID_INPUT,
                                reason=f"candidate_results[{index}]不符合公共接口。")
        if candidate.segment_id not in by_segment:
            return ToolResponse("Tspeed", Status.INVALID_INPUT,
                                reason=f"候选引用未知航段：{candidate.segment_id}")
        segment = segment_lookup[candidate.segment_id]
        expected_duration = segment.distance_km / candidate.speed_kmh + segment.waiting_h
        if not isclose(candidate.duration_h, expected_duration, rel_tol=1e-6, abs_tol=1e-6):
            return ToolResponse("Tspeed", Status.INVALID_INPUT,
                                reason=f"{candidate.segment_id}候选耗时与距离、航速、等待不一致。")
        if scope is None:
            scope = candidate.energy_scope
        elif scope != candidate.energy_scope:
            return ToolResponse("Tspeed", Status.INVALID_INPUT,
                                reason="所有候选必须使用同一能耗口径。")
        if candidate.energy_scope not in ("propulsion", "total"):
            return ToolResponse("Tspeed", Status.INVALID_INPUT,
                                reason="候选能耗口径只允许propulsion或total。")
        try:
            for name in ("speed_kmh", "power_kw", "duration_h", "energy_kwh",
                         "peak_power_kw"):
                if getattr(candidate, name) is not None:
                    number(getattr(candidate, name), name, minimum=0)
        except (TypeError, ValueError, OverflowError) as exc:
            return ToolResponse("Tspeed", Status.INVALID_INPUT, reason=str(exc))
        if candidate.peak_power_kw is None:
            missing.append(f"candidate_results[{index}].peak_power_kw")
            questions.append("功率约束已启用，候选必须提供同边界模型峰值。")
        by_segment[candidate.segment_id].append(candidate)
        for note in candidate.assumptions:
            if note not in assumptions:
                assumptions.append(note)
    if missing:
        return _clarification(missing, questions)
    for segment_id, items in by_segment.items():
        speeds = [item.speed_kmh for item in items]
        if len(set(speeds)) != len(speeds):
            return ToolResponse("Tspeed", Status.INVALID_INPUT,
                                reason=f"航段{segment_id}的候选航速不能重复。")
    empty = [segment_id for segment_id, items in by_segment.items() if not items]
    if empty:
        return _clarification([f"candidate_results.{item}" for item in empty],
                              [f"航段{item}没有候选能耗结果。" for item in empty])

    eligible = []
    for segment in segments:
        items = [item for item in by_segment[segment.segment_id]
                 if item.speed_kmh <= segment.max_speed_kmh
                 and (segment.min_speed_kmh is None or item.speed_kmh >= segment.min_speed_kmh)
                 and item.peak_power_kw <= power_limit]
        if not items:
            if any(item.peak_power_kw > power_limit for item in by_segment[segment.segment_id]):
                return _infeasible(InfeasibleType.POWER,
                    f"航段{segment.segment_id}没有满足{power_limit}kW功率上限的候选。",
                    scope, "有限候选网格穷举；未声称连续全局最优。", assumptions)
            return _infeasible(InfeasibleType.ROUTE,
                f"航段{segment.segment_id}没有满足限速范围的候选。",
                scope, "有限候选网格穷举；未声称连续全局最优。", assumptions)
        eligible.append(items)
    combinations = prod(len(items) for items in eligible)
    if combinations > max_combinations:
        return ToolResponse("Tspeed", Status.INVALID_INPUT,
                            reason=f"候选组合{combinations}超过上限{max_combinations}，请缩小网格。")

    budget = capacity * (initial - soc_min)
    feasible, time_pass, soc_pass = [], False, False
    for choices in product(*eligible):
        try:
            duration = number(sum(item.duration_h for item in choices), "total_duration_h", minimum=0)
            energy = number(sum(item.energy_kwh for item in choices), "total_energy_kwh", minimum=0)
            required = number(energy + (auxiliary * duration if scope == "propulsion" else 0),
                              "required_energy_kwh", minimum=0)
        except (TypeError, ValueError, OverflowError) as exc:
            return ToolResponse("Tspeed", Status.INVALID_INPUT, reason=str(exc))
        current_time = duration <= time_limit + 1e-9
        current_soc = required <= budget + 1e-9
        time_pass = time_pass or current_time
        soc_pass = soc_pass or current_soc
        if current_time and current_soc:
            feasible.append((required, duration, tuple(item.speed_kmh for item in choices),
                             energy, choices))
    description = (f"有限候选网格穷举{combinations}种组合；按总能耗、总耗时、航速序列排序；"
                   "仅保证所给离散网格内最优。")
    if not feasible:
        if time_pass and not soc_pass:
            kind, reason = InfeasibleType.SOC, "候选中有时间可行组合，但均超过规划SOC预算。"
        elif soc_pass and not time_pass:
            kind, reason = InfeasibleType.TIME, "候选中有SOC可行组合，但均超过到达时间约束。"
        else:
            kind, reason = InfeasibleType.COMBINED, "候选网格内时间与SOC约束无法同时满足。"
        return _infeasible(kind, reason, scope, description, assumptions)
    _, duration, _, energy, selected = min(feasible, key=lambda item: item[:3])
    eta = (departure + timedelta(hours=duration)).isoformat() if departure else None
    checks = [
        ConstraintCheck("time", True, duration, time_limit, "h",
                        "总耗时满足用户给出的最严格时间约束。",
                        SourceRef("voyage_request", "user", confirmed=True)),
        ConstraintCheck("speed", True, max(item.speed_kmh for item in selected), None, "km/h",
                        "逐航段核对最低/最高速度。"),
        ConstraintCheck("power", True, max(item.peak_power_kw for item in selected),
                        power_limit, "kW", "逐航段模型峰值不超过同边界采用上限。",
                        vessel.sources["max_power_kw"]),
        ConstraintCheck("soc", True,
                        initial - (energy + (auxiliary * duration if scope == "propulsion" else 0)) / capacity,
                        soc_min, "fraction", "阶段预算检查；最终轨迹由Tmanagement复核。",
                        vessel.sources["soc_min"]),
    ]
    result = OptimizationResult(True, scope, list(selected), energy, duration, eta,
                                checks=checks, search_description=description,
                                assumptions=assumptions)
    response = ToolResponse("Tspeed", Status.OK, result.to_dict())
    if not validate(response).valid:
        return ToolResponse("Tspeed", Status.FAILED,
                            reason="Tspeed内部结果未通过公共接口校验。")
    return response
