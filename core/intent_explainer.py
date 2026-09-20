"""Qualitative task-understanding confirmation with deterministic authority."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.llm_layer import LLMClient


_FIELD_LABELS = {
    "origin": "起点",
    "destination": "终点",
    "departure_at": "出发时间",
    "arrival_deadline": "到达约束",
    "max_duration_h": "最长航时",
    "soc_initial": "初始电量条件",
    "load_state": "载况",
    "draft_m": "吃水",
}


def contains_engineering_value(text: str) -> bool:
    """Reject model prose that could introduce or restate engineering values."""
    import re

    return bool(re.search(
        r"\d|[零〇一二两三四五六七八九十百千万亿点]|%|％|"
        r"kW|kWh|km|公里|千瓦|千瓦时|小时|节|SOC|荷电状态",
        text,
        re.IGNORECASE,
    ))


def build_task_understanding(
    request: Mapping[str, Any], client: LLMClient | None = None,
) -> dict[str, Any]:
    """Describe parsed field coverage; the LLM cannot change parsed values."""
    recognised = [label for key, label in _FIELD_LABELS.items() if request.get(key) is not None]
    labels = "、".join(recognised) if recognised else "尚未识别到完整任务字段"
    fallback = f"确定性解析器已识别：{labels}；缺失项将由校验器追问。"
    mode = "template_fallback"
    text = fallback
    if client is not None:
        result = client.complete_text(
            "确定性解析器已完成字段提取。请用一句中文确认任务理解边界；"
            "不得复述或增加任何数值、单位、地点、时间或工程结论。",
            system_text=(
                "你只确认语义理解边界，不提取参数，不修改结构化请求。"
                "不得输出阿拉伯数字、中文数字、工程单位或安全结论。"
            ),
            max_tokens=96,
        )
        if result.status == "ok" and result.text and not contains_engineering_value(result.text):
            text = result.text.strip()
            mode = "llm_qualitative"
    return {
        "status": "confirmed",
        "mode": mode,
        "scope": "semantic_confirmation_only",
        "text": text,
        "recognised_fields": [
            key for key in _FIELD_LABELS if request.get(key) is not None
        ],
        "authoritative_source": "deterministic_parser_and_schema_validation",
    }
