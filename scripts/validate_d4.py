"""Run the three D4 acceptance scenarios with explicit demo-only boundaries.

The runner uses checked route distances and A-approved software-demo values. It
does not turn the unknown route speed limits or the uncalibrated cubic model into
real-vessel facts. The same route, speed grid and hard constraints are retained
across scenarios; only the user time limit or initial SOC changes.
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import replace
from itertools import product
from pathlib import Path
from typing import Any

from schemas.types import (
    DataContext,
    EnergyResult,
    OptimizationResult,
    Segment,
    SourceRef,
    Status,
    VoyageRequest,
)
from tools.tdata import load_config, tdata
from tools.tenergy import EnergyModel, run_tenergy
from tools.tmanagement import run_tmanagement
from tools.tseg import segment
from tools.tspeed import run_tspeed


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "configs/examples/d4_scenarios.json"


def load_scenarios(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("mode") != "software_demo_only":
        raise ValueError("D4场景必须明确标记software_demo_only。")
    if payload.get("real_ship_validation") is not False:
        raise ValueError("D4场景不得标记为实船验证。")
    if not isinstance(payload.get("scenarios"), list) or not payload["scenarios"]:
        raise ValueError("D4场景配置至少需要一个场景。")
    return payload


def load_runtime_configs() -> dict[str, dict[str, Any]]:
    return {
        "route_config": load_config(ROOT / "configs/route_facts.yaml"),
        "aliases_config": load_config(ROOT / "configs/aliases.yaml"),
        "vessel_config": load_config(ROOT / "configs/vessel_facts.yaml"),
        "limits_config": load_config(ROOT / "configs/limits.yaml"),
        "demo_policy_config": load_config(ROOT / "configs/demo_policy.yaml"),
    }


def _prepare_inputs(
    definition: dict[str, Any], route: dict[str, Any], configs: dict[str, dict[str, Any]]
) -> tuple[VoyageRequest, DataContext, list[Segment], dict[str, Any]]:
    request = VoyageRequest(
        origin=route["origin"],
        destination=route["destination"],
        max_duration_h=float(definition["max_duration_h"]),
        soc_initial=float(definition["soc_initial"]),
        load_state=route["load_state"],
    )
    data_response = tdata(request, **configs)
    if not data_response.payload:
        raise ValueError("Tdata未返回可供D4显式补充假设的部分结果。")
    raw_data = DataContext.from_dict(data_response.payload)
    segment_response = segment(
        request,
        raw_data,
        route_config=configs["route_config"],
        aliases_config=configs["aliases_config"],
        demo_policy_config=configs["demo_policy_config"],
    )
    if not segment_response.payload:
        raise ValueError("Tseg未返回可供D4显式补充假设的航段。")

    policy = configs["demo_policy_config"]
    speed_ceiling = float(policy["power_and_energy"]["economic_speed_kmh"])
    speed_note = (
        f"D4软件演示搜索上界采用A批准的经济航速{speed_ceiling:g}km/h；"
        "真实逐段限速仍未知，本值不是通航限速。"
    )
    segments = [
        replace(
            Segment.from_dict(item),
            max_speed_kmh=speed_ceiling,
            assumptions=[*item.get("assumptions", []), speed_note],
        )
        for item in segment_response.payload["segments"]
    ]

    effective_capacity = float(policy["battery"]["total_effective_capacity_kwh"])
    capacity_source = SourceRef(
        "configs/demo_policy.yaml",
        "assumption",
        "battery.total_effective_capacity_kwh",
        False,
        "A批准的软件演示有效容量；由标称容量乘初始SOH得到，不是实测可用容量。",
    )
    vessel_sources = dict(raw_data.vessel.sources)
    vessel_sources["capacity_kwh"] = capacity_source
    vessel = replace(
        raw_data.vessel,
        capacity_kwh=effective_capacity,
        sources=vessel_sources,
        assumptions=[
            *raw_data.vessel.assumptions,
            "能量预算使用A批准的1411.065kWh演示有效容量，不重复使用1567.85kWh标称容量。",
        ],
    )
    data = DataContext(
        vessel=vessel,
        route_id=raw_data.route_id,
        parameters=raw_data.parameters,
        conflicts=[],
        missing_fields=[],
        assumptions=[*raw_data.assumptions, speed_note],
    )
    upstream = {
        "tdata_status_before_demo_resolution": data_response.status.value,
        "tdata_missing_fields": data_response.missing_fields,
        "tseg_status_before_demo_resolution": segment_response.status.value,
        "tseg_missing_fields": segment_response.missing_fields,
        "demo_resolution": {
            "effective_capacity_kwh": effective_capacity,
            "speed_ceiling_kmh": speed_ceiling,
            "speed_ceiling_role": "synthetic_demo_operating_cap_not_legal_waterway_limit",
            "speed_ceiling_source": "configs/demo_policy.yaml#power_and_energy.economic_speed_kmh",
            "waiting_h": "A批准的河南境内演示排队等待规则；不含船闸内部通行时间",
        },
    }
    return request, data, segments, upstream


def evaluate_scenario(
    definition: dict[str, Any],
    route: dict[str, Any],
    configs: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Evaluate one scenario without asserting a predeclared outcome.

    D4 wraps this function with expected-result checks.  D5 reuses the same
    calculation path and checks it against an independent finite-grid
    reference instead of predicting outcomes in configuration.
    """
    configs = configs or load_runtime_configs()
    request, data, segments, upstream = _prepare_inputs(definition, route, configs)
    policy = configs["demo_policy_config"]
    economic_speed = float(policy["power_and_energy"]["economic_speed_kmh"])
    economic_propulsion_kw = float(
        policy["power_and_energy"]["economic_propulsion_power_kw"]
    )
    coefficient = economic_propulsion_kw / economic_speed ** 3
    auxiliary_kw = float(policy["power_and_energy"]["demo_auxiliary_power_kw"])
    model = EnergyModel(
        model_id="d4-demo-cubic-from-approved-economic-point-v1",
        coefficient_kw_per_kmh3=coefficient,
        auxiliary_power_kw=auxiliary_kw,
        energy_scope="propulsion",
        usage="synthetic_demo",
    )
    energy = run_tenergy(segments, list(route["candidate_speeds_kmh"]), model)
    if energy.status != Status.OK:
        raise ValueError(f"D4场景{definition['id']}在Tenergy停止：{energy.to_dict()}")
    candidates = [
        EnergyResult.from_dict(item) for item in energy.payload["candidate_results"]
    ]
    by_segment = {
        item.segment_id: [candidate for candidate in candidates
                          if candidate.segment_id == item.segment_id]
        for item in segments
    }
    combinations = list(product(*(by_segment[item.segment_id] for item in segments)))
    boundary_rows = []
    for choices in combinations:
        duration_h = sum(item.duration_h for item in choices)
        propulsion_kwh = sum(item.energy_kwh for item in choices)
        required_kwh = propulsion_kwh + auxiliary_kw * duration_h
        boundary_rows.append((duration_h, required_kwh))
    fastest_duration_h = min(item[0] for item in boundary_rows)
    within_time = [
        item for item in boundary_rows
        if item[0] <= request.max_duration_h + 1e-9
    ]
    minimum_required_kwh = min((item[1] for item in within_time), default=None)
    available_kwh = data.vessel.capacity_kwh * (
        data.vessel.soc_initial - data.vessel.soc_min
    )
    boundary_diagnostics = {
        "candidate_combinations": len(boundary_rows),
        "fastest_duration_h": fastest_duration_h,
        "time_shortfall_h": max(fastest_duration_h - request.max_duration_h, 0.0),
        "available_energy_above_soc_min_kwh": available_kwh,
        "minimum_required_energy_within_time_kwh": minimum_required_kwh,
        "minimum_charge_required_within_time_kwh": (
            max(minimum_required_kwh - available_kwh, 0.0)
            if minimum_required_kwh is not None else None
        ),
        "diagnostic_only": True,
        "note": "边界诊断来自同一Tenergy候选网格，不代表已选择或执行补能方案。",
    }
    speed = run_tspeed(request, segments, candidates, data.vessel)
    management = None
    final_status = speed.status
    infeasible_type = speed.infeasible_type
    if speed.status == Status.OK:
        optimization = OptimizationResult.from_dict(speed.payload)
        management = run_tmanagement(request, data, optimization)
        final_status = management.status
        infeasible_type = management.infeasible_type

    actual_type = infeasible_type.value if infeasible_type is not None else None
    result = {
        "id": definition["id"],
        "status": final_status.value,
        "infeasible_type": actual_type,
        "real_ship_validation": False,
        "request": request.to_dict(),
        "route_distance_km": sum(item.distance_km for item in segments),
        "segments": [item.to_dict() for item in segments],
        "upstream": upstream,
        "model": {
            "model_id": model.model_id,
            "coefficient_kw_per_kmh3": coefficient,
            "energy_scope": "propulsion",
            "auxiliary_power_kw": auxiliary_kw,
            "calibration": "derived_demo_point_not_real_voyage_fit",
        },
        "boundary_diagnostics": boundary_diagnostics,
        "tenergy": energy.to_dict(),
        "tspeed": speed.to_dict(),
        "tmanagement": management.to_dict() if management is not None else None,
    }
    return result


