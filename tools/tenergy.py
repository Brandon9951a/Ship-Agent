"""Tenergy candidate calculator with explicit model provenance and scope.

This module does not select vessel parameters.  It refuses incomplete inputs and
labels synthetic demonstrations so they cannot be mistaken for ship estimates.
"""

from dataclasses import dataclass
from typing import Literal

from schemas.types import EnergyResult, Segment, Status, ToolResponse
from schemas.validate import validate
from tools.energy_math import number, segment_energy


@dataclass(frozen=True)
class EnergyModel:
    model_id: str | None = None
    coefficient_kw_per_kmh3: float | None = None
    auxiliary_power_kw: float | None = None
    energy_scope: Literal["propulsion", "total"] | None = None
    usage: Literal["approved", "synthetic_demo"] | None = None
    approval_ref: str | None = None
    default_waiting_h: float | None = None


def _clarification(missing: list[str], questions: list[str]) -> ToolResponse:
    return ToolResponse("Tenergy", Status.NEED_CLARIFICATION,
                        missing_fields=missing, questions=questions)


def run_tenergy(segments: list[Segment], candidate_speeds_kmh: list[float],
                model: EnergyModel | None) -> ToolResponse:
    """Calculate the segment/speed cross-product or return an explicit blocker.

    ``approved`` requires an approval reference and does not permit a waiting-time
    assumption. ``synthetic_demo`` may use ``default_waiting_h`` but every result
    is marked as non-vessel evidence.
    """
    missing, questions = [], []
    if not segments:
        missing.append("segments")
        questions.append("请先由Tseg提供至少一个有效航段。")
    if not candidate_speeds_kmh:
        missing.append("candidate_speeds_kmh")
        questions.append("请提供至少一个候选航速（km/h）。")
    if model is None:
        missing.append("model")
        questions.append("请提供能耗模型、计量口径及其批准或演示标记。")
        return _clarification(missing, questions)
    for field, question in (
        ("model_id", "请提供可追溯的能耗模型编号。"),
        ("coefficient_kw_per_kmh3", "请提供三次方推进模型系数及来源。"),
        ("auxiliary_power_kw", "请提供辅助功率及计量边界。"),
        ("energy_scope", "请明确能耗口径为propulsion或total。"),
        ("usage", "请明确模型是approved还是synthetic_demo。"),
    ):
        if getattr(model, field) is None or (isinstance(getattr(model, field), str)
                                             and not getattr(model, field).strip()):
            missing.append("model." + field)
            questions.append(question)
    if model.usage == "approved" and not (model.approval_ref and model.approval_ref.strip()):
        missing.append("model.approval_ref")
        questions.append("正式计算需提供A的参数批准记录位置。")
    if model.usage not in (None, "approved", "synthetic_demo"):
        return ToolResponse("Tenergy", Status.INVALID_INPUT,
                            reason="model.usage只允许approved或synthetic_demo。")
    if model.usage == "approved" and model.default_waiting_h is not None:
        return ToolResponse("Tenergy", Status.INVALID_INPUT,
                            reason="approved模型不得用默认等待时间代替逐航段已确认输入。")
    for index, segment in enumerate(segments):
        result = validate(segment)
        if not result.valid:
            if result.status == Status.NEED_CLARIFICATION:
                unresolved = []
                for item in result.issues:
                    if item.field == "waiting_h" and model.default_waiting_h is not None:
                        continue
                    unresolved.append(item)
                    missing.append(f"segments[{index}].{item.field}")
                    questions.append(f"请补充航段{segment.segment_id}的{item.field}。")
                if unresolved:
                    continue
            else:
                return ToolResponse("Tenergy", Status.INVALID_INPUT,
                                    reason=f"segments[{index}]不符合公共接口：" +
                                           "; ".join(item.message for item in result.issues))
        if segment.waiting_h is None and model.default_waiting_h is None:
            missing.append(f"segments[{index}].waiting_h")
            questions.append(f"请确认航段{segment.segment_id}的等待时长；未知值不能按0计算。")
        if (model.usage == "approved"
                and (segment.source is None or not segment.source.confirmed)):
            missing.append(f"segments[{index}].source.confirmed")
            questions.append(
                f"航段{segment.segment_id}的来源尚未确认，不能生成approved结果。"
            )
    if missing:
        return _clarification(missing, questions)

    try:
        coefficient = number(model.coefficient_kw_per_kmh3, "coefficient_kw_per_kmh3", minimum=0)
        auxiliary = number(model.auxiliary_power_kw, "auxiliary_power_kw", minimum=0)
        default_waiting = (None if model.default_waiting_h is None else
                           number(model.default_waiting_h, "default_waiting_h", minimum=0))
        speeds = [number(speed, "candidate_speed_kmh", minimum=0) for speed in candidate_speeds_kmh]
        if any(speed == 0 for speed in speeds):
            raise ValueError("candidate speeds must be positive")
        if len(set(speeds)) != len(speeds):
            raise ValueError("candidate speeds must not repeat")
        ids = [segment.segment_id for segment in segments]
        if len(set(ids)) != len(ids):
            raise ValueError("segment ids must not repeat")
        results = []
        for segment in segments:
            waiting = segment.waiting_h if segment.waiting_h is not None else default_waiting
            for speed in speeds:
                if segment.min_speed_kmh is not None and speed < segment.min_speed_kmh:
                    raise ValueError(f"{segment.segment_id}: candidate speed below minimum")
                if segment.max_speed_kmh is not None and speed > segment.max_speed_kmh:
                    raise ValueError(f"{segment.segment_id}: candidate speed above limit")
                values = segment_energy(segment.distance_km, speed, waiting, coefficient,
                                        auxiliary, scope=model.energy_scope)
                assumptions = list(segment.assumptions)
                if model.usage == "synthetic_demo":
                    assumptions.append(
                        "synthetic_demo_only: 仅用于软件仿真可行性，"
                        "不得作为实船安全、运营批准或模型标定结论"
                    )
                    if segment.waiting_h is None:
                        assumptions.append(f"等待时间使用演示假设{default_waiting}h")
                results.append(EnergyResult(
                    segment.segment_id, speed, values["power_kw"], values["duration_h"],
                    values["energy_kwh"], model.energy_scope, model.model_id,
                    peak_power_kw=values["peak_power_kw"], assumptions=assumptions,
                    propulsion_energy_kwh=values["propulsion_kwh"],
                    auxiliary_energy_kwh=values["auxiliary_kwh"],
                    source=segment.source,
                    model_approval_ref=model.approval_ref,
                ))
    except (TypeError, ValueError, OverflowError) as exc:
        return ToolResponse("Tenergy", Status.INVALID_INPUT, reason=str(exc))
    response = ToolResponse("Tenergy", Status.OK,
                            payload={"candidate_results": [item.to_dict() for item in results]})
    checked = validate(response)
    if not checked.valid:
        return ToolResponse("Tenergy", Status.FAILED,
                            reason="Tenergy内部结果未通过公共接口校验。")
    return response
