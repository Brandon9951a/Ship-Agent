"""Authoritative tool-value report with optional qualitative LLM wording."""

from __future__ import annotations

from typing import Any

from core.intent_explainer import contains_engineering_value
from core.llm_layer import LLMClient
from schemas.types import VoyagePlan
from schemas.validate import validate


def _template_explanation(plan: VoyagePlan) -> str:
    if plan.management and plan.management.warnings:
        return "建议先查看能量管理提示，并在出发前确认电量和任务安排。"
    return "建议按推荐航速执行，并在航行中持续关注电量变化和现场通航条件。"


def _operator_facing(text: str) -> bool:
    """Reject internal validation language from the crew-facing explanation."""
    internal_terms = (
        "软件演示", "仿真", "结论边界", "语义边界", "工程数值", "工具计算",
        "模型输出", "实船验证", "运营批准", "数值锁定", "演示", "边界",
        "验证", "批准", "审批", "锁定", "工具", "模型",
    )
    return not contains_engineering_value(text) and not any(
        term in text for term in internal_terms
    )


def _qualitative_explanation(plan: VoyagePlan, client: LLMClient | None) -> tuple[str, str]:
    fallback = _template_explanation(plan)
    if client is None:
        return fallback, "template_fallback"
    needs_attention = bool(plan.management and plan.management.warnings)
    prompt = (
        f"航行方案已经生成，当前{'存在电量关注事项' if needs_attention else '没有额外电量提示'}。"
        "请用一句中文给船员明确、自然的执行提示。不要复述起终点，不要描述系统内部验证过程，"
        "不得出现任何数字、单位或新增工程参数。"
    )
    result = client.complete_text(
        prompt,
        system_text=(
            "你是船舶驾驶舱助手，只输出面向船员的简洁动作建议。不得出现阿拉伯数字，"
            "不得增加速度、时间、功率、能耗、SOC、距离或阈值。禁止使用软件演示、"
            "仿真、结论边界、语义边界、工程数值、工具、模型、验证、批准、锁定等内部措辞。"
        ),
        max_tokens=128,
    )
    if result.status != "ok" or not result.text or not _operator_facing(result.text):
        return fallback, "template_fallback"
    return result.text.strip(), "llm_qualitative"


def _management_advice(plan: VoyagePlan) -> list[str]:
    """Build operator actions from deterministic management and power results."""
    management = plan.management
    optimization = plan.optimization
    data = plan.data
    assert management is not None and optimization is not None and data is not None
    final_soc = float(management.soc_final)
    alarm = float(data.vessel.soc_alarm or 0.35)
    threshold_record = data.parameters.get("parallel_enter_propulsion_kw")
    threshold = threshold_record.value if threshold_record is not None else None
    peak_power = max(
        (item.peak_power_kw or item.power_kw for item in optimization.energy_results),
        default=0.0,
    )
    advice: list[str] = []
    if isinstance(threshold, (int, float)) and peak_power > threshold:
        advice.append(
            "推进负荷达到并联辅助条件：建议两组电池协同供电，"
            "通过高负荷航段后恢复常规分工。"
        )
    elif final_soc < alarm:
        advice.append(
            "到港电量余度偏低：电池组一保持推进供电，电池组二优先保障"
            "必要日常负载并保留辅助推进能力。"
        )
    else:
        advice.append(
            "本航次电量余度充足：电池组一承担推进，电池组二保障日常负载；"
            "出现瞬时高负荷时再启用并联辅助。"
        )
    if final_soc < alarm:
        advice.append(
            f"预计到港电量 {final_soc:.1%}；建议出发前补能或缩短航程，"
            "并提高航行中的电量检查频次。"
        )
    else:
        advice.append(
            f"预计到港电量 {final_soc:.1%}；当前任务无需途中补能，"
            "航行中按计划检查电量变化。"
        )
    return advice


def build_report(plan: VoyagePlan, client: LLMClient | None = None) -> dict[str, Any]:
    """Build a unit/source-bearing report; no numeric value originates in the LLM."""
    checked = validate(plan)
    if not checked.valid:
        raise ValueError("Only a contract-valid successful VoyagePlan can be reported")
    optimization = plan.optimization
    management = plan.management
    assert optimization is not None and management is not None
    energy_by_id = {item.segment_id: item for item in optimization.energy_results}
    soc_by_id = {item.segment_id: item.soc for item in management.soc_trajectory}
    rows = []
    for segment in plan.segments:
        energy = energy_by_id[segment.segment_id]
        rows.append({
            "segment_id": segment.segment_id,
            "origin": segment.origin,
            "destination": segment.destination,
            "distance": {"value": segment.distance_km, "unit": "km"},
            "speed": {"value": energy.speed_kmh, "unit": "km/h"},
            "duration": {"value": energy.duration_h, "unit": "h"},
            "propulsion_energy": {
                "value": energy.propulsion_energy_kwh,
                "unit": "kWh",
            },
            "soc_end": {"value": soc_by_id[segment.segment_id], "unit": "fraction"},
            "distance_source": segment.source.to_dict() if segment.source else None,
            "model_id": energy.model_id,
            "model_approval_ref": energy.model_approval_ref,
        })
    explanation, explanation_mode = _qualitative_explanation(plan, client)
    return {
        "status": "ok",
        "scope": "synthetic_demo",
        "explanation": explanation,
        "explanation_mode": explanation_mode,
        "segments": rows,
        "summary": {
            "total_distance": {
                "value": sum(item.distance_km for item in plan.segments), "unit": "km"
            },
            "total_duration": {"value": optimization.total_duration_h, "unit": "h"},
            "eta": {"value": optimization.eta, "unit": "ISO 8601"},
            "propulsion_energy": {
                "value": optimization.total_energy_kwh, "unit": "kWh"
            },
            "auxiliary_energy": {
                "value": management.auxiliary_energy_kwh, "unit": "kWh"
            },
            "required_energy": {
                "value": management.required_energy_kwh, "unit": "kWh"
            },
            "soc_initial": {"value": management.soc_initial, "unit": "fraction"},
            "soc_final": {"value": management.soc_final, "unit": "fraction"},
            "soc_planning_min": {"value": management.soc_min, "unit": "fraction"},
            "charge_required": {
                "value": management.charge_required_kwh, "unit": "kWh"
            },
        },
        "checks": [item.to_dict() for item in optimization.checks + management.checks],
        "warnings": list(management.warnings),
        "management_advice": _management_advice(plan),
        "assumptions": list(dict.fromkeys(
            plan.assumptions + optimization.assumptions + management.assumptions
        )),
        "sources": {
            "capacity": plan.data.vessel.sources["capacity_kwh"].to_dict(),
            "power_limit": plan.data.vessel.sources["max_power_kw"].to_dict(),
            "soc_min": plan.data.vessel.sources["soc_min"].to_dict(),
        },
    }
