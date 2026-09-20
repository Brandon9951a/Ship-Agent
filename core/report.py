"""Authoritative tool-value report with optional qualitative LLM wording."""

from __future__ import annotations

from typing import Any

from core.intent_explainer import contains_engineering_value
from core.llm_layer import LLMClient
from schemas.types import VoyagePlan
from schemas.validate import validate


def _template_explanation(plan: VoyagePlan) -> str:
    if plan.management and plan.management.warnings:
        return "方案仅在软件演示约束下成立，并存在需要操作员关注的风险提示。"
    return "方案仅在软件演示约束下成立；工程数值以工具计算表为准。"


def _qualitative_explanation(plan: VoyagePlan, client: LLMClient | None) -> tuple[str, str]:
    fallback = _template_explanation(plan)
    if client is None:
        return fallback, "template_fallback"
    warning_labels = plan.management.warnings if plan.management else []
    prompt = (
        f"任务从{plan.request.origin}到{plan.request.destination}。"
        f"状态为软件演示可行。风险标签：{'；'.join(warning_labels) or '无额外阈值告警'}。"
        "请用一句中文解释结果边界。不得出现任何阿拉伯数字，不得增加工程参数或承诺实船安全。"
    )
    result = client.complete_text(
        prompt,
        system_text=(
            "你只负责定性解释。工程数字由工具锁定。不得出现阿拉伯数字，"
            "不得增加速度、时间、功率、能耗、SOC、距离或阈值。"
        ),
        max_tokens=128,
    )
    if result.status != "ok" or not result.text or contains_engineering_value(result.text):
        return fallback, "template_fallback"
    return result.text.strip(), "llm_qualitative"


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
        "assumptions": list(dict.fromkeys(
            plan.assumptions + optimization.assumptions + management.assumptions
        )),
        "sources": {
            "capacity": plan.data.vessel.sources["capacity_kwh"].to_dict(),
            "power_limit": plan.data.vessel.sources["max_power_kw"].to_dict(),
            "soc_min": plan.data.vessel.sources["soc_min"].to_dict(),
        },
    }
