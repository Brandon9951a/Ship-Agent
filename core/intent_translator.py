"""Build concrete, pre-checked alternatives for an infeasible voyage request.

Every offered modification is derived from the same finite Tenergy grid used by
Tspeed. Generic advice is omitted: if a change cannot satisfy the current time,
power and SOC constraints, it is not returned to the operator.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import product
from math import ceil
from typing import Any

from schemas.types import (
    EnergyResult, InfeasibleType, OptimizationResult, Segment, VesselState,
    VoyageRequest,
)


DIRECTIONS_BY_TYPE: dict[InfeasibleType, list[str]] = {
    InfeasibleType.TIME: ["accept_late", "adjust_departure", "shorten_route", "give_up"],
    InfeasibleType.SOC: ["recharge", "shorten_route", "give_up"],
    InfeasibleType.POWER: ["shorten_route", "give_up"],
    InfeasibleType.ROUTE: ["shorten_route", "give_up"],
    InfeasibleType.COMBINED: [
        "accept_late", "recharge", "recharge_and_extend", "shorten_route", "give_up",
    ],
}


@dataclass(frozen=True)
class CompromiseOption:
    direction: str
    label: str
    quantified: dict[str, float | None]
    modification: dict[str, Any] | None
    requires_input: str | None = None
    preview: dict[str, Any] | None = None
    verified: bool = False


@dataclass(frozen=True)
class _CandidatePlan:
    choices: tuple[EnergyResult, ...]
    duration_h: float
    propulsion_energy_kwh: float
    required_energy_kwh: float


def _round_up(value: float, digits: int) -> float:
    scale = 10 ** digits
    return ceil((value - 1e-12) * scale) / scale


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else None


def _time_limit(request: VoyageRequest) -> float | None:
    limits: list[float] = []
    if request.max_duration_h is not None:
        limits.append(float(request.max_duration_h))
    departure = _parse_timestamp(request.departure_at)
    deadline = _parse_timestamp(request.arrival_deadline)
    if departure is not None and deadline is not None:
        limits.append((deadline - departure).total_seconds() / 3600)
    return min(limits) if limits else None


def _available_energy(vessel: VesselState, soc_initial: float | None = None) -> float | None:
    initial = vessel.soc_initial if soc_initial is None else soc_initial
    if vessel.capacity_kwh is None or initial is None or vessel.soc_min is None:
        return None
    return vessel.capacity_kwh * max(initial - vessel.soc_min, 0.0)


def _candidate_plans(
    segments: list[Segment],
    candidates: list[EnergyResult],
    vessel: VesselState,
    auxiliary_power_kw: float,
) -> list[_CandidatePlan]:
    """Enumerate plans after applying the same speed and power filters as Tspeed."""
    if not segments or vessel.max_power_kw is None:
        return []
    grids: list[list[EnergyResult]] = []
    for segment in segments:
        items = []
        for item in candidates:
            if item.segment_id != segment.segment_id or item.peak_power_kw is None:
                continue
            if segment.max_speed_kmh is not None and item.speed_kmh > segment.max_speed_kmh:
                continue
            if segment.min_speed_kmh is not None and item.speed_kmh < segment.min_speed_kmh:
                continue
            if item.peak_power_kw > vessel.max_power_kw:
                continue
            items.append(item)
        if not items:
            return []
        grids.append(items)

    plans = []
    for choices in product(*grids):
        scopes = {item.energy_scope for item in choices}
        if len(scopes) != 1:
            continue
        duration = sum(item.duration_h for item in choices)
        propulsion = sum(item.energy_kwh for item in choices)
        required = propulsion + (
            auxiliary_power_kw * duration if next(iter(scopes)) == "propulsion" else 0.0
        )
        plans.append(_CandidatePlan(tuple(choices), duration, propulsion, required))
    return plans


def _preview(
    plan: _CandidatePlan,
    request: VoyageRequest,
    vessel: VesselState,
    *,
    soc_initial: float | None = None,
    segments: list[Segment] | None = None,
) -> dict[str, Any]:
    initial = vessel.soc_initial if soc_initial is None else soc_initial
    final_soc = None
    if initial is not None and vessel.capacity_kwh:
        final_soc = initial - plan.required_energy_kwh / vessel.capacity_kwh
    departure = _parse_timestamp(request.departure_at)
    eta = (departure + timedelta(hours=plan.duration_h)).isoformat() if departure else None
    route = None
    distance = None
    if segments:
        route = f"{segments[0].origin} → {segments[-1].destination}"
        distance = sum(item.distance_km for item in segments)
    return {
        "route": route,
        "distance_km": distance,
        "duration_h": plan.duration_h,
        "required_energy_kwh": plan.required_energy_kwh,
        "soc_initial": initial,
        "soc_final": final_soc,
        "eta": eta,
        "speeds_kmh": [item.speed_kmh for item in plan.choices],
    }


def build_boundary_diagnostics(
    segments: list[Segment],
    candidates: list[EnergyResult],
    vessel: VesselState,
    request: VoyageRequest,
    auxiliary_power_kw: float,
) -> dict[str, Any]:
    plans = _candidate_plans(segments, candidates, vessel, auxiliary_power_kw)
    if not plans:
        raise ValueError("候选网格没有通过航速和功率边界的完整组合。")
    limit = _time_limit(request)
    fastest = min(item.duration_h for item in plans)
    shortfall = max(fastest - limit, 0.0) if limit is not None else 0.0
    within_time = [item for item in plans if limit is None or item.duration_h <= limit + 1e-9]
    minimum_required = min((item.required_energy_kwh for item in within_time), default=None)
    available = _available_energy(vessel)
    minimum_charge = None
    if minimum_required is not None and available is not None:
        minimum_charge = max(minimum_required - available, 0.0)
    required_soc = None
    if minimum_required is not None and vessel.capacity_kwh and vessel.soc_min is not None:
        required_soc = vessel.soc_min + minimum_required / vessel.capacity_kwh
    energy_feasible = [
        item for item in plans
        if available is not None and item.required_energy_kwh <= available + 1e-9
    ]
    minimum_energy_feasible_duration = min(
        (item.duration_h for item in energy_feasible), default=None,
    )
    return {
        "fastest_duration_h": fastest,
        "time_shortfall_h": shortfall,
        "available_energy_above_soc_min_kwh": available,
        "minimum_required_energy_within_time_kwh": minimum_required,
        "minimum_charge_required_within_time_kwh": minimum_charge,
        "required_soc_initial_within_time": required_soc,
        "minimum_energy_feasible_duration_h": minimum_energy_feasible_duration,
        "candidate_plan_count": len(plans),
        "diagnostic_only": True,
    }


def _recharge_option(
    plans: list[_CandidatePlan], request: VoyageRequest, vessel: VesselState,
    segments: list[Segment],
) -> CompromiseOption | None:
    limit = _time_limit(request)
    within_time = [item for item in plans if limit is None or item.duration_h <= limit + 1e-9]
    if not within_time or not vessel.capacity_kwh or vessel.soc_min is None:
        return None
    selected = min(within_time, key=lambda item: (item.required_energy_kwh, item.duration_h))
    target_soc = _round_up(vessel.soc_min + selected.required_energy_kwh / vessel.capacity_kwh, 3)
    if target_soc > 1.0 or vessel.soc_initial is None or target_soc <= vessel.soc_initial + 1e-9:
        return None
    charge = (target_soc - vessel.soc_initial) * vessel.capacity_kwh
    preview = _preview(selected, request, vessel, soc_initial=target_soc, segments=segments)
    return CompromiseOption(
        "recharge",
        f"出发前补能至实测 SOC 至少 {target_soc:.1%}｜预计 {selected.duration_h:.2f} h、"
        f"{selected.required_energy_kwh:.1f} kWh、到达 SOC {preview['soc_final']:.1%}",
        {
            "minimum_charge_required_kwh": charge,
            "required_soc_initial": target_soc,
            "duration_h": selected.duration_h,
            "required_energy_kwh": selected.required_energy_kwh,
            "soc_final": preview["soc_final"],
        },
        {"soc_initial": target_soc},
        requires_input="仅在补能完成并确认实测 SOC 后执行；未验证充电设施的现场可用性。",
        preview=preview,
        verified=True,
    )


def _time_option(
    plans: list[_CandidatePlan], request: VoyageRequest, vessel: VesselState,
    segments: list[Segment],
) -> CompromiseOption | None:
    available = _available_energy(vessel)
    limit = _time_limit(request)
    if available is None or limit is None:
        return None
    energy_feasible = [item for item in plans if item.required_energy_kwh <= available + 1e-9]
    if not energy_feasible:
        return None
    selected = min(energy_feasible, key=lambda item: (item.duration_h, item.required_energy_kwh))
    if selected.duration_h <= limit + 1e-9:
        return None
    duration = _round_up(selected.duration_h, 3)
    modification: dict[str, Any] = {"max_duration_h": duration}
    direction = "accept_late"
    label_prefix = f"将最长航时放宽至 {duration:.3f} h"
    deadline = _parse_timestamp(request.arrival_deadline)
    if deadline is not None:
        departure = deadline - timedelta(hours=duration)
        modification["departure_at"] = departure.isoformat()
        direction = "adjust_departure"
        label_prefix = f"提前至 {departure.strftime('%m-%d %H:%M')} 出发"
    preview = _preview(selected, request, vessel, segments=segments)
    return CompromiseOption(
        direction,
        f"{label_prefix}｜预计能耗 {selected.required_energy_kwh:.1f} kWh、"
        f"到达 SOC {preview['soc_final']:.1%}",
        {
            "required_duration_h": duration,
            "time_shortfall_h": duration - limit,
            "required_energy_kwh": selected.required_energy_kwh,
            "soc_final": preview["soc_final"],
        },
        modification,
        preview=preview,
        verified=True,
    )


def _subroute_options(
    request: VoyageRequest, vessel: VesselState, segments: list[Segment],
    candidates: list[EnergyResult], auxiliary_power_kw: float,
) -> list[CompromiseOption]:
    if len(segments) < 2:
        return []
    available = _available_energy(vessel)
    limit = _time_limit(request)
    if available is None:
        return []
    ranked: list[tuple[tuple[float, ...], CompromiseOption]] = []
    for length in range(len(segments) - 1, 0, -1):
        for start in range(0, len(segments) - length + 1):
            subset = segments[start:start + length]
            plans = _candidate_plans(subset, candidates, vessel, auxiliary_power_kw)
            feasible = [
                item for item in plans
                if item.required_energy_kwh <= available + 1e-9
                and (limit is None or item.duration_h <= limit + 1e-9)
            ]
            if not feasible:
                continue
            selected = min(feasible, key=lambda item: (item.required_energy_kwh, item.duration_h))
            distance = sum(item.distance_km for item in subset)
            preview = _preview(selected, request, vessel, segments=subset)
            option = CompromiseOption(
                "shorten_route",
                f"改为 {subset[0].origin} → {subset[-1].destination}（{distance:.3g} km）｜"
                f"预计 {selected.duration_h:.2f} h、{selected.required_energy_kwh:.1f} kWh、"
                f"到达 SOC {preview['soc_final']:.1%}",
                {
                    "distance_km": distance,
                    "duration_h": selected.duration_h,
                    "required_energy_kwh": selected.required_energy_kwh,
                    "soc_final": preview["soc_final"],
                },
                {"origin": subset[0].origin, "destination": subset[-1].destination},
                preview=preview,
                verified=True,
            )
            keeps_endpoint = float(subset[0].origin == request.origin) + float(
                subset[-1].destination == request.destination
            )
            ranked.append(((keeps_endpoint, distance, -selected.required_energy_kwh), option))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in ranked[:3]]


def _combined_options(
    plans: list[_CandidatePlan], request: VoyageRequest, vessel: VesselState,
    segments: list[Segment], existing: list[CompromiseOption],
) -> list[CompromiseOption]:
    if not plans or not vessel.capacity_kwh or vessel.soc_min is None:
        return []
    signatures = {
        tuple(sorted((item.modification or {}).items())) for item in existing if item.modification
    }
    selected_plans = [
        min(plans, key=lambda item: (item.duration_h, item.required_energy_kwh)),
        min(plans, key=lambda item: (item.required_energy_kwh, item.duration_h)),
    ]
    options = []
    for selected in selected_plans:
        modification: dict[str, Any] = {}
        limit = _time_limit(request)
        if limit is not None and selected.duration_h > limit + 1e-9:
            modification["max_duration_h"] = _round_up(selected.duration_h, 3)
        target_soc = _round_up(
            vessel.soc_min + selected.required_energy_kwh / vessel.capacity_kwh, 3,
        )
        if vessel.soc_initial is not None and target_soc > vessel.soc_initial + 1e-9:
            modification["soc_initial"] = target_soc
        if target_soc > 1.0 or len(modification) < 2:
            continue
        signature = tuple(sorted(modification.items()))
        if signature in signatures:
            continue
        signatures.add(signature)
        preview = _preview(selected, request, vessel, soc_initial=target_soc, segments=segments)
        options.append(CompromiseOption(
            "recharge_and_extend",
            f"补能至 SOC {target_soc:.1%}，并将最长航时放宽至 "
            f"{modification['max_duration_h']:.3f} h｜预计能耗 "
            f"{selected.required_energy_kwh:.1f} kWh、到达 SOC {preview['soc_final']:.1%}",
            {
                "required_soc_initial": target_soc,
                "required_duration_h": modification["max_duration_h"],
                "required_energy_kwh": selected.required_energy_kwh,
                "soc_final": preview["soc_final"],
            },
            modification,
            requires_input="仅在补能完成并确认实测 SOC 后执行。",
            preview=preview,
            verified=True,
        ))
    return options


def enumerate_options(
    optimization: OptimizationResult,
    request: VoyageRequest,
    vessel: VesselState,
    segments: list[Segment],
    candidates: list[EnergyResult],
    auxiliary_power_kw: float,
) -> list[CompromiseOption]:
    """Return only concrete alternatives that pass the current finite-grid checks."""
    plans = _candidate_plans(segments, candidates, vessel, auxiliary_power_kw)
    options: list[CompromiseOption] = []
    kind = optimization.infeasible_type or InfeasibleType.COMBINED
    if kind in (InfeasibleType.SOC, InfeasibleType.COMBINED):
        recharge = _recharge_option(plans, request, vessel, segments)
        if recharge is not None:
            options.append(recharge)
    if kind in (InfeasibleType.TIME, InfeasibleType.COMBINED):
        time_option = _time_option(plans, request, vessel, segments)
        if time_option is not None:
            options.append(time_option)
    options.extend(_subroute_options(
        request, vessel, segments, candidates, auxiliary_power_kw,
    ))
    if kind == InfeasibleType.COMBINED:
        options.extend(_combined_options(plans, request, vessel, segments, options))
    options.append(CompromiseOption(
        "give_up", "放弃当前任务，保留不可行结论", {}, None,
    ))
    return options


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
    """Compatibility helper for deterministic single-field choices."""
    if diagnostics is None and segments is not None and candidates is not None and vessel is not None:
        diagnostics = build_boundary_diagnostics(
            segments, candidates, vessel, request, auxiliary_power_kw,
        )
    diagnostics = diagnostics or {}
    modification: dict[str, Any] = {}
    if direction == "accept_late":
        duration = diagnostics.get("minimum_energy_feasible_duration_h")
        if duration is None:
            duration = (request.max_duration_h or 0.0) + diagnostics.get("time_shortfall_h", 0.0)
        if not duration or duration <= (request.max_duration_h or 0.0):
            raise ValueError("accept_late 没有可验证的延长航时值。")
        modification["max_duration_h"] = _round_up(float(duration), 3)
    elif direction == "recharge":
        target = diagnostics.get("required_soc_initial_within_time")
        if target is None or target > 1:
            raise ValueError("recharge 没有可验证的目标 SOC。")
        modification["soc_initial"] = _round_up(float(target), 3)
    elif direction == "give_up":
        return request
    else:
        raise ValueError(f"方向 {direction} 必须使用后端返回的具体 modification。")
    payload = request.to_dict()
    payload.update(modification)
    return VoyageRequest.from_dict(payload)