def run_scenario(
    definition: dict[str, Any], route: dict[str, Any], configs: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    result = evaluate_scenario(definition, route, configs)
    expected_status = definition["expected_status"]
    expected_type = definition.get("expected_infeasible_type")
    expected_match = (
        result["status"] == expected_status
        and result["infeasible_type"] == expected_type
    )
    result.update({
        "expected_status": expected_status,
        "expected_infeasible_type": expected_type,
        "expected_match": expected_match,
    })
    if not expected_match:
        raise AssertionError(
            f"D4场景{definition['id']}预期{expected_status}/{expected_type}，"
            f"实际{result['status']}/{result['infeasible_type']}。"
        )
    return result


def run_suite(config_path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    definition = load_scenarios(config_path)
    configs = load_runtime_configs()
    results = [
        run_scenario(item, definition["route"], configs)
        for item in definition["scenarios"]
    ]
    return {
        "status": "passed" if all(item["expected_match"] for item in results) else "failed",
        "mode": definition["mode"],
        "real_ship_validation": False,
        "units": {
            "distance": "km",
            "speed": "km/h",
            "power": "kW",
            "energy": "kWh",
            "duration": "h",
            "soc": "fraction",
        },
        "scenario_count": len(results),
        "scenarios": results,
        "limitations": [
            "逐段真实限速、船闸内部通行时间和充电功率仍未知。",
            "三次方系数由A批准的演示经济工况点推导，未用独立实船航次标定。",
            "结果用于软件边界验收，不是豫交投001真实航行建议。",
        ],
    }


def _summary_row(item: dict[str, Any]) -> dict[str, Any]:
    optimization = (item["tspeed"].get("payload") or {})
    management = ((item.get("tmanagement") or {}).get("payload") or {})
    return {
        "scenario": item["id"],
        "status": item["status"],
        "infeasible_type": item["infeasible_type"] or "",
        "expected_match": str(item["expected_match"]).lower(),
        "route_distance_km": item["route_distance_km"],
        "total_duration_h": optimization.get("total_duration_h", ""),
        "propulsion_energy_kwh": optimization.get("total_energy_kwh", ""),
        "required_energy_kwh": management.get("required_energy_kwh", ""),
        "soc_final": management.get("soc_final", ""),
        "real_ship_validation": "false",
    }


def write_report(report: dict[str, Any], output_dir: Path) -> None:
    if output_dir.exists():
        raise FileExistsError(f"输出目录已存在，拒绝覆盖：{output_dir}")
    output_dir.mkdir(parents=True)
    (output_dir / "results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    rows = [_summary_row(item) for item in report["scenarios"]]
    with (output_dir / "summary.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="运行D4三场景软件级验收")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    report = run_suite(args.config)
    if args.output_dir is not None:
        write_report(report, args.output_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
