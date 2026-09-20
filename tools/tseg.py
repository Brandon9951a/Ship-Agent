"""Tseg: select contiguous route segments in the requested direction."""

from __future__ import annotations

from typing import Any

from schemas.types import DataContext, Segment, SourceRef, Status, ToolResponse, VoyageRequest
from schemas.validate import validate


def _canonical(value: str | None, aliases: dict[str, Any]) -> str | None:
    if value is None:
        return None
    item = aliases.get("aliases", {}).get(value)
    return item["canonical"] if item else None


def segment(
    request: VoyageRequest,
    data: DataContext,
    *,
    route_config: dict[str, Any],
    aliases_config: dict[str, Any],
    demo_policy_config: dict[str, Any] | None = None,
) -> ToolResponse:
    origin = _canonical(request.origin, aliases_config)
    destination = _canonical(request.destination, aliases_config)
    if origin is None or destination is None:
        missing = [field for field, value in (("origin", origin), ("destination", destination)) if value is None]
        return ToolResponse(
            tool="Tseg", status=Status.NEED_CLARIFICATION,
            missing_fields=missing,
            questions=["请提供已登记且可归一化的起点和终点。"],
        )
    by_id = {item["id"]: item for item in route_config.get("segments", [])}
    selected: list[str] | None = None
    for route in route_config.get("routes", {}).values():
        ids = route.get("segment_ids", [])
        if route.get("origin") == origin and route.get("destination") == destination:
            selected = ids
            break
        for start in range(len(ids)):
            for end in range(start + 1, len(ids) + 1):
                candidate = [by_id[item_id] for item_id in ids[start:end]]
                if candidate[0]["origin"] == origin and candidate[-1]["destination"] == destination:
                    selected = ids[start:end]
                    break
            if selected is not None:
                break
        if selected is not None:
            break
    if selected is None:
        return ToolResponse(
            tool="Tseg", status=Status.NEED_CLARIFICATION,
            missing_fields=["route_subsequence"],
            questions=["起点和终点不构成已登记的连续正向或返程子航线，请确认路线。"],
        )
    unknown: list[str] = []
    segments: list[Segment] = []
    locks_policy = (demo_policy_config or {}).get("locks", {})
    henan_route_ids = set(locks_policy.get("henan_route_ids", []))
    default_queue_wait_h = locks_policy.get("henan_default_queue_wait_h")
    uses_henan_demo_wait = (
        data.route_id in henan_route_ids and default_queue_wait_h is not None
    )
    voyage_demo = (demo_policy_config or {}).get("voyage_demo", {})
    demo_speed_cap = voyage_demo.get("operating_speed_cap_kmh")
    uses_demo_speed_cap = (
        data.route_id == voyage_demo.get("route_id")
        and isinstance(demo_speed_cap, (int, float))
        and not isinstance(demo_speed_cap, bool)
        and demo_speed_cap > 0
    )
    for item_id in selected:
        item = by_id[item_id]
        max_speed_kmh = item.get("max_speed_kmh")
        assumptions = list(item.get("assumptions", []))
        if max_speed_kmh is None and uses_demo_speed_cap:
            max_speed_kmh = float(demo_speed_cap)
            assumptions.append(
                f"synthetic_demo软件运行上限{max_speed_kmh:g}km/h；"
                "不是航道法定限速或实船批准速度。"
            )
        elif max_speed_kmh is None:
            unknown.append(f"segments.{item_id}.max_speed_kmh")
        waiting_h = item.get("waiting_h")
        if waiting_h is None and uses_henan_demo_wait:
            waiting_h = default_queue_wait_h
            assumptions.append(
                "河南境内船闸演示默认排队等待0小时，可人工覆盖；"
                "该值不包含船闸内部通行时间。"
            )
        elif waiting_h is None:
            unknown.append(f"segments.{item_id}.waiting_h")
        source = SourceRef.from_dict(item["source"])
        segments.append(Segment(
            segment_id=item["id"], origin=item["origin"], destination=item["destination"],
            distance_km=item["distance_km"], max_speed_kmh=max_speed_kmh,
            min_speed_kmh=item.get("min_speed_kmh"), waiting_h=waiting_h,
            source=source, assumptions=assumptions,
        ))
    if unknown:
        return ToolResponse(
            tool="Tseg", status=Status.NEED_CLARIFICATION,
            payload={"segments": [item.to_dict() for item in segments]},
            missing_fields=unknown,
            questions=["请确认所选航段仍缺失的限速或等待时间；未批准的未知值不能按0计算。"],
        )
    response = ToolResponse(tool="Tseg", status=Status.OK,
                            payload={"segments": [item.to_dict() for item in segments]})
    result = validate(response)
    if not result.valid:
        return ToolResponse(tool="Tseg", status=Status.INVALID_INPUT,
                            reason="Tseg 结果未通过契约校验：" + "; ".join(
                                issue.message for issue in result.issues))
    return response
