"""Reproducible, explicitly assumed inputs for Tmanagement v2 acceptance.

These cases exercise an algorithmic research model only. Values are fixtures,
not vessel facts or approved battery operating limits.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from schemas.research_management import (
    SCHEMA_VERSION,
    BatteryManagementRequest,
    BatteryParameters,
    CostWeights,
    FDPGridConfig,
    PowerDemandPoint,
)
from schemas.types import SourceRef
from tools.tmanagement_fdp import DEFAULT_TRANSITIONS


def _assumption_source(source_id: str, note: str = "named acceptance fixture") -> SourceRef:
    return SourceRef(source_id, "assumption", confirmed=False, note=note)


def _battery(battery_id: str, *, capacity_kwh: float = 1000,
             initial_soc: float = 0.8, soc_min: float = 0.2,
             min_power_kw: float = 5, max_power_kw: float = 50,
             efficiency: float = 0.9) -> BatteryParameters:
    values = {
        "capacity_kwh": capacity_kwh,
        "initial_soc": initial_soc,
        "soc_min": soc_min,
        "min_power_kw": min_power_kw,
        "max_power_kw": max_power_kw,
        "discharge_efficiency": efficiency,
    }
    return BatteryParameters(
        battery_id=battery_id,
        **values,
        sources={field: _assumption_source(f"fixture:{battery_id}:{field}")
                 for field in values},
    )


def _point(index: int, demand_kw: float, duration_s: float = 600,
           phase: str = "sailing") -> PowerDemandPoint:
    propulsion = demand_kw - (5 if demand_kw > 0 else 0)
    auxiliary = demand_kw - propulsion
    return PowerDemandPoint(
        segment_id=f"segment-{index // 2 + 1}",
        phase=phase,
        start_offset_s=sum(600 if i != 1 else 3600 for i in range(index)),
        duration_s=duration_s,
        demand_bus_kw=demand_kw,
        demand_source=_assumption_source(f"fixture:demand:{index}"),
        conversion_model_ref="named-fixture-conversion-v1",
        conversion_source=_assumption_source("fixture:conversion"),
        motor_power_kw=max(propulsion - 1, 0),
        propulsion_bus_kw=propulsion,
        auxiliary_bus_kw=auxiliary,
        component_source=_assumption_source("fixture:shared-adapter-components"),
    )


def build_request(case_id: str = "success") -> BatteryManagementRequest:
    """Build one of the static-power, SOC, grid, charging, or success cases."""
    demands = [30.0, 80.0, 0.0, 30.0]
    durations = [600.0, 3600.0, 600.0, 600.0]
    phases = ["sailing", "sailing", "waiting", "sailing"]
    battery_1 = _battery("battery_1")
    battery_2 = _battery("battery_2")
    grid = FDPGridConfig(soc_step=0.05, max_states_per_layer=1000,
                         max_transitions=100_000)
    weights = CostWeights(k_p=1e-4, k_s=1e3, k_soc=0.3, k_low=1e5,
                          low_load_threshold=0.65)

    if case_id == "instantaneous_overload":
        demands, durations, phases = [120.0], [60.0], ["sailing"]
    elif case_id == "soc_depletion":
        battery_1 = _battery("battery_1", capacity_kwh=10, initial_soc=0.3,
                             soc_min=0.2, max_power_kw=100, min_power_kw=5)
        battery_2 = _battery("battery_2", capacity_kwh=10, initial_soc=0.3,
                             soc_min=0.2, max_power_kw=100, min_power_kw=5)
        demands, durations, phases = [100.0], [3600.0], ["sailing"]
    elif case_id == "grid_budget":
        demands, durations, phases = [30.0, 35.0], [600.0, 600.0], ["sailing", "sailing"]
        grid = replace(grid, max_transitions=1)
    elif case_id == "charging_unsupported":
        demands, durations, phases = [-10.0], [300.0], ["docking"]
    elif case_id == "exhausted_single_battery":
        demands, durations, phases = [60.0], [3600.0], ["sailing"]
        battery_1 = _battery("battery_1", capacity_kwh=10, initial_soc=0.3,
                             soc_min=0.2, max_power_kw=100)
        battery_2 = _battery("battery_2", capacity_kwh=1000, initial_soc=0.8,
                             soc_min=0.2, max_power_kw=50)
    points = []
    start = 0.0
    for index, (demand, duration, phase) in enumerate(zip(demands, durations, phases)):
        point = _point(index, demand, duration, phase)
        point = replace(point, start_offset_s=start)
        points.append(point)
        start += duration

    return BatteryManagementRequest(
        schema_version=SCHEMA_VERSION,
        task_id=f"task:{case_id}",
        request_revision=1,
        snapshot_id=f"snapshot:{case_id}",
        scenario_id=f"assumption:{case_id}",
        call_id=f"call:{case_id}",
        parent_call_id="call:acceptance-harness",
        selected_candidate_id=f"candidate:{case_id}",
        selected_demand_ref=f"demand:{case_id}",
        demand_series=points,
        battery_1=battery_1,
        battery_2=battery_2,
        load_rate_reference_kw=100.0,
        load_rate_reference_source=_assumption_source("fixture:load-rate-reference"),
        allowed_mode_transitions=DEFAULT_TRANSITIONS,
        allowed_mode_transitions_source=_assumption_source("fixture:mode-transitions"),
        previous_mode=None,
        planning_origin_at=None,
        grid=grid,
        weights=weights,
        assumptions=[
            {"assumption_id": "acceptance-battery-parameters",
             "value": "容量、SOC、功率界限和效率为测试假设，不是实船参数。"},
            {"assumption_id": "acceptance-bus-demand",
             "value": "需求、转换和辅助功率仅用于工具验收夹具。"},
        ],
        source_mode="simulation",
    )


def build_full_voyage_request() -> BatteryManagementRequest:
    """A named, assumed 24-hour run with sailing and waiting intervals."""
    base = build_request("success")
    points = []
    for index in range(48):
        phase = "waiting" if index % 3 == 2 else "sailing"
        demand = 5.0 if phase == "waiting" else 70.0
        propulsion = demand - 5.0 if phase == "sailing" else 0.0
        point = _point(index, demand, duration_s=1800, phase=phase)
        points.append(replace(
            point,
            segment_id=f"leg-{index // 3 + 1:02d}",
            start_offset_s=index * 1800.0,
            duration_s=1800.0,
            motor_power_kw=max(propulsion - 1.0, 0.0),
            propulsion_bus_kw=propulsion,
            auxiliary_bus_kw=5.0,
        ))
    assumptions = [
        *base.assumptions,
        {"assumption_id": "fdp-24h-battery-fixture",
         "value": "两组 1500 kWh、初始 SOC 0.9、效率 0.9、每组 50 kW 上限均为具名仿真假设。"},
        {"assumption_id": "fdp-24h-demand-fixture",
         "value": "48 个 30 分钟区间；32 个 70 kW 航行区间、16 个 5 kW 等待区间。"},
    ]
    return replace(
        base,
        task_id="task:fdp-full-voyage-24h",
        snapshot_id="snapshot:fdp-full-voyage-24h",
        scenario_id="assumption:fdp-full-voyage-24h",
        call_id="call:fdp-full-voyage-24h",
        selected_candidate_id="candidate:fdp-full-voyage-24h",
        selected_demand_ref="demand:fdp-full-voyage-24h",
        demand_series=points,
        battery_1=_battery("battery_1", capacity_kwh=1500, initial_soc=0.9,
                           soc_min=0.2, min_power_kw=5, max_power_kw=50),
        battery_2=_battery("battery_2", capacity_kwh=1500, initial_soc=0.9,
                           soc_min=0.2, min_power_kw=5, max_power_kw=50),
        # This explicit fine grid avoids excessive conservative rounding over
        # the full horizon; report it with the measured resource results.
        grid=replace(base.grid, soc_step=0.005, max_states_per_layer=5000,
                     max_transitions=1_000_000),
        assumptions=assumptions,
    )


def invalid_missing_efficiency_request() -> dict[str, Any]:
    """A JSON-shaped example showing missing facts stay missing, not defaults."""
    request = build_request("success").to_dict()
    request["battery_1"]["discharge_efficiency"] = None
    request["battery_1"]["sources"].pop("discharge_efficiency")
    return request

