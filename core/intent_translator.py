"""Translate an infeasible tool result into bounded, quantified user choices.

The translator never relaxes a hard safety lower bound on its own: options are
offered to the operator with tool-trial numbers, and an accepted direction is
translated into an explicit input modification that is re-run through the same
deterministic tools.  Numeric values come from the same Tenergy candidate grid
that produced the infeasible result, so no new number originates here.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Any

from schemas.types import (
    EnergyResult, InfeasibleType, OptimizationResult, Segment, VesselState,
    VoyageRequest,
)


# 不可行类型 → 相关妥协方向(规则化映射表, 运行时 LLM 不参与判断相关性)
DIRECTIONS_BY_TYPE: dict[InfeasibleType, list[str]] = {
    InfeasibleType.TIME: ["accept_late", "adjust_departure", "give_up"],
    InfeasibleType.SOC: ["accept_lower_soc", "recharge", "slow_down", "give_up"],
    InfeasibleType.POWER: ["slow_down", "give_up"],
    InfeasibleType.ROUTE: ["give_up"],
    InfeasibleType.COMBINED: ["accept_late", "accept_lower_soc", "recharge", "give_up"],
}


@dataclass(frozen=True)
class CompromiseOption:
    direction: str
    label: str
    quantified: dict[str, float | None]
    modification: dict[str, Any] | None
    requires_input: str | None = None


def build_boundary_diagnostics(
    segments: list[Segment],
    candidates: list[EnergyResult],
    vessel: VesselState,
    request: VoyageRequest,
    auxiliary_power_kw: float,
) -> dict[str, Any]:
    """Quantify the infeasible boundary from the same Tenergy candidate grid.

    Mirrors the D4 boundary diagnostics (scripts/validate_d4.py) so the options
    reuse identical numbers and never invent a plan.
    """
    by_segment: dict[str, list[EnergyResult]] = {
        segment.segment_id: [
            item for item in candidates if item.segment_id == segment.segment_id
        ]
        for segment in segments
    }
    if any(not items for items in by_segment.values()):
        raise ValueError("候选网格无法覆盖全部航段，无法进行边界诊断。")
    rows: list[tuple[float, float]] = []
    for choices in product(*(by_segment[segment.segment_id] for segment in segments)):
        duration_h = sum(item.duration_h for item in choices)
        propulsion_kwh = sum(item.energy_kwh for item in choices)
        required_kwh = propulsion_kwh + auxiliary_power_kw * duration_h
        rows.append((duration_h, required_kwh))
    if not rows:
        raise ValueError("无法从候选网格构造组合，无法进行边界诊断。")

    fastest_duration_h = min(item[0] for item in rows)
    time_limit = request.max_duration_h
    time_shortfall_h = (
        max(fastest_duration_h - time_limit, 0.0) if time_limit is not None else 0.0
    )
    within_time = [
        item for item in rows if time_limit is None or item[0] <= time_limit + 1e-9
    ]
    minimum_required_kwh = min((item[1] for item in within_time), default=None)

    available_kwh = None
    if vessel.capacity_kwh is not None and vessel.soc_initial is not None and vessel.soc_min is not None:
        available_kwh = vessel.capacity_kwh * (vessel.soc_initial - vessel.soc_min)
    minimum_charge_required_kwh = None
    if minimum_required_kwh is not None and available_kwh is not None:
        minimum_charge_required_kwh = max(minimum_required_kwh - available_kwh, 0.0)
    required_soc_min = None
    if (minimum_required_kwh is not None and vessel.capacity_kwh and vessel.soc_initial):
        required_soc_min = vessel.soc_initial - minimum_required_kwh / vessel.capacity_kwh

    return {
        "fastest_duration_h": fastest_duration_h,
        "time_shortfall_h": time_shortfall_h,
        "available_energy_above_soc_min_kwh": available_kwh,
        "minimum_required_energy_within_time_kwh": minimum_required_kwh,
        "minimum_charge_required_within_time_kwh": minimum_charge_required_kwh,
        "required_soc_min_within_time": required_soc_min,
        "diagnostic_only": True,
    }


def enumerate_options(
    optimization: OptimizationResult,
    request: VoyageRequest,
    vessel: VesselState,
    segments: list[Segment],
    candidates: list[EnergyResult],
    auxiliary_power_kw: float,
    warning_min: float = 0.25,
) -> list[CompromiseOption]:
    """Enumerate quantified compromise options for an infeasible result."""
    diagnostics = build_boundary_diagnostics(
        segments, candidates, vessel, request, auxiliary_power_kw,
    )
    kind = optimization.infeasible_type or InfeasibleType.COMBINED
    options: list[CompromiseOption] = []
    for direction in DIRECTIONS_BY_TYPE[kind]:
        option = _make_option(direction, request, vessel, diagnostics, warning_min)
        if option is not None:
            options.append(option)
    return options


def _make_option(
    direction: str,
    request: VoyageRequest,
    vessel: VesselState,
    diagnostics: dict[str, Any],
    warning_min: float,
) -> CompromiseOption | None:
    shortfall = diagnostics["time_shortfall_h"]
    charge = diagnostics["minimum_charge_required_within_time_kwh"]
    required_soc_min = diagnostics["required_soc_min_within_time"]
    soc_min = vessel.soc_min

    if direction == "accept_late":
        if shortfall <= 0:
            return None
        minutes = shortfall * 60
        return CompromiseOption(
            "accept_late",
            f"放宽到达截止时间至少 {shortfall:.2f} 小时（约 {minutes:.0f} 分钟）后重算",
            {"time_shortfall_h": shortfall},
            {"max_duration_h": (request.max_duration_h or 0.0) + shortfall},
        )
    if direction == "accept_lower_soc":
        if required_soc_min is None or soc_min is None or required_soc_min >= soc_min:
            return None
        if required_soc_min < warning_min:
            target = warning_min
            label = (
                f"将规划SOC下限由 {soc_min:.0%} 降至警告线 {target:.0%} 后重算"
                "（所需下限已低于警告线，不得再降）"
            )
        else:
            target = required_soc_min
            label = f"将规划SOC下限由 {soc_min:.0%} 降至 {target:.0%} 后重算"
        return CompromiseOption(
            "accept_lower_soc",
            label,
            {"required_soc_min": target},
            None,
            requires_input="降低规划SOC下限须经A批准，且不得低于警告线25%。",
        )
    if direction == "recharge":
        if charge is None or charge <= 0:
            return None
        return CompromiseOption(
            "recharge",
            f"补充至少 {charge:.2f} kWh 电量（需有来源的充电地点、功率与可用性）后重算",
            {"minimum_charge_required_kwh": charge},
            None,
            requires_input="补能需要提供有来源的充电地点、充电功率与可用性，系统不承诺未知补能可行。",
        )
    if direction == "slow_down":
        return CompromiseOption(
            "slow_down",
            "选择较低航速以降低功率与能耗（时间约束软化为省电优先）后重算",
            {},
            None,
            requires_input="降低航速将延长航时，需用户确认接受更晚到港。",
        )
    if direction == "adjust_departure":
        if request.departure_at is None or request.arrival_deadline is None:
            return None
        return CompromiseOption(
            "adjust_departure",
            "提前出发时间后重算（在固定到达截止时间下）",
            {},
            None,
            requires_input="需用户提供新的出发时间。",
        )
    if direction == "give_up":
        return CompromiseOption("give_up", "放弃当前任务，接受不可行结论", {}, None)
    return None


def translate_choice(
    direction: str,
    request: VoyageRequest,
    *,
    diagnostics: dict[str, Any] | None = None,
    segments: list[Segment] | None = None,
    candidates: list[EnergyResult] | None = None,
    vessel: VesselState | None = None,
    auxiliary_power_kw: float = 0.0,
) -> VoyageRequest:
    """Translate an accepted direction into a concrete request modification.

    Only directions that are fully determined by the current request are applied
    automatically (accept_late, give_up).  Directions that need external facts
    (a charging node, a new departure time, an A-approved SOC floor) raise
    ValueError so the caller prompts the operator instead of guessing.
    """
    if diagnostics is None and segments is not None and candidates is not None and vessel is not None:
        diagnostics = build_boundary_diagnostics(
            segments, candidates, vessel, request, auxiliary_power_kw,
        )
    if direction == "accept_late":
        shortfall = (diagnostics or {}).get("time_shortfall_h", 0.0)
        if shortfall <= 0:
            raise ValueError("accept_late 需要正的 time_shortfall_h。")
        return VoyageRequest(
            origin=request.origin,
            destination=request.destination,
            departure_at=request.departure_at,
            arrival_deadline=request.arrival_deadline,
            max_duration_h=(request.max_duration_h or 0.0) + shortfall,
            soc_initial=request.soc_initial,
            load_state=request.load_state,
            draft_m=request.draft_m,
            environment=request.environment,
        )
    if direction == "give_up":
        return request
    raise ValueError(f"方向 {direction} 需要额外用户输入，无法自动翻译。")
