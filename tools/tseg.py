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
    for item_id in selected:
        item = by_id[item_id]
        if item.get("max_speed_kmh") is None:
            unknown.append(f"segments.{item_id}.max_speed_kmh")
        if item.get("waiting_h") is None:
            unknown.append(f"segments.{item_id}.waiting_h")
        source = SourceRef.from_dict(item["source"])
        segments.append(Segment(
            segment_id=item["id"], origin=item["origin"], destination=item["destination"],
            distance_km=item["distance_km"], max_speed_kmh=item.get("max_speed_kmh"),
            min_speed_kmh=item.get("min_speed_kmh"), waiting_h=item.get("waiting_h"),
            source=source, assumptions=item.get("assumptions", []),
        ))
    if unknown:
        return ToolResponse(
            tool="Tseg", status=Status.NEED_CLARIFICATION,
            missing_fields=unknown,
            questions=["请确认所选航段的限速和等待时间；未知值不能按 0 计算。"],
        )
    response = ToolResponse(tool="Tseg", status=Status.OK,
                            payload={"segments": [item.to_dict() for item in segments]})
    result = validate(response)
    if not result.valid:
        return ToolResponse(tool="Tseg", status=Status.INVALID_INPUT,
                            reason="Tseg 结果未通过契约校验：" + "; ".join(
                                issue.message for issue in result.issues))
    return response

