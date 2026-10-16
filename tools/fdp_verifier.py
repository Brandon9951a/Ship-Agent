"""Independent replay checks for the draft research FDP result.

This module intentionally does not call the dynamic-programming implementation.
It replays the returned bus powers against the request and checks their SOC
effects, mode dispatch, time alignment, and cost totals from first principles.
"""

from __future__ import annotations

from math import isclose
from typing import Any

from schemas.research_management import BatteryManagementRequest


TOL = 1e-8


def _check(name: str, passed: bool, *, actual: Any = None, limit: Any = None,
           detail: str = "") -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "actual": actual,
            "limit": limit, "unit": None, "reason": detail}


def _numbers_match(actual: Any, expected: float) -> bool:
    try:
        return isclose(float(actual), float(expected), rel_tol=1e-8, abs_tol=1e-6)
    except (TypeError, ValueError):
        return False


def verify_trajectory(
    request: BatteryManagementRequest,
    rows: list[dict[str, Any]],
    optimized_cost_terms: dict[str, float],
    *,
    conservative_grid: bool = True,
) -> dict[str, Any]:
    """Independently check every selected interval and both cost summaries."""
    checks: list[dict[str, Any]] = []
    if len(rows) != len(request.demand_series):
        checks.append(_check("row_count", False, actual=len(rows),
                             limit=len(request.demand_series),
                             detail="输出时序行数与需求时序不一致。"))
        return {"passed": False, "checks": checks}

    batteries = (request.battery_1, request.battery_2)
    current_actual = [batteries[0].initial_soc, batteries[1].initial_soc]
    previous_mode = request.previous_mode
    expected_start = 0.0
    if conservative_grid:
        planning_soc = [
            battery.soc_min
            + int((battery.initial_soc - battery.soc_min + TOL)
                  / request.grid.soc_step) * request.grid.soc_step
            for battery in batteries
        ]
    else:
        planning_soc = [battery.initial_soc for battery in batteries]
    recomputed_grid = {key: 0.0 for key in ("power", "switch", "soc_balance", "low_load_mode3")}
    recomputed_replay = {key: 0.0 for key in recomputed_grid}

    for index, (point, row) in enumerate(zip(request.demand_series, rows)):
        prefix = f"interval[{index}]"
        try:
            time_ok = (
                isclose(float(row["start_offset_s"]), expected_start, abs_tol=TOL)
                and isclose(float(row["start_offset_s"]), float(point.start_offset_s), abs_tol=TOL)
                and isclose(float(row["duration_s"]), float(point.duration_s), abs_tol=TOL)
                and isclose(float(row["demand_bus_kw"]), float(point.demand_bus_kw), abs_tol=TOL)
                and row.get("segment_id") == point.segment_id
                and row.get("phase") == point.phase
            )
        except (KeyError, TypeError, ValueError):
            time_ok = False
        checks.append(_check(f"{prefix}.time_and_input", time_ok,
                             actual=row.get("start_offset_s"), limit=expected_start,
                             detail="区间边界、需求或时长与输入不一致。"))
        expected_start = float(point.start_offset_s) + float(point.duration_s)

        try:
            mode = row.get("mode")
            p1, p2 = float(row["battery_1_power_kw"]), float(row["battery_2_power_kw"])
            demand = float(point.demand_bus_kw)
            duration_h = float(point.duration_s) / 3600.0
            actual_before = [float(row["soc_1_before"]), float(row["soc_2_before"])]
            actual_after = [float(row["soc_1_after"]), float(row["soc_2_after"])]
            planned_before = [float(row["planning_soc_1_before"]),
                              float(row["planning_soc_2_before"])]
            planned_after = [float(row["planning_soc_1_after"]),
                             float(row["planning_soc_2_after"])]
        except (KeyError, TypeError, ValueError):
            checks.append(_check(f"{prefix}.fields", False,
                                 detail="缺少可复算的功率或 SOC 字段。"))
            continue

        balance_ok = (p1 >= -TOL and p2 >= -TOL
                      and isclose(p1 + p2, demand, rel_tol=1e-9, abs_tol=TOL))
        checks.append(_check(f"{prefix}.bus_balance", balance_ok,
                             actual=p1 + p2, limit=demand, detail="P1 + P2 必须等于母线需求。"))

        power_ok = True
        for power, battery in zip((p1, p2), batteries):
            if power > battery.max_power_kw + TOL:
                power_ok = False
            if power > TOL and power < battery.min_power_kw - TOL:
                power_ok = False
        checks.append(_check(f"{prefix}.battery_power_limits", power_ok,
                             actual=[p1, p2], limit=[
                                 [b.min_power_kw, b.max_power_kw] for b in batteries
                             ], detail="单组输出超出已提供的运行功率范围。"))

        if demand <= TOL:
            expected_mode = previous_mode if previous_mode is not None else mode
            mode_ok = p1 <= TOL and p2 <= TOL and mode == expected_mode
        elif mode == 1:
            mode_ok = isclose(p1, demand, rel_tol=1e-9, abs_tol=TOL) and p2 <= TOL
        elif mode == 2:
            mode_ok = isclose(p2, demand, rel_tol=1e-9, abs_tol=TOL) and p1 <= TOL
        elif mode == 3:
            mode_ok = isclose(p1 + p2, demand, rel_tol=1e-9, abs_tol=TOL)
            denominator = actual_before[0] + actual_before[1]
            if denominator <= TOL:
                mode_ok = False
            else:
                mode_ok = mode_ok and isclose(
                    p1 / demand, actual_before[0] / denominator,
                    rel_tol=1e-8, abs_tol=TOL,
                )
        else:
            mode_ok = False
        checks.append(_check(f"{prefix}.mode_dispatch", mode_ok,
                             actual=mode, detail="模式功率分配与任务书规则不一致。"))

        transition_ok = True
        if demand > TOL and previous_mode in (1, 2, 3) and mode != previous_mode:
            transition_ok = (previous_mode, mode) in request.allowed_mode_transitions
        checks.append(_check(f"{prefix}.mode_transition", transition_ok,
                             actual=[previous_mode, mode],
                             detail="模式转换不在请求允许的转换集合中。"))

        soc_transition_ok = True
        planning_continuity_ok = True
        soc_bound_ok = True
        for i, (power, battery) in enumerate(zip((p1, p2), batteries)):
            expected_soc = current_actual[i] - (
                power * duration_h / (battery.discharge_efficiency * battery.capacity_kwh)
            )
            soc_transition_ok = soc_transition_ok and isclose(
                actual_before[i], current_actual[i], rel_tol=1e-8, abs_tol=TOL
            ) and isclose(actual_after[i], expected_soc, rel_tol=1e-8, abs_tol=TOL)
            planning_continuity_ok = planning_continuity_ok and isclose(
                planned_before[i], planning_soc[i], rel_tol=1e-8, abs_tol=TOL
            )
            soc_bound_ok = soc_bound_ok and (
                battery.soc_min - TOL <= actual_after[i] <= 1.0 + TOL
            )
            if conservative_grid:
                planned_expected = battery.soc_min + int(
                    (expected_soc - battery.soc_min + TOL) / request.grid.soc_step
                ) * request.grid.soc_step
                planned_soc_ok = (
                    planned_before[i] <= actual_before[i] + TOL
                    and isclose(planned_after[i], planned_expected,
                                rel_tol=1e-8, abs_tol=TOL)
                    and planned_after[i] <= actual_after[i] + TOL
                    and isclose(
                        (planned_after[i] - battery.soc_min) / request.grid.soc_step,
                        round((planned_after[i] - battery.soc_min) / request.grid.soc_step),
                        rel_tol=0, abs_tol=1e-7,
                    )
                )
            else:
                planned_soc_ok = (
                    isclose(planned_before[i], actual_before[i], rel_tol=1e-8, abs_tol=TOL)
                    and isclose(planned_after[i], expected_soc, rel_tol=1e-8, abs_tol=TOL)
                )
            soc_transition_ok = soc_transition_ok and planned_soc_ok
        checks.append(_check(f"{prefix}.planning_soc_continuity",
                             planning_continuity_ok,
                             actual=planned_before, limit=planning_soc,
                             detail="规划 SOC 必须连续，不能在区间间跳变。"))
        checks.append(_check(f"{prefix}.soc_transition", soc_transition_ok,
                             actual=actual_after, detail="SOC 必须由母线输出、效率、容量和时长复算。"))
        checks.append(_check(f"{prefix}.soc_bounds", soc_bound_ok,
                             actual=actual_after,
                             limit=[b.soc_min for b in batteries],
                             detail="SOC 越过规划下限或 1.0。"))

        for cost_name, target, soc_pair in (
            ("optimized", recomputed_grid, planned_before),
            ("replayed", recomputed_replay, actual_before),
        ):
            component = _costs(
                request, mode, p1, p2, soc_pair[0], soc_pair[1],
                demand, previous_mode,
            )
            for key in target:
                target[key] += component[key]
            if cost_name == "optimized":
                row_cost = row.get("cost_terms") or {}
                row_ok = all(
                    key in row_cost and _numbers_match(row_cost[key], value)
                    for key, value in component.items()
                )
                checks.append(_check(f"{prefix}.cost_terms", row_ok,
                                     actual=row_cost, limit=component,
                                     detail="逐时成本分项与论文结构复算不一致。"))

        expected_energy = {
            "demand_bus_energy_kwh": demand * duration_h,
            "battery_1_bus_energy_kwh": p1 * duration_h,
            "battery_2_bus_energy_kwh": p2 * duration_h,
            "battery_1_energy_drawn_kwh": p1 * duration_h / batteries[0].discharge_efficiency,
            "battery_2_energy_drawn_kwh": p2 * duration_h / batteries[1].discharge_efficiency,
            "battery_1_discharge_loss_kwh": (
                p1 * duration_h / batteries[0].discharge_efficiency - p1 * duration_h
            ),
            "battery_2_discharge_loss_kwh": (
                p2 * duration_h / batteries[1].discharge_efficiency - p2 * duration_h
            ),
            "motor_energy_kwh": (
                float(point.motor_power_kw) * duration_h
                if point.motor_power_kw is not None else None
            ),
            "propulsion_bus_energy_kwh": (
                float(point.propulsion_bus_kw) * duration_h
                if point.propulsion_bus_kw is not None else None
            ),
            "auxiliary_bus_energy_kwh": (
                float(point.auxiliary_bus_kw) * duration_h
                if point.auxiliary_bus_kw is not None else None
            ),
            "conversion_delta_kwh": (
                (float(point.propulsion_bus_kw) - float(point.motor_power_kw)) * duration_h
                if point.propulsion_bus_kw is not None and point.motor_power_kw is not None
                else None
            ),
        }
        energy_ok = True
        for field, expected in expected_energy.items():
            actual = row.get(field)
            if expected is None:
                energy_ok = energy_ok and actual is None
            else:
                try:
                    energy_ok = energy_ok and isclose(
                        float(actual), expected, rel_tol=1e-8, abs_tol=TOL
                    )
                except (TypeError, ValueError):
                    energy_ok = False
        if (point.propulsion_bus_kw is not None and point.auxiliary_bus_kw is not None):
            energy_ok = energy_ok and isclose(
                point.propulsion_bus_kw + point.auxiliary_bus_kw,
                demand, rel_tol=1e-8, abs_tol=TOL
            )
        checks.append(_check(f"{prefix}.energy_conservation", energy_ok,
                             actual={key: row.get(key) for key in expected_energy},
                             limit=expected_energy,
                             detail="母线、电池侧能量及适配器分项必须可按 kW × s / 3600 复算。"))

        current_actual = actual_after
        planning_soc = planned_after
        if demand > TOL:
            previous_mode = mode

    optimized_ok = isinstance(optimized_cost_terms, dict) and all(
        key in optimized_cost_terms
        and _numbers_match(optimized_cost_terms[key], value)
        for key, value in recomputed_grid.items()
    )
    checks.append(_check("cost_terms.optimized", optimized_ok,
                         actual=optimized_cost_terms, limit=recomputed_grid,
                         detail="FDP 成本分项与独立复算结果不一致。"))

    passed = all(item["passed"] for item in checks)
    return {"passed": passed, "checks": checks,
            "recomputed_cost_terms": recomputed_grid,
            "recomputed_replayed_cost_terms": recomputed_replay}


def _costs(request: BatteryManagementRequest, mode: int | None,
           p1: float, p2: float, soc1: float, soc2: float,
           demand: float, previous_mode: int | None) -> dict[str, float]:
    weights = request.weights
    switch = 0.0
    if (mode in (1, 2, 3) and previous_mode in (1, 2, 3)
            and mode != previous_mode
            and not (mode in (1, 2) and previous_mode in (1, 2))):
        switch = weights.k_s
    delta = abs(soc1 - soc2)
    return {
        "power": weights.k_p * (p1 * p1 + p2 * p2),
        "switch": switch,
        "soc_balance": (weights.k_soc * delta if mode in (1, 2)
                        else (2.0 * weights.k_soc * delta if mode == 3 else 0.0)),
        "low_load_mode3": (weights.k_low if mode == 3 and demand > TOL
                           and demand / request.load_rate_reference_kw
                           <= weights.low_load_threshold else 0.0),
    }

