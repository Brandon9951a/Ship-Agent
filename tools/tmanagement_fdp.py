"""Research-system Tmanagement: three-mode finite-grid forward dynamic programming.

The legacy ``tools.tmanagement`` module remains the v1 demonstration budget
calculator.  This module implements the task-book's v2 research contract with
two independent batteries, a shared bus-demand time series, explicit sources,
and an independent replay check before a result can be marked safe.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from math import isclose, isfinite
from typing import Any, Iterable

from schemas.research_management import (
    SCHEMA_VERSION,
    BatteryManagementRequest,
    BatteryParameters,
    CostWeights,
    FDPGridConfig,
    PowerDemandPoint,
)
from schemas.types import SourceRef
from tools.fdp_verifier import verify_trajectory


MODES = (1, 2, 3)
DEFAULT_TRANSITIONS = tuple((left, right) for left in MODES for right in MODES)
EPS = 1e-9


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_safe(value: Any) -> Any:
    """Keep invalid non-finite inputs hashable and diagnostics JSON-safe."""
    if isinstance(value, float) and not isfinite(value):
        label = "nan" if value != value else (
            "positive_infinity" if value > 0 else "negative_infinity"
        )
        return {"$non_finite": label}
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if value is None or type(value) in (str, int, float, bool):
        return value
    return repr(value)


def _canonical(value: Any) -> bytes:
    return json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _finite(value: Any) -> bool:
    return type(value) in (int, float) and isfinite(float(value))


def _source_dict(source: SourceRef | None) -> dict[str, Any] | None:
    return source.to_dict() if isinstance(source, SourceRef) else None


def _diagnostic(stage: str, code: str, interval: str | None = None,
                field_ref: str | None = None, observed: Any = None,
                limit: Any = None, unit: str | None = None,
                source_refs: list[Any] | None = None, retryable: bool = False,
                detail: str = "") -> dict[str, Any]:
    return {
        "stage": stage,
        "code": code,
        "time_interval_or_segment_id": interval,
        "field_ref": field_ref,
        "observed": _json_safe(observed),
        "limit": _json_safe(limit),
        "unit": unit,
        "source_refs": source_refs or [],
        "retryable": retryable,
        "detail": detail,
    }


def _envelope(request: BatteryManagementRequest, *, started_at: str,
              finished_at: str, execution_status: str, outcome_code: str,
              diagnostics: list[dict[str, Any]], payload: dict[str, Any] | None,
              payload_ref: Any = None, runtime_ms: float | None = None) -> dict[str, Any]:
    config = {
        "algorithm_version": request.grid.algorithm_version,
        "grid": request.grid.to_dict(),
        "weights": request.weights.to_dict(),
        "allowed_mode_transitions": request.allowed_mode_transitions,
    }
    result: dict[str, Any] = {
        "schema_version": request.schema_version or SCHEMA_VERSION,
        "tool_name": "Tmanagement",
        "call_id": request.call_id,
        "parent_call_id": request.parent_call_id,
        "input_hash": _sha(request.to_dict()),
        "request_revision": request.request_revision,
        "snapshot_id": request.snapshot_id,
        "scenario_id": request.scenario_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "execution_status": execution_status,
        "outcome_code": outcome_code,
        "payload_ref": payload_ref,
        "diagnostics": diagnostics,
        "model_or_algorithm_version": request.grid.algorithm_version,
        "config_hash": _sha(config),
    }
    if payload is not None:
        result["payload"] = payload
    if runtime_ms is not None:
        result["runtime_ms"] = round(runtime_ms, 6)
    return result


def validate_request(request: BatteryManagementRequest) -> list[dict[str, Any]]:
    """Return v2 diagnostics without filling unknown values."""
    errors: list[dict[str, Any]] = []

    def missing(field: str, detail: str) -> None:
        errors.append(_diagnostic("input", "missing_input", field_ref=field,
                                  retryable=True, detail=detail))

    def invalid(field: str, observed: Any, detail: str, limit: Any = None) -> None:
        errors.append(_diagnostic("input", "invalid_input", field_ref=field,
                                  observed=observed, limit=limit,
                                  retryable=False, detail=detail))

    for name in ("task_id", "snapshot_id", "scenario_id", "call_id",
                 "selected_candidate_id", "selected_demand_ref"):
        value = getattr(request, name, None)
        if not isinstance(value, str) or not value.strip():
            missing(name, "v2 状态关联字段不能为空。")
    if request.schema_version != SCHEMA_VERSION:
        invalid("schema_version", request.schema_version,
                "当前实现只接受 research-tools/2.0-draft。", SCHEMA_VERSION)
    if type(request.request_revision) is not int or request.request_revision < 1:
        invalid("request_revision", request.request_revision, "必须是正整数。")
    if request.source_mode not in ("simulation", "replay", "measured"):
        invalid("source_mode", request.source_mode, "只允许 simulation/replay/measured。")
    if request.source_mode == "simulation":
        if not request.assumptions:
            missing("assumptions", "仿真场景必须提供具名研究假设。")
        for index, assumption in enumerate(request.assumptions):
            if (not isinstance(assumption, dict)
                    or not str(assumption.get("assumption_id", "")).strip()
                    or "value" not in assumption):
                invalid(f"assumptions[{index}]", assumption,
                        "每条假设需含 assumption_id 和 value。")
    if request.planning_origin_at is not None:
        try:
            parsed = datetime.fromisoformat(request.planning_origin_at.replace("Z", "+00:00"))
            if parsed.utcoffset() is None:
                raise ValueError("timezone required")
        except (AttributeError, TypeError, ValueError):
            invalid("planning_origin_at", request.planning_origin_at,
                    "如提供，必须是带时区 ISO 8601。")
    if not request.demand_series:
        missing("demand_series", "必须提供逐时母线需求，不能传分段平均能耗。")
    expected_start = 0.0
    for index, point in enumerate(request.demand_series):
        path = f"demand_series[{index}]"
        if not isinstance(point.segment_id, str) or not point.segment_id.strip():
            missing(path + ".segment_id", "航段 ID 不能为空。")
        if point.phase not in ("sailing", "waiting", "docking"):
            invalid(path + ".phase", point.phase, "阶段枚举无效。")
        for field in ("start_offset_s", "duration_s", "demand_bus_kw"):
            value = getattr(point, field)
            if value is None:
                missing(path + "." + field, "需求时序关键数值未知，不能按零处理。")
            elif not _finite(value):
                invalid(path + "." + field, value, "必须是有限数值。")
        if _finite(point.start_offset_s) and not isclose(
                float(point.start_offset_s), expected_start, rel_tol=0, abs_tol=EPS):
            invalid(path + ".start_offset_s", point.start_offset_s,
                    "时间轴必须连续、从 0 开始且不重叠。", expected_start)
        if _finite(point.duration_s) and point.duration_s <= 0:
            invalid(path + ".duration_s", point.duration_s, "必须大于 0 秒。")
        if _finite(point.demand_bus_kw) and point.demand_bus_kw < -EPS:
            errors.append(_diagnostic(
                "input", "charging_not_supported", point.segment_id,
                path + ".demand_bus_kw", point.demand_bus_kw, 0, "kW",
                retryable=False,
                detail="首版三模式 FDP 只支持放电供能；负母线需求不能冒充补能状态转移。",
            ))
        if point.demand_source is None:
            missing(path + ".demand_source", "母线需求必须有共享适配器来源。")
        elif not isinstance(point.demand_source.source_id, str) or not point.demand_source.source_id.strip():
            missing(path + ".demand_source.source_id", "source_id 不能为空。")
        if (not isinstance(point.conversion_model_ref, str)
                or not point.conversion_model_ref.strip()):
            missing(path + ".conversion_model_ref", "电机侧到母线侧的转换依据不能省略。")
        if point.conversion_source is None:
            missing(path + ".conversion_source", "转换模型必须有来源。")
        elif not isinstance(point.conversion_source.source_id, str) or not point.conversion_source.source_id.strip():
            missing(path + ".conversion_source.source_id", "source_id 不能为空。")
        components = (point.motor_power_kw, point.propulsion_bus_kw,
                      point.auxiliary_bus_kw)
        for name, value in zip(("motor_power_kw", "propulsion_bus_kw", "auxiliary_bus_kw"),
                               components):
            if value is None:
                missing(path + "." + name, "共享需求工件必须保留电机、推进母线和辅助功率分项。")
            elif not _finite(value):
                invalid(path + "." + name, value, "必须是有限数值。")
            elif (value < -EPS and _finite(point.demand_bus_kw)
                  and point.demand_bus_kw >= -EPS):
                invalid(path + "." + name, value, "功率分项不能为负；回馈/充电尚未实现。")
        if (point.component_source is None
                or not isinstance(point.component_source.source_id, str)
                or not point.component_source.source_id.strip()):
            missing(path + ".component_source", "需求分项必须保留共享适配器来源。")
        if (point.propulsion_bus_kw is not None and point.auxiliary_bus_kw is not None
                and _finite(point.demand_bus_kw)
                and not isclose(point.propulsion_bus_kw + point.auxiliary_bus_kw,
                                point.demand_bus_kw, rel_tol=1e-8, abs_tol=EPS)):
            invalid(path + ".demand_bus_kw", point.demand_bus_kw,
                    "母线需求应等于适配器提供的推进母线功率与辅助功率之和。",
                    point.propulsion_bus_kw + point.auxiliary_bus_kw)
        if _finite(point.start_offset_s) and _finite(point.duration_s):
            expected_start = float(point.start_offset_s) + float(point.duration_s)

    for label, battery in (("battery_1", request.battery_1),
                           ("battery_2", request.battery_2)):
        _validate_battery(label, battery, missing, invalid)
    if not _finite(request.load_rate_reference_kw) or request.load_rate_reference_kw <= 0:
        invalid("load_rate_reference_kw", request.load_rate_reference_kw,
                "负荷率参考功率必须大于 0 kW。")
    if request.load_rate_reference_source is None:
        missing("load_rate_reference_source", "负荷率参考功率必须有来源。")
    elif not request.load_rate_reference_source.source_id.strip():
        missing("load_rate_reference_source.source_id", "source_id 不能为空。")
    if not request.allowed_mode_transitions:
        missing("allowed_mode_transitions", "必须显式给出模式转换规则。")
    else:
        for pair in request.allowed_mode_transitions:
            if (not isinstance(pair, tuple) or len(pair) != 2
                    or pair[0] not in MODES or pair[1] not in MODES):
                invalid("allowed_mode_transitions", pair, "模式转换只能引用 1、2、3。")
        if request.previous_mode not in (None, *MODES):
            invalid("previous_mode", request.previous_mode, "上一模式只能是 1、2、3 或 null。")
        for mode in MODES:
            if (mode, mode) not in request.allowed_mode_transitions:
                invalid("allowed_mode_transitions", request.allowed_mode_transitions,
                        f"必须包含模式 {mode} 的保持转换。")
    if request.allowed_mode_transitions_source is None:
        missing("allowed_mode_transitions_source", "模式转换规则必须有来源。")
    elif not request.allowed_mode_transitions_source.source_id.strip():
        missing("allowed_mode_transitions_source.source_id", "source_id 不能为空。")
    if request.source_mode in ("replay", "measured"):
        for label, battery in (("battery_1", request.battery_1),
                               ("battery_2", request.battery_2)):
            for field, source in battery.sources.items():
                if not source.confirmed:
                    missing(f"{label}.sources.{field}.confirmed",
                            "回放/实测输入要求已确认来源。")
        for index, point in enumerate(request.demand_series):
            for field, source in (("demand_source", point.demand_source),
                                  ("conversion_source", point.conversion_source),
                                  ("component_source", point.component_source)):
                if source is not None and not source.confirmed:
                    missing(f"demand_series[{index}].{field}.confirmed",
                            "回放/实测输入要求已确认来源。")
    for field, source in (("load_rate_reference_source", request.load_rate_reference_source),
                          ("allowed_mode_transitions_source", request.allowed_mode_transitions_source),
                          ("weights.source", request.weights.source)):
        if (request.source_mode in ("replay", "measured") and source is not None
                and not source.confirmed):
            missing(field + ".confirmed", "回放/实测输入要求已确认来源。")
    _validate_grid(request.grid, invalid)
    _validate_weights(request.weights, invalid)
    if not isinstance(request.assumptions, list):
        invalid("assumptions", request.assumptions, "必须是研究假设对象数组。")
    return errors


def _validate_battery(label: str, battery: BatteryParameters, missing: Any,
                      invalid: Any) -> None:
    if not isinstance(battery.battery_id, str) or not battery.battery_id.strip():
        missing(label + ".battery_id", "必须标识 battery_1 或 battery_2。")
    if battery.battery_id != label:
        invalid(label + ".battery_id", battery.battery_id, "电池编号必须与字段一致。", label)
    values = {
        "capacity_kwh": battery.capacity_kwh,
        "initial_soc": battery.initial_soc,
        "soc_min": battery.soc_min,
        "min_power_kw": battery.min_power_kw,
        "max_power_kw": battery.max_power_kw,
        "discharge_efficiency": battery.discharge_efficiency,
    }
    for field, value in values.items():
        if value is None:
            missing(f"{label}.{field}", "电池关键参数未知，不能填默认值。")
        elif not _finite(value):
            invalid(f"{label}.{field}", value, "必须是有限数值。")
    if _finite(battery.capacity_kwh) and battery.capacity_kwh <= 0:
        invalid(f"{label}.capacity_kwh", battery.capacity_kwh, "容量必须大于 0 kWh。")
    for field in ("initial_soc", "soc_min"):
        value = getattr(battery, field)
        if _finite(value) and not 0 <= value <= 1:
            invalid(f"{label}.{field}", value, "SOC 必须在 0–1。", [0, 1])
    if (_finite(battery.initial_soc) and _finite(battery.soc_min)
            and battery.initial_soc < battery.soc_min - EPS):
        invalid(f"{label}.initial_soc", battery.initial_soc,
                "初始 SOC 不能低于规划下限。", battery.soc_min)
    if _finite(battery.min_power_kw) and battery.min_power_kw < 0:
        invalid(f"{label}.min_power_kw", battery.min_power_kw, "不能为负。")
    if _finite(battery.max_power_kw) and battery.max_power_kw <= 0:
        invalid(f"{label}.max_power_kw", battery.max_power_kw, "必须大于 0 kW。")
    if (_finite(battery.min_power_kw) and _finite(battery.max_power_kw)
            and battery.min_power_kw > battery.max_power_kw):
        invalid(f"{label}.min_power_kw", battery.min_power_kw,
                "最小输出不能超过最大输出。", battery.max_power_kw)
    if (_finite(battery.discharge_efficiency)
            and not 0 < battery.discharge_efficiency <= 1):
        invalid(f"{label}.discharge_efficiency", battery.discharge_efficiency,
                "放电效率必须在 (0, 1]。", [0, 1])
    for field in values:
        source = battery.sources.get(field)
        if not isinstance(source, SourceRef) or not source.source_id.strip():
            missing(f"{label}.sources.{field}", "采用值必须带 source_ref。")


def _validate_grid(grid: FDPGridConfig, invalid: Any) -> None:
    if not _finite(grid.soc_step) or not 0 < grid.soc_step <= 1:
        invalid("grid.soc_step", grid.soc_step, "必须在 (0, 1]。")
    for field in ("max_states_per_layer", "max_transitions"):
        value = getattr(grid, field)
        if type(value) is not int or value <= 0:
            invalid("grid." + field, value, "必须是正整数。")
    if not isinstance(grid.algorithm_version, str) or not grid.algorithm_version.strip():
        invalid("grid.algorithm_version", grid.algorithm_version, "算法版本不能为空。")


def _validate_weights(weights: CostWeights, invalid: Any) -> None:
    for field in ("k_p", "k_s", "k_soc", "k_low"):
        value = getattr(weights, field)
        if not _finite(value) or value < 0:
            invalid("weights." + field, value, "权重必须是非负有限数值。")
    if not _finite(weights.low_load_threshold) or not 0 <= weights.low_load_threshold <= 1:
        invalid("weights.low_load_threshold", weights.low_load_threshold,
                "低负荷阈值必须在 0–1。")
    if not isinstance(weights.source, SourceRef) or not weights.source.source_id.strip():
        invalid("weights.source", weights.source, "代价配置必须有来源。")


def _grid_index(value: float, step: float) -> int:
    return int(round(value / step))


def _state_key(soc1: float, soc2: float, mode: int | None, step: float,
               min1: float, min2: float) -> tuple[int, int, int | None]:
    return (int(round((soc1 - min1) / step)),
            int(round((soc2 - min2) / step)), mode)


def _floor_to_grid(soc: float, minimum: float, step: float) -> float:
    """Snap down from the hard lower bound so planning never overstates SOC."""
    if soc < minimum - EPS:
        return minimum - step
    return minimum + int((soc - minimum + EPS) / step) * step


def _mode_power(demand: float, mode: int, soc1: float, soc2: float) -> tuple[float, float]:
    if demand <= EPS:
        return 0.0, 0.0
    if mode == 1:
        return demand, 0.0
    if mode == 2:
        return 0.0, demand
    total_soc = soc1 + soc2
    if total_soc <= EPS:
        raise ValueError("双启模式无法在两组 SOC 总和为零时按比例分配。")
    return demand * soc1 / total_soc, demand * soc2 / total_soc


def state_transition(request: BatteryManagementRequest, point: PowerDemandPoint,
                     mode: int | None, soc1: float, soc2: float,
                     previous_mode: int | None = None) -> dict[str, Any]:
    """Check one action and return powers, next SOC, and equation costs."""
    if point.demand_bus_kw < -EPS:
        return {"feasible": False, "reason": "charging_not_supported"}
    if mode is None:
        if point.demand_bus_kw <= EPS and previous_mode is None:
            return {"feasible": True, "mode": None,
                    "battery_1_power_kw": 0.0, "battery_2_power_kw": 0.0,
                    "soc_1_after": soc1, "soc_2_after": soc2,
                    "cost_terms": {key: 0.0 for key in
                                   ("power", "switch", "soc_balance", "low_load_mode3")}}
        return {"feasible": False, "reason": "mode must be 1, 2, or 3 for positive load"}
    if mode not in MODES:
        return {"feasible": False, "reason": "mode must be 1, 2, or 3"}
    if point.demand_bus_kw <= EPS and previous_mode is not None and mode != previous_mode:
        return {"feasible": False, "reason": "zero_load_preserves_previous_mode"}
    if previous_mode is not None and mode != previous_mode:
        if (previous_mode, mode) not in request.allowed_mode_transitions:
            return {"feasible": False, "reason": "mode_transition_not_allowed"}
    try:
        p1, p2 = _mode_power(point.demand_bus_kw, mode, soc1, soc2)
    except ValueError as exc:
        return {"feasible": False, "reason": str(exc)}
    batteries = (request.battery_1, request.battery_2)
    powers = (p1, p2)
    for index, (power, battery) in enumerate(zip(powers, batteries), 1):
        if power > battery.max_power_kw + EPS:
            return {"feasible": False, "reason": f"battery_{index}_max_power"}
        if power > EPS and power < battery.min_power_kw - EPS:
            return {"feasible": False, "reason": f"battery_{index}_min_power"}
    duration_h = point.duration_s / 3600.0
    next_socs = tuple(
        current - power * duration_h / (battery.discharge_efficiency * battery.capacity_kwh)
        for current, power, battery in zip((soc1, soc2), powers, batteries)
    )
    for index, (next_soc, battery) in enumerate(zip(next_socs, batteries), 1):
        if next_soc < battery.soc_min - EPS or next_soc > 1 + EPS:
            return {"feasible": False, "reason": f"battery_{index}_soc_bound",
                    "next_soc": next_soc}
    components = _dp_costs(request, mode, p1, p2, soc1, soc2,
                           point.demand_bus_kw, previous_mode)
    return {
        "feasible": True,
        "mode": mode,
        "battery_1_power_kw": p1,
        "battery_2_power_kw": p2,
        "soc_1_after": next_socs[0],
        "soc_2_after": next_socs[1],
        "cost_terms": components,
    }


def _dp_costs(request: BatteryManagementRequest, mode: int,
              p1: float, p2: float, soc1: float, soc2: float,
              demand: float, previous_mode: int | None) -> dict[str, float]:
    """FDP objective from thesis equations (5-26)--(5-30)."""
    weight = request.weights
    mode_switch = 0.0
    if (previous_mode in MODES and mode != previous_mode
            and not (mode in (1, 2) and previous_mode in (1, 2))):
        mode_switch = weight.k_s
    soc_difference = abs(soc1 - soc2)
    return {
        "power": weight.k_p * (p1 ** 2 + p2 ** 2),
        "switch": mode_switch,
        # Equation (5-29) prints mode 1; this implementation applies the
        # same single-pack term symmetrically to the paper's mode 2.
        "soc_balance": (weight.k_soc * soc_difference if mode in (1, 2)
                        else 2 * weight.k_soc * soc_difference if mode == 3 else 0.0),
        "low_load_mode3": (weight.k_low if mode == 3 and demand > EPS
                           and demand / request.load_rate_reference_kw
                           <= weight.low_load_threshold else 0.0),
    }


def _row(request: BatteryManagementRequest, point: PowerDemandPoint,
         transition: dict[str, Any],
         actual_before: tuple[float, float], actual_after: tuple[float, float],
         planned_before: tuple[float, float], planned_after: tuple[float, float]) -> dict[str, Any]:
    duration_h = point.duration_s / 3600.0
    p1, p2 = transition["battery_1_power_kw"], transition["battery_2_power_kw"]
    bus_energy_1, bus_energy_2 = p1 * duration_h, p2 * duration_h
    drawn_1 = bus_energy_1 / request.battery_1.discharge_efficiency
    drawn_2 = bus_energy_2 / request.battery_2.discharge_efficiency
    return {
        "segment_id": point.segment_id,
        "phase": point.phase,
        "start_offset_s": point.start_offset_s,
        "duration_s": point.duration_s,
        "demand_bus_kw": point.demand_bus_kw,
        "demand_bus_energy_kwh": point.demand_bus_kw * duration_h,
        "mode": transition["mode"],
        "battery_1_power_kw": transition["battery_1_power_kw"],
        "battery_2_power_kw": transition["battery_2_power_kw"],
        "soc_1_before": actual_before[0],
        "soc_2_before": actual_before[1],
        "soc_1_after": actual_after[0],
        "soc_2_after": actual_after[1],
        "planning_soc_1_before": planned_before[0],
        "planning_soc_2_before": planned_before[1],
        "planning_soc_1_after": planned_after[0],
        "planning_soc_2_after": planned_after[1],
        "grid_soc_1_after": planned_after[0],
        "grid_soc_2_after": planned_after[1],
        "motor_energy_kwh": (point.motor_power_kw * duration_h
                              if point.motor_power_kw is not None else None),
        "propulsion_bus_energy_kwh": (point.propulsion_bus_kw * duration_h
                                       if point.propulsion_bus_kw is not None else None),
        "auxiliary_bus_energy_kwh": (point.auxiliary_bus_kw * duration_h
                                      if point.auxiliary_bus_kw is not None else None),
        "conversion_delta_kwh": (
            (point.propulsion_bus_kw - point.motor_power_kw) * duration_h
            if point.propulsion_bus_kw is not None and point.motor_power_kw is not None else None
        ),
        "battery_1_bus_energy_kwh": bus_energy_1,
        "battery_2_bus_energy_kwh": bus_energy_2,
        "battery_1_energy_drawn_kwh": drawn_1,
        "battery_2_energy_drawn_kwh": drawn_2,
        "battery_1_discharge_loss_kwh": drawn_1 - bus_energy_1,
        "battery_2_discharge_loss_kwh": drawn_2 - bus_energy_2,
        "input_provenance": {
            "demand_source": _source_dict(point.demand_source),
            "conversion_source": _source_dict(point.conversion_source),
            "component_source": _source_dict(point.component_source),
            "load_rate_reference_source": _source_dict(request.load_rate_reference_source),
            "allowed_mode_transitions_source": _source_dict(
                request.allowed_mode_transitions_source),
            "weights_source": _source_dict(request.weights.source),
            "assumptions": request.assumptions,
            "battery_1": {key: source.to_dict()
                          for key, source in request.battery_1.sources.items()},
            "battery_2": {key: source.to_dict()
                          for key, source in request.battery_2.sources.items()},
            "scenario_id": request.scenario_id,
            "source_mode": request.source_mode,
        },
        "cost_terms": transition["cost_terms"],
    }


def _artifact(name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "artifact_id": f"tmanagement:{name}:{_sha(rows)[:16]}",
        "sha256": _sha(rows),
        "row_count": len(rows),
        "schema_version": SCHEMA_VERSION,
        "name": name,
    }


def _cost_summary(rows: list[dict[str, Any]]) -> dict[str, float]:
    totals = {"power": 0.0, "switch": 0.0, "soc_balance": 0.0,
              "low_load_mode3": 0.0}
    for row in rows:
        for key in totals:
            totals[key] += float(row["cost_terms"][key])
    totals["total"] = sum(totals.values())
    return totals


def _feedback(request: BatteryManagementRequest, *, scope: str,
              point: PowerDemandPoint | None, violated: str, demand: float | None,
              deliverable: float | None, soc1: float | None, soc2: float | None,
              certainty: str, suggestion: str, limit_source: Any = None) -> dict[str, Any]:
    battery_hash = _sha({"battery_1": request.battery_1.to_dict(),
                         "battery_2": request.battery_2.to_dict()})
    interval = None
    if point is not None:
        interval = {
            "segment_id": point.segment_id,
            "start_offset_s": point.start_offset_s,
            "duration_s": point.duration_s,
            "end_offset_s": point.start_offset_s + point.duration_s,
        }
    return {
        "selected_candidate_id": request.selected_candidate_id,
        "selected_demand_ref": request.selected_demand_ref,
        "request_revision": request.request_revision,
        "snapshot_id": request.snapshot_id,
        "scenario_id": request.scenario_id,
        "call_id": request.call_id,
        "demand_series_hash": _sha([point.to_dict() for point in request.demand_series]),
        "battery_config_hash": battery_hash,
        "failure_scope": scope,
        "first_failure_interval": interval,
        "violated_rule": violated,
        "demand_kw": demand,
        "deliverable_kw": deliverable,
        "soc_1": soc1,
        "soc_2": soc2,
        "limit_source_ref": limit_source,
        "diagnostic_certainty": certainty,
        "suggested_search_action": suggestion,
        "hard_constraint_applicable_to_candidate_family": scope == "instantaneous_power",
    }


def _run_management_fdp_once(request: BatteryManagementRequest) -> dict[str, Any]:
    """Run the draft v2 FDP and return a complete ToolRun-shaped dictionary."""
    started = _now()
    wall_start = time.perf_counter()
    diagnostics = validate_request(request)
    if diagnostics:
        outcome = "unsupported_charging" if any(
            item["code"] == "charging_not_supported" for item in diagnostics
        ) else ("needs_input" if all(
            item["code"] == "missing_input" for item in diagnostics
        ) else "invalid_input")
        return _envelope(request, started_at=started, finished_at=_now(),
                         execution_status="completed", outcome_code=outcome,
                         diagnostics=diagnostics, payload=None,
                         runtime_ms=(time.perf_counter() - wall_start) * 1000)

    # A sourced instantaneous power maximum is a candidate-family constraint.
    for point in request.demand_series:
        deliverable = request.battery_1.max_power_kw + request.battery_2.max_power_kw
        if point.demand_bus_kw > deliverable + EPS:
            diagnostic = _diagnostic(
                "fdp", "instantaneous_power_exceeds_known_limit", point.segment_id,
                "demand_bus_kw", point.demand_bus_kw, deliverable, "kW",
                [_source_dict(request.battery_1.sources.get("max_power_kw")),
                 _source_dict(request.battery_2.sources.get("max_power_kw"))],
                False, "该峰值在当前两组功率上限下不可供能；适用于有相同上限证据的候选族。",
            )
            feedback = _feedback(
                request, scope="instantaneous_power", point=point,
                violated="P_1 + P_2 <= P_max_1 + P_max_2",
                demand=point.demand_bus_kw, deliverable=deliverable,
                soc1=request.battery_1.initial_soc, soc2=request.battery_2.initial_soc,
                certainty="proven_under_request", suggestion="reduce_peak_power_or_revise_constraints",
                limit_source=[_source_dict(request.battery_1.sources.get("max_power_kw")),
                              _source_dict(request.battery_2.sources.get("max_power_kw"))],
            )
            payload = {"safe": False, "feedback": feedback,
                       "diagnostic_ref": {"diagnostic_id": _sha(diagnostic)[:16]},
                       "algorithm_version": request.grid.algorithm_version,
                       "grid": request.grid.to_dict(), "weights": request.weights.to_dict()}
            return _envelope(request, started_at=started, finished_at=_now(),
                             execution_status="completed", outcome_code="infeasible_under_assumptions",
                             diagnostics=[diagnostic], payload=payload,
                             runtime_ms=(time.perf_counter() - wall_start) * 1000)

    # Quantize accumulated exact SOC for the state key. Never subtract a new
    # interval from an already-rounded SOC: that would lose up to one grid step
    # at every interval and falsely exhaust a battery on fine time grids.
    initial_mode = request.previous_mode if request.previous_mode in MODES else None
    initial_grid_soc1 = _floor_to_grid(request.battery_1.initial_soc,
                                       request.battery_1.soc_min, request.grid.soc_step)
    initial_grid_soc2 = _floor_to_grid(request.battery_2.initial_soc,
                                       request.battery_2.soc_min, request.grid.soc_step)
    layers: dict[tuple[int, int, int | None], dict[str, Any]] = {
        _state_key(initial_grid_soc1, initial_grid_soc2, initial_mode,
                   request.grid.soc_step, request.battery_1.soc_min,
                   request.battery_2.soc_min): {
            "soc1": initial_grid_soc1,
            "soc2": initial_grid_soc2,
            "actual_soc1": request.battery_1.initial_soc,
            "actual_soc2": request.battery_2.initial_soc,
            "mode": initial_mode,
            "previous_mode": request.previous_mode,
            "cost_terms": {key: 0.0 for key in ("power", "switch", "soc_balance", "low_load_mode3")},
            # Keep a parent pointer while searching. Copying the complete
            # trajectory for every candidate makes long horizons quadratic.
            "parent": None,
            "row": None,
        }
    }
    transitions_seen = 0
    peak_states_per_layer = len(layers)
    for point_index, point in enumerate(request.demand_series):
        next_layer: dict[tuple[int, int, int | None], dict[str, Any]] = {}
        for state in layers.values():
            if point.demand_bus_kw <= EPS:
                candidate_modes: Iterable[int | None] = (state["mode"],)
            else:
                candidate_modes = MODES
            for mode in candidate_modes:
                transitions_seen += 1
                if transitions_seen > request.grid.max_transitions:
                    diagnostic = _diagnostic(
                        "fdp", "grid_unresolved", point.segment_id, "grid.max_transitions",
                        transitions_seen, request.grid.max_transitions, "transitions", [], True,
                        "FDP transition budget exhausted; rerun with an explicit larger budget or coarser grid.",
                    )
                    payload = {"safe": False, "feedback": _feedback(
                        request, scope="grid_budget", point=point, violated="max_transitions",
                        demand=point.demand_bus_kw, deliverable=None,
                        soc1=state["actual_soc1"], soc2=state["actual_soc2"],
                        certainty="transition_budget_exhausted",
                        suggestion="increase_fdp_budget_or_adjust_grid"),
                        "grid": request.grid.to_dict(),
                        "transitions_seen": transitions_seen,
                        "peak_states_per_layer": max(
                            peak_states_per_layer, len(next_layer)
                        )}
                    return _envelope(request, started_at=started, finished_at=_now(),
                                     execution_status="completed", outcome_code="grid_unresolved",
                                     diagnostics=[diagnostic], payload=payload,
                                     runtime_ms=(time.perf_counter() - wall_start) * 1000)
                transition = state_transition(request, point, mode,
                                              state["actual_soc1"], state["actual_soc2"],
                                              state["previous_mode"])
                if not transition.get("feasible"):
                    continue
                transition["cost_terms"] = _dp_costs(
                    request, mode, transition["battery_1_power_kw"],
                    transition["battery_2_power_kw"], state["soc1"], state["soc2"],
                    point.demand_bus_kw, state["previous_mode"])
                actual_after = (transition["soc_1_after"], transition["soc_2_after"])
                next_grid_soc1 = _floor_to_grid(
                    actual_after[0], request.battery_1.soc_min,
                    request.grid.soc_step)
                next_grid_soc2 = _floor_to_grid(
                    actual_after[1], request.battery_2.soc_min,
                    request.grid.soc_step)
                if (next_grid_soc1 < request.battery_1.soc_min - EPS
                        or next_grid_soc2 < request.battery_2.soc_min - EPS):
                    continue
                row = _row(
                    request, point, transition,
                    (state["actual_soc1"], state["actual_soc2"]), actual_after,
                    (state["soc1"], state["soc2"]),
                    (next_grid_soc1, next_grid_soc2),
                )
                cumulative = dict(state["cost_terms"])
                for key, value in transition["cost_terms"].items():
                    cumulative[key] += value
                candidate = {
                    "soc1": next_grid_soc1,
                    "soc2": next_grid_soc2,
                    "actual_soc1": actual_after[0],
                    "actual_soc2": actual_after[1],
                    "mode": mode,
                    "previous_mode": (mode if point.demand_bus_kw > EPS
                                       or state["previous_mode"] is not None else None),
                    "cost_terms": cumulative,
                    "parent": state,
                    "row": row,
                }
                key = _state_key(candidate["soc1"], candidate["soc2"], mode,
                                 request.grid.soc_step, request.battery_1.soc_min,
                                 request.battery_2.soc_min)
                old = next_layer.get(key)
                if old is None or _total_cost(candidate["cost_terms"]) < _total_cost(old["cost_terms"]):
                    next_layer[key] = candidate
            if len(next_layer) > request.grid.max_states_per_layer:
                diagnostic = _diagnostic(
                    "fdp", "grid_unresolved", point.segment_id, "grid.max_states_per_layer",
                    len(next_layer), request.grid.max_states_per_layer, "states", [], True,
                    "FDP state-layer budget exhausted; result is not a proof of global infeasibility.",
                )
                payload = {"safe": False, "feedback": _feedback(
                    request, scope="grid_budget", point=point, violated="max_states_per_layer",
                    demand=point.demand_bus_kw, deliverable=None,
                    soc1=state["soc1"], soc2=state["soc2"], certainty="grid_limited",
                    suggestion="increase_fdp_budget_or_adjust_grid"),
                    "grid": request.grid.to_dict(),
                    "transitions_seen": transitions_seen,
                    "peak_states_per_layer": len(next_layer)}
                return _envelope(request, started_at=started, finished_at=_now(),
                                 execution_status="completed", outcome_code="grid_unresolved",
                                 diagnostics=[diagnostic], payload=payload,
                                 runtime_ms=(time.perf_counter() - wall_start) * 1000)
        peak_states_per_layer = max(peak_states_per_layer, len(next_layer))
        if not next_layer:
            diagnostic = _diagnostic(
                "fdp", "no_path_on_requested_grid", point.segment_id, "soc_or_mode_constraints",
                point.demand_bus_kw, None, "kW", [], False,
                "当前离散 SOC 网格的全部状态均无可行转移；这不证明连续模型或实船全局无解。",
            )
            fallback_state = min(
                layers.values(), key=lambda item: _total_cost(item["cost_terms"])
            )
            payload = {"safe": False, "feedback": _feedback(
                request, scope="candidate_grid_path", point=point,
                violated="no_feasible_state_transition", demand=point.demand_bus_kw,
                deliverable=None,
                soc1=fallback_state["actual_soc1"], soc2=fallback_state["actual_soc2"],
                certainty="no_path_on_requested_grid",
                suggestion="inspect_grid_or_replan_candidate"),
                "grid": request.grid.to_dict(),
                "transitions_seen": transitions_seen,
                "peak_states_per_layer": peak_states_per_layer}
            return _envelope(request, started_at=started, finished_at=_now(),
                             execution_status="completed", outcome_code="grid_unresolved",
                             diagnostics=[diagnostic], payload=payload,
                             runtime_ms=(time.perf_counter() - wall_start) * 1000)
        layers = next_layer

    best = min(layers.values(), key=lambda item: _total_cost(item["cost_terms"]))
    rows = _materialize_rows(best)
    optimized_costs = {**best["cost_terms"], "total": _total_cost(best["cost_terms"])}
    verification = verify_trajectory(request, rows, optimized_costs)
    replay_costs = {**verification["recomputed_replayed_cost_terms"],
                    "total": _total_cost(verification["recomputed_replayed_cost_terms"])}
    artifacts = {
        "mode_trajectory_ref": _artifact("mode_trajectory", rows),
        "battery_1_power_ref": _artifact("battery_1_power", [
            {key: row[key] for key in ("start_offset_s", "duration_s", "battery_1_power_kw")}
            for row in rows
        ]),
        "battery_2_power_ref": _artifact("battery_2_power", [
            {key: row[key] for key in ("start_offset_s", "duration_s", "battery_2_power_kw")}
            for row in rows
        ]),
        "battery_1_soc_ref": _artifact("battery_1_soc", [
            {key: row[key] for key in ("start_offset_s", "soc_1_before", "soc_1_after")}
            for row in rows
        ]),
        "battery_2_soc_ref": _artifact("battery_2_soc", [
            {key: row[key] for key in ("start_offset_s", "soc_2_before", "soc_2_after")}
            for row in rows
        ]),
    }
    payload = {
        "safe": bool(verification["passed"]),
        **artifacts,
        "rows": rows,
        "mode_trajectory": [row["mode"] for row in rows],
        "cost_terms": optimized_costs,
        "replayed_cost_terms": replay_costs,
        "constraint_checks": verification["checks"],
        "diagnostic_ref": {"verification": verification["passed"]},
        "feedback_ref": None,
        "algorithm_version": request.grid.algorithm_version,
        "grid": request.grid.to_dict(),
        "weights": request.weights.to_dict(),
        "assumptions": request.assumptions + [
            {"assumption_id": "research-model-only",
             "value": "两组电池为可调度研究模型，输出不连接实船控制器。"},
            {"assumption_id": "no-charging-v1",
             "value": "首版三模式仅处理放电供能，未实现途中补能状态转移。"},
        ],
        "summary": {
            "final_soc_1": best["actual_soc1"], "final_soc_2": best["actual_soc2"],
            "final_planning_soc_1": best["soc1"], "final_planning_soc_2": best["soc2"],
            "demand_bus_energy_kwh": sum(row["demand_bus_energy_kwh"] for row in rows),
            "bus_energy_1_kwh": sum(row["battery_1_bus_energy_kwh"] for row in rows),
            "bus_energy_2_kwh": sum(row["battery_2_bus_energy_kwh"] for row in rows),
            "battery_energy_drawn_1_kwh": sum(row["battery_1_energy_drawn_kwh"] for row in rows),
            "battery_energy_drawn_2_kwh": sum(row["battery_2_energy_drawn_kwh"] for row in rows),
            "battery_discharge_loss_1_kwh": sum(row["battery_1_discharge_loss_kwh"] for row in rows),
            "battery_discharge_loss_2_kwh": sum(row["battery_2_discharge_loss_kwh"] for row in rows),
            "motor_energy_kwh": (sum(row["motor_energy_kwh"] or 0 for row in rows)
                                 if all(row["motor_energy_kwh"] is not None for row in rows) else None),
            "propulsion_bus_energy_kwh": (
                sum(row["propulsion_bus_energy_kwh"] or 0 for row in rows)
                if all(row["propulsion_bus_energy_kwh"] is not None for row in rows) else None),
            "auxiliary_bus_energy_kwh": (
                sum(row["auxiliary_bus_energy_kwh"] or 0 for row in rows)
                if all(row["auxiliary_bus_energy_kwh"] is not None for row in rows) else None),
            "conversion_delta_kwh": (
                sum(row["conversion_delta_kwh"] or 0 for row in rows)
                if all(row["conversion_delta_kwh"] is not None for row in rows) else None),
            "segment_summary": _segment_summary(rows),
            "mode_counts": {str(mode): sum(row["mode"] == mode for row in rows) for mode in MODES},
            "transitions_seen": transitions_seen,
            "peak_states_per_layer": peak_states_per_layer,
        },
    }
    outcome = "ok" if verification["passed"] else "tool_error"
    diagnostics = [] if verification["passed"] else [_diagnostic(
        "verification", "independent_check_failed", None, "constraint_checks",
        None, None, None, [], False,
        "规划路径未通过独立复核，不能标记 safe=true。",
    )]
    return _envelope(request, started_at=started, finished_at=_now(),
                     execution_status="completed", outcome_code=outcome,
                     diagnostics=diagnostics, payload=payload,
                     payload_ref=artifacts["mode_trajectory_ref"],
                     runtime_ms=(time.perf_counter() - wall_start) * 1000)


def _materialize_rows(state: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor = state
    while cursor is not None and cursor.get("row") is not None:
        rows.append(cursor["row"])
        cursor = cursor.get("parent")
    rows.reverse()
    return rows


def run_management_fdp(request: BatteryManagementRequest) -> dict[str, Any]:
    """Run FDP with one explicit, bounded adaptive retry.

    A budget result is unresolved search evidence, never proof of infeasibility.
    The retry changes only the research grid and records both attempts; a
    successful retry still passes the same independent trajectory verifier.
    """
    first = _run_management_fdp_once(request)
    if first.get("outcome_code") != "grid_unresolved":
        return first

    grid = request.grid
    retry_grid = replace(
        grid,
        soc_step=min(grid.soc_step * 2.0, 0.25),
        max_states_per_layer=min(grid.max_states_per_layer * 2, 10_000),
        max_transitions=min(grid.max_transitions * 4, 2_000_000),
        algorithm_version=f"{grid.algorithm_version}+bounded-adaptive-retry",
    )
    if retry_grid == grid:
        return first
    retry_request = replace(request, grid=retry_grid)
    second = _run_management_fdp_once(retry_request)
    attempts = [
        {"outcome_code": first.get("outcome_code"),
         "grid": grid.to_dict(),
         "diagnostics": first.get("diagnostics", []),
         "runtime_ms": first.get("runtime_ms")},
        {"outcome_code": second.get("outcome_code"),
         "grid": retry_grid.to_dict(),
         "diagnostics": second.get("diagnostics", []),
         "runtime_ms": second.get("runtime_ms")},
    ]
    # Preserve the first failure evidence when all bounded attempts remain
    # unresolved. The later attempt may fail at a different interval, but it
    # must not erase the original budget boundary used by the caller.
    if second.get("outcome_code") == "grid_unresolved":
        payload = dict(first.get("payload") or {})
        payload["original_failure"] = second.get("payload")
    else:
        payload = second.get("payload")
        if payload is None:
            payload = dict(first.get("payload") or {})
    payload["search_strategy"] = "bounded_adaptive_grid"
    payload["search_attempts"] = attempts
    second["payload"] = payload
    second["search_attempts"] = attempts
    second["search_strategy"] = "bounded_adaptive_grid"
    return second


def _total_cost(costs: dict[str, float]) -> float:
    return sum(float(costs.get(key, 0.0))
               for key in ("power", "switch", "soc_balance", "low_load_mode3"))


def _segment_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["segment_id"], []).append(row)
    result = []
    for segment_id, items in grouped.items():
        result.append({
            "segment_id": segment_id,
            "start_offset_s": items[0]["start_offset_s"],
            "end_offset_s": items[-1]["start_offset_s"] + items[-1]["duration_s"],
            "duration_s": sum(item["duration_s"] for item in items),
            "demand_bus_energy_kwh": sum(
                item["demand_bus_kw"] * item["duration_s"] / 3600 for item in items
            ),
            "battery_1_bus_energy_kwh": sum(item["battery_1_bus_energy_kwh"] for item in items),
            "battery_2_bus_energy_kwh": sum(item["battery_2_bus_energy_kwh"] for item in items),
            "battery_1_energy_drawn_kwh": sum(item["battery_1_energy_drawn_kwh"] for item in items),
            "battery_2_energy_drawn_kwh": sum(item["battery_2_energy_drawn_kwh"] for item in items),
            "battery_1_discharge_loss_kwh": sum(item["battery_1_discharge_loss_kwh"] for item in items),
            "battery_2_discharge_loss_kwh": sum(item["battery_2_discharge_loss_kwh"] for item in items),
            "final_soc_1": items[-1]["soc_1_after"],
            "final_soc_2": items[-1]["soc_2_after"],
            "phases": list(dict.fromkeys(item["phase"] for item in items)),
            "mode_counts": {str(mode): sum(item["mode"] == mode for item in items)
                            for mode in MODES},
        })
    return result


def run_state_machine(request: BatteryManagementRequest, *, high_load_threshold: float = 0.60,
                      exit_high_load_threshold: float = 0.55,
                      soc_difference_threshold: float = 0.40) -> dict[str, Any]:
    """Reference state-machine policy from thesis p. 87, with explicit hysteresis."""
    diagnostics = validate_request(request)
    if diagnostics:
        return {"outcome_code": "invalid_input", "diagnostics": diagnostics, "rows": []}
    soc1, soc2 = request.battery_1.initial_soc, request.battery_2.initial_soc
    mode = request.previous_mode if request.previous_mode in MODES else None
    rows: list[dict[str, Any]] = []
    costs = {key: 0.0 for key in ("power", "switch", "soc_balance", "low_load_mode3")}
    for point in request.demand_series:
        if point.demand_bus_kw <= EPS:
            target_mode = mode
        elif mode is None:
            target_mode = 1 if soc1 >= soc2 else 2
            if point.demand_bus_kw / request.load_rate_reference_kw > high_load_threshold:
                target_mode = 3
        else:
            rate = point.demand_bus_kw / request.load_rate_reference_kw
            if mode == 3:
                if rate > exit_high_load_threshold:
                    target_mode = 3
                else:
                    target_mode = 1 if soc1 >= soc2 else 2
            elif rate > high_load_threshold:
                target_mode = 3
            elif abs(soc1 - soc2) > soc_difference_threshold:
                target_mode = 1 if soc1 >= soc2 else 2
            else:
                target_mode = mode
        transition = state_transition(request, point, target_mode, soc1, soc2, mode)
        if not transition.get("feasible"):
            return {"outcome_code": "infeasible_under_assumptions",
                    "diagnostics": [_diagnostic(
                        "state_machine", "state_machine_transition_failed", point.segment_id,
                        "mode", target_mode, None, None, [], False,
                        transition.get("reason", "状态机动作不可行。"),
                    )], "rows": rows}
        row = _row(
            request, point, transition, (soc1, soc2),
            (transition["soc_1_after"], transition["soc_2_after"]),
            (soc1, soc2), (transition["soc_1_after"], transition["soc_2_after"]),
        )
        rows.append(row)
        for key, value in transition["cost_terms"].items():
            costs[key] += value
        soc1, soc2 = transition["soc_1_after"], transition["soc_2_after"]
        if point.demand_bus_kw > EPS:
            mode = target_mode
    cost_summary = {**costs, "total": _total_cost(costs)}
    verification = verify_trajectory(
        request, rows, cost_summary, conservative_grid=False
    )
    if not verification["passed"]:
        return {"outcome_code": "tool_error", "safe": False,
                "diagnostics": [_diagnostic(
                    "verification", "independent_check_failed", None,
                    "state_machine.constraint_checks", None, None, None, [], False,
                    "状态机轨迹未通过独立复核。",
                )], "rows": rows, "constraint_checks": verification["checks"]}
    return {"outcome_code": "ok", "safe": True, "rows": rows,
            "cost_terms": cost_summary, "constraint_checks": verification["checks"],
            "summary": {"final_soc_1": soc1, "final_soc_2": soc2,
                        "mode_counts": {str(m): sum(row["mode"] == m for row in rows) for m in MODES}},
            "policy": {"high_load_threshold": high_load_threshold,
                       "exit_high_load_threshold": exit_high_load_threshold,
                       "soc_difference_threshold": soc_difference_threshold,
                       "source": "thesis-5.2.1-state-machine"}}


def compare_policies(request: BatteryManagementRequest) -> dict[str, Any]:
    """Compare FDP and the reference state machine without hiding infeasibility."""
    fdp = run_management_fdp(request)
    state = run_state_machine(request)
    result: dict[str, Any] = {"fdp_outcome": fdp["outcome_code"],
                              "state_machine_outcome": state["outcome_code"]}
    if fdp.get("payload", {}).get("safe"):
        frows = fdp["payload"]["rows"]
        result["fdp"] = _policy_metrics(
            frows, fdp["payload"]["replayed_cost_terms"], initial_mode=request.previous_mode
        )
        result["fdp"]["grid_objective_cost_terms"] = fdp["payload"]["cost_terms"]
    else:
        result["fdp"] = {"feasible": False, "diagnostics": fdp.get("diagnostics", [])}
    if state["outcome_code"] == "ok" and state.get("safe"):
        result["state_machine"] = _policy_metrics(
            state["rows"], state["cost_terms"], initial_mode=request.previous_mode
        )
    else:
        result["state_machine"] = {"feasible": False, "diagnostics": state.get("diagnostics", [])}
    if result["fdp"].get("feasible") and result["state_machine"].get("feasible"):
        result["comparison"] = {
            "final_soc_delta": [
                result["fdp"]["final_soc_1"] - result["state_machine"]["final_soc_1"],
                result["fdp"]["final_soc_2"] - result["state_machine"]["final_soc_2"],
            ],
            "final_soc_difference_delta": (
                result["fdp"]["final_soc_difference"]
                - result["state_machine"]["final_soc_difference"]
            ),
            "max_soc_difference_delta": (
                result["fdp"]["max_soc_difference"]
                - result["state_machine"]["max_soc_difference"]
            ),
            "power_variation_delta_kw": [
                result["fdp"]["power_variation_1_kw"]
                - result["state_machine"]["power_variation_1_kw"],
                result["fdp"]["power_variation_2_kw"]
                - result["state_machine"]["power_variation_2_kw"],
            ],
            "switch_count_delta": result["fdp"]["switch_count"] - result["state_machine"]["switch_count"],
            "cost_term_delta": {
                key: result["fdp"]["cost_terms"][key]
                - result["state_machine"]["cost_terms"][key]
                for key in ("power", "switch", "soc_balance", "low_load_mode3", "total")
            },
            "constraint_violations": {
                "fdp": sum(not item["passed"] for item in
                           fdp["payload"].get("constraint_checks", [])),
                "state_machine": sum(not item["passed"] for item in
                                     state.get("constraint_checks", [])),
            },
        }
    else:
        result["comparison"] = {"status": "not_comparable_when_either_policy_is_infeasible"}
    return result


def _policy_metrics(rows: list[dict[str, Any]], costs: dict[str, float],
                    *, initial_mode: int | None = None) -> dict[str, Any]:
    powers = [(row["battery_1_power_kw"], row["battery_2_power_kw"]) for row in rows]
    soc_differences = [
        abs(value)
        for row in rows
        for value in (row["soc_1_before"] - row["soc_2_before"],
                      row["soc_1_after"] - row["soc_2_after"])
    ]
    switches = 0
    previous_mode = initial_mode
    for row in rows:
        if row["demand_bus_kw"] > EPS and row["mode"] in MODES:
            if previous_mode in MODES and row["mode"] != previous_mode:
                switches += 1
            previous_mode = row["mode"]
    final_soc_1 = rows[-1]["soc_1_after"] if rows else None
    final_soc_2 = rows[-1]["soc_2_after"] if rows else None
    return {
        "feasible": True,
        "final_soc_1": final_soc_1,
        "final_soc_2": final_soc_2,
        "final_soc_difference": (abs(final_soc_1 - final_soc_2)
                                 if rows else None),
        "max_soc_difference": max(soc_differences, default=0.0),
        "power_variation_1_kw": sum(
            abs(powers[i][0] - powers[i - 1][0]) for i in range(1, len(powers))
        ),
        "power_variation_2_kw": sum(
            abs(powers[i][1] - powers[i - 1][1]) for i in range(1, len(powers))
        ),
        "switch_count": switches,
        "cost_terms": costs,
        "total_cost": costs.get("total", _total_cost(costs)),
    }


def run_sensitivity(request: BatteryManagementRequest,
                    soc_steps: Iterable[float] = (0.1, 0.05, 0.025),
                    weight_profiles: dict[str, CostWeights] | None = None) -> list[dict[str, Any]]:
    """Run explicit grid/weight sensitivity cases and report runtime/outcome."""
    profiles = weight_profiles or {request.weights.profile_id: request.weights}
    records: list[dict[str, Any]] = []
    for step in soc_steps:
        for profile_id, weights in profiles.items():
            candidate = replace(request, grid=replace(request.grid, soc_step=step), weights=weights)
            started = time.perf_counter()
            result = run_management_fdp(candidate)
            elapsed = (time.perf_counter() - started) * 1000
            payload = result.get("payload") or {}
            summary = payload.get("summary") or {}
            records.append({"soc_step": step, "weight_profile": profile_id,
                            "outcome_code": result["outcome_code"],
                            "safe": payload.get("safe", False),
                            "total_cost": (payload.get("cost_terms") or {}).get("total"),
                            "runtime_ms": round(elapsed, 6),
                            "transitions_seen": summary.get("transitions_seen"),
                            "peak_states_per_layer": summary.get("peak_states_per_layer"),
                            "mode_trajectory": payload.get("mode_trajectory"),
                            "final_soc": [
                                summary.get("final_soc_1"), summary.get("final_soc_2")
                            ],
                            "constraint_checks_passed": sum(
                                bool(item.get("passed")) for item in payload.get("constraint_checks", [])
                            ),
                            "constraint_checks_total": len(payload.get("constraint_checks", []))})
    return records

