"""Tdata: collect route and vessel facts without inventing unknown values."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from schemas.types import (
    DataConflict,
    DataContext,
    ParameterValue,
    SourceRef,
    Status,
    ToolResponse,
    VesselState,
    VoyageRequest,
)
from schemas.validate import validate


ROOT = Path(__file__).resolve().parents[1]


def load_config(path: str | Path) -> dict[str, Any]:
    """Load the repository's JSON-compatible YAML files without a new dependency."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _source(raw: dict[str, Any], *, fallback: str) -> SourceRef:
    return SourceRef(
        source_id=str(raw.get("source_id", fallback)),
        kind=raw.get("kind", "document"),
        locator=raw.get("locator"),
        confirmed=bool(raw.get("confirmed", False)),
        note=raw.get("note"),
    )


def _parameter(value: Any, unit: str | None, source: SourceRef) -> ParameterValue:
    return ParameterValue(value=value, unit=unit, source=source)


def _adopted_parameter(
    facts: dict[str, Any], name: str, *, source_fallback: str
) -> tuple[ParameterValue | None, SourceRef | None, list[DataConflict]]:
    raw = facts.get("parameters", {}).get(name, {})
    adopted = raw.get("adopted")
    conflicts: list[DataConflict] = []
    candidates = raw.get("candidates", [])
    if len(candidates) > 1:
        values = [
            _parameter(
                item.get("value"),
                raw.get("unit"),
                _source({"source_id": item.get("source", source_fallback),
                         "kind": "document", "locator": item.get("locator")},
                        fallback=source_fallback),
            )
            for item in candidates if isinstance(item, dict)
        ]
        if values:
            conflicts.append(DataConflict(field_name=name, candidates=values))
    if not isinstance(adopted, dict) or adopted.get("value") is None:
        return None, None, conflicts
    return (
        _parameter(adopted["value"], raw.get("unit"), _source(
            adopted.get("source", {}), fallback=source_fallback
        )),
        _source(adopted.get("source", {}), fallback=source_fallback),
        [],
    )


def _normalize_place(value: str | None, aliases: dict[str, Any]) -> str | None:
    if value is None:
        return None
    entry = aliases.get("aliases", {}).get(value)
    return entry["canonical"] if entry else None


def collect(
    request: VoyageRequest,
    *,
    route_config: dict[str, Any],
    aliases_config: dict[str, Any],
    vessel_config: dict[str, Any],
    limits_config: dict[str, Any],
) -> ToolResponse:
    """Build a Tdata response; unresolved facts become clarification items."""
    missing: list[str] = []
    questions: list[str] = []
    origin = _normalize_place(request.origin, aliases_config)
    destination = _normalize_place(request.destination, aliases_config)
    if request.origin and origin is None:
        missing.append("origin")
        questions.append(f"无法识别起点“{request.origin}”，请从已登记港口或船闸中选择。")
    if request.destination and destination is None:
        missing.append("destination")
        questions.append(f"无法识别终点“{request.destination}”，请从已登记港口或船闸中选择。")

    route_id = route_config.get("route_id")
    parameters: dict[str, ParameterValue] = {}
    sources: dict[str, SourceRef] = {}
    conflicts: list[DataConflict] = []
    source_fallback = route_config.get("sources", {}).get("distance_km", {}).get(
        "source_id", "route_facts"
    )
    names_units = {
        "capacity_kwh": "kWh", "max_power_kw": "kW", "auxiliary_power_kw": "kW",
        "draft_m": "m", "max_speed_kmh": "km/h", "economic_speed_kmh": "km/h",
    }
    values: dict[str, float | None] = {}
    for name, unit in names_units.items():
        value, source, new_conflicts = _adopted_parameter(
            vessel_config, name, source_fallback=source_fallback
        )
        if value is not None and source is not None:
            parameters[name] = value
            sources[name] = source
            values[name] = value.value if isinstance(value.value, (int, float)) else None
        else:
            missing.append(f"vessel.{name}")
            questions.append(f"请确认船舶参数 {name} 及其来源。")
        conflicts.extend(new_conflicts)

    soc_min_raw = limits_config.get("limits", {}).get("soc_min", {})
    if soc_min_raw.get("adopted") is None:
        missing.append("vessel.soc_min")
        questions.append("请由 A 确认规划 SOC 安全下限，不能用停机线代替。")
    else:
        adopted = soc_min_raw["adopted"]
        source = _source(adopted.get("source", {}), fallback="limits")
        parameters["soc_min"] = _parameter(adopted.get("value"), "fraction", source)
        sources["soc_min"] = source
        values["soc_min"] = adopted.get("value")

    for name, value, unit, source_kind in (
        ("soc_initial", request.soc_initial, "fraction", "user"),
        ("draft_m", request.draft_m, "m", "user"),
    ):
        if value is not None:
            source = SourceRef(source_id="user_request", kind=source_kind, confirmed=True)
            parameters[name] = _parameter(value, unit, source)
            sources[name] = source
            values[name] = value

    if request.soc_initial is None:
        missing.append("vessel.soc_initial")
        questions.append("请补充初始 SOC。")
    if request.load_state is None and request.draft_m is None:
        missing.append("load_state_or_draft_m")
        questions.append("请补充载况或吃水。")
    for field_name, config_name in (("max_speed_kmh", "speed_limits"), ("waiting_h", "waiting_h")):
        if not route_config.get("sources", {}).get(config_name, {}).get("confirmed", False):
            missing.append(f"route.{field_name}")
            questions.append("请确认航段限速和船闸等待时间后再计算。")

    vessel = VesselState(
        vessel_id=vessel_config.get("vessel_id", "unknown"),
        capacity_kwh=values.get("capacity_kwh"),
        soc_initial=values.get("soc_initial"),
        soc_min=values.get("soc_min"),
        soc_alarm=None,
        max_power_kw=values.get("max_power_kw"),
        auxiliary_power_kw=values.get("auxiliary_power_kw"),
        draft_m=values.get("draft_m"),
        sources=sources,
        assumptions=[],
    )
    context = DataContext(
        vessel=vessel,
        route_id=route_id,
        parameters=parameters,
        conflicts=conflicts,
        missing_fields=sorted(set(missing)),
        assumptions=["路线距离来自已核对资料；未知运营约束未填充。"],
    )
    status = Status.NEED_CLARIFICATION if missing or conflicts else Status.OK
    response = ToolResponse(
        tool="Tdata", status=status, payload=context.to_dict(),
        missing_fields=sorted(set(missing)), questions=questions,
        reason="存在未确认事实或参数冲突。" if status != Status.OK else None,
    )
    result = validate(response)
    if not result.valid:
        return ToolResponse(tool="Tdata", status=Status.INVALID_INPUT,
                            reason="Tdata 结果未通过契约校验：" + "; ".join(
                                issue.message for issue in result.issues))
    return response


def tdata(request: VoyageRequest, **configs: dict[str, Any]) -> ToolResponse:
    defaults = {
        "route_config": load_config(ROOT / "configs/route_facts.yaml"),
        "aliases_config": load_config(ROOT / "configs/aliases.yaml"),
        "vessel_config": load_config(ROOT / "configs/vessel_facts.yaml"),
        "limits_config": load_config(ROOT / "configs/limits.yaml"),
    }
    defaults.update(configs)
    return collect(request, **defaults)

