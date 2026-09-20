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
    demo_policy_config: dict[str, Any],
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
        "capacity_kwh": "kWh", "battery_group_capacity_kwh": "kWh",
        "max_power_kw": "kW", "auxiliary_power_kw": "kW", "draft_m": "m",
        "max_speed_kmh": "km/h", "economic_speed_kmh": "km/h",
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

    voyage_demo = demo_policy_config.get("voyage_demo", {})
    demo_route_id = voyage_demo.get("route_id")
    demo_speed_cap = voyage_demo.get("operating_speed_cap_kmh")
    uses_demo_speed_cap = route_id == demo_route_id and isinstance(
        demo_speed_cap, (int, float)
    ) and not isinstance(demo_speed_cap, bool) and demo_speed_cap > 0
    if uses_demo_speed_cap:
        demo_source = SourceRef(
            source_id="docs/协作/D3_A_演示参数决策与集成记录.md",
            kind="assumption",
            locator="决策：软件演示运行上限",
            confirmed=False,
            note="仅用于synthetic_demo离散搜索，不是航道法定限速或实船批准速度。",
        )
        parameters["max_speed_kmh"] = _parameter(demo_speed_cap, "km/h", demo_source)
        sources["max_speed_kmh"] = demo_source
        values["max_speed_kmh"] = float(demo_speed_cap)
        missing = [field for field in missing if field != "vessel.max_speed_kmh"]
        questions = [
            question for question in questions
            if question != "请确认船舶参数 max_speed_kmh 及其来源。"
        ]

    effective_capacity = demo_policy_config.get("battery", {}).get(
        "total_effective_capacity_kwh"
    )
    if isinstance(effective_capacity, (int, float)) and not isinstance(
        effective_capacity, bool
    ) and effective_capacity > 0:
        nominal = parameters.get("capacity_kwh")
        if nominal is not None:
            parameters["nominal_capacity_kwh"] = nominal
        effective_source = SourceRef(
            source_id="docs/协作/D2_A_决策与实现记录.md",
            kind="assumption",
            locator="A批准的软件演示规则：初始SOH与有效总容量",
            confirmed=False,
            note="1567.85kWh标称总容量乘90%初始SOH一次；不与两组容量重复相加。",
        )
        parameters["capacity_kwh"] = _parameter(
            effective_capacity, "kWh", effective_source
        )
        sources["capacity_kwh"] = effective_source
        values["capacity_kwh"] = float(effective_capacity)

    for config_name, field_name, question in (
        ("soc_min", "soc_min", "请由 A 确认规划 SOC 安全下限，不能用停机线代替。"),
        ("soc_alarm", "soc_alarm", "请确认软件 SOC 关注阈值。"),
    ):
        raw = limits_config.get("limits", {}).get(config_name, {})
        adopted = raw.get("adopted")
        if not isinstance(adopted, dict) or adopted.get("value") is None:
            missing.append(f"vessel.{field_name}")
            questions.append(question)
            continue
        source = _source(adopted.get("source", {}), fallback="limits")
        parameters[field_name] = _parameter(adopted["value"], "fraction", source)
        sources[field_name] = source
        values[field_name] = adopted["value"]

    initial_soh = demo_policy_config.get("battery", {}).get("initial_soh")
    if initial_soh is not None:
        source = SourceRef(
            source_id="configs/demo_policy.yaml",
            kind="assumption",
            locator="battery.initial_soh",
            confirmed=False,
            note="A批准的软件演示初始值；实船部署前须由测量或BMS数据替换。",
        )
        parameters["soh_initial"] = _parameter(initial_soh, "fraction", source)

    demo_source = SourceRef(
        source_id="configs/demo_policy.yaml",
        kind="assumption",
        locator="A批准的软件演示规则",
        confirmed=False,
        note="仅用于软件演示；实船部署前须以当前BMS、设备协议和船东批准参数替换。",
    )
    for name, value, unit in (
        ("soc_warning", demo_policy_config.get("soc", {}).get("warning_min"), "fraction"),
        ("soc_critical", demo_policy_config.get("soc", {}).get("critical_min"), "fraction"),
        ("parallel_enter_propulsion_kw",
         demo_policy_config.get("dispatch", {}).get("parallel_enter_propulsion_kw"), "kW"),
        ("parallel_exit_propulsion_kw",
         demo_policy_config.get("dispatch", {}).get("parallel_exit_propulsion_kw"), "kW"),
    ):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            parameters[name] = _parameter(float(value), unit, demo_source)
    battery_demo = demo_policy_config.get("battery", {})
    group_effective = battery_demo.get("group_effective_capacity_kwh")
    if isinstance(group_effective, (int, float)) and not isinstance(group_effective, bool):
        parameters["battery_group_effective_capacity_kwh"] = _parameter(
            float(group_effective), "kWh", demo_source
        )
    roles = battery_demo.get("roles", {})
    for key, parameter_name in (
        ("group_1", "battery_group_1_role"),
        ("group_2", "battery_group_2_role"),
    ):
        if isinstance(roles.get(key), str) and roles[key].strip():
            parameters[parameter_name] = _parameter(roles[key], None, demo_source)

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
    if (not uses_demo_speed_cap
            and not route_config.get("sources", {}).get("speed_limits", {}).get(
                "confirmed", False
            )):
        missing.append("route.max_speed_kmh")
        questions.append("请确认所选航段限速；未知值不能作为安全约束参与计算。")

    locks_policy = demo_policy_config.get("locks", {})
    henan_route_ids = set(locks_policy.get("henan_route_ids", []))
    default_queue_wait_h = locks_policy.get("henan_default_queue_wait_h")
    uses_henan_demo_wait = route_id in henan_route_ids and default_queue_wait_h is not None
    assumptions = ["路线距离来自已核对资料；未知运营约束未填充。"]
    if uses_demo_speed_cap:
        assumptions.append(
            f"synthetic_demo软件运行上限为{float(demo_speed_cap):g}km/h；"
            "不是航道法定限速或实船批准速度。"
        )
    if uses_henan_demo_wait:
        source = SourceRef(
            source_id="configs/demo_policy.yaml",
            kind="assumption",
            locator="locks.henan_default_queue_wait_h",
            confirmed=False,
            note="仅表示河南境内演示的排队等待默认值；可人工覆盖，不包含船闸内部通行时间。",
        )
        parameters["default_queue_wait_h"] = _parameter(
            default_queue_wait_h, "h", source
        )
        assumptions.append(
            "河南境内船闸演示默认排队等待0小时，可人工覆盖；船闸内部通行时间另计。"
        )
    elif not route_config.get("sources", {}).get("waiting_h", {}).get("confirmed", False):
        missing.append("route.waiting_h")
        questions.append("请确认船闸排队等待时间；未知值不能按0小时计算。")

    vessel = VesselState(
        vessel_id=vessel_config.get("vessel_id", "unknown"),
        capacity_kwh=values.get("capacity_kwh"),
        soc_initial=values.get("soc_initial"),
        soc_min=values.get("soc_min"),
        soc_alarm=values.get("soc_alarm"),
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
        assumptions=assumptions,
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
        "demo_policy_config": load_config(ROOT / "configs/demo_policy.yaml"),
    }
    defaults.update(configs)
    return collect(request, **defaults)
