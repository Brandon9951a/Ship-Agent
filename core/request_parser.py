"""Deterministic Chinese voyage-request parser for safety-relevant fields.

The parser intentionally handles a narrow, auditable grammar.  It never asks an
LLM to invent engineering values and never fills missing SOC, time, load or
route fields with defaults.  Free-form understanding can be added later as a
text-only aid, but the extracted values still pass the shared schema validator.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

from schemas.types import Status, VoyageRequest
from schemas.validate import validate


ROOT = Path(__file__).resolve().parents[1]
CHINA_TZ = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class ParseResult:
    request: VoyageRequest
    status: Status
    missing_fields: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "request": self.request.to_dict(),
            "status": self.status.value,
            "missing_fields": list(self.missing_fields),
            "questions": list(self.questions),
            "warnings": list(self.warnings),
        }


def load_aliases(path: Path | None = None) -> dict[str, str]:
    source = path or ROOT / "configs/aliases.yaml"
    data = json.loads(source.read_text(encoding="utf-8-sig"))
    aliases = data.get("aliases")
    if not isinstance(aliases, dict):
        raise ValueError("aliases configuration must contain an aliases object")
    result: dict[str, str] = {}
    for alias, record in aliases.items():
        if not isinstance(alias, str) or not isinstance(record, dict):
            raise ValueError("alias entries must map text to objects")
        canonical = record.get("canonical")
        if not isinstance(canonical, str) or not canonical.strip():
            raise ValueError("every alias needs a canonical place name")
        result[alias.strip()] = canonical.strip()
    return result


def _places(text: str, aliases: dict[str, str]) -> tuple[str | None, str | None]:
    alternatives = "|".join(re.escape(name) for name in sorted(aliases, key=len, reverse=True))
    route = re.search(
        rf"(?:从\s*)?(?P<origin>{alternatives})\s*(?:到|至|前往)\s*"
        rf"(?P<destination>{alternatives})",
        text,
    )
    if route is None:
        return None, None
    return aliases[route.group("origin")], aliases[route.group("destination")]


def _soc(text: str) -> tuple[float | None, str | None]:
    match = re.search(
        r"(?:初始\s*)?(?:SOC|soc|电量)\s*(?:为|是|=|:|：)?\s*"
        r"(?P<value>\d+(?:\.\d+)?)\s*(?P<percent>%|％)?",
        text,
    )
    if match is None:
        return None, None
    value = float(match.group("value"))
    if not math.isfinite(value):
        return None, "初始SOC必须是有限数值。"
    if not match.group("percent") and 1 < value < 10:
        return None, "无百分号且介于1和10之间的SOC含义不明确，请写成比例或百分数。"
    if match.group("percent") or value > 1:
        value /= 100
    if not 0 <= value <= 1:
        return None, "初始SOC必须在0%到100%之间。"
    return value, None


def _duration(text: str) -> tuple[float | None, str | None]:
    patterns = (
        r"(?:最长|最多|不超过|控制在)\s*(\d+(?:\.\d+)?)\s*(?:个)?小时",
        r"(\d+(?:\.\d+)?)\s*(?:个)?小时(?:内|以内)(?:到达|抵达)?",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            value = float(match.group(1))
            if value <= 0:
                return None, "最长耗时必须大于0小时。"
            return value, None
    return None, None


def _load_and_draft(text: str) -> tuple[str | None, float | None, str | None]:
    load = next((name for name in ("空载", "半载", "满载") if name in text), None)
    match = re.search(r"吃水\s*(?:为|是|=|:|：)?\s*(\d+(?:\.\d+)?)\s*(?:m|米)", text, re.I)
    if match is None:
        return load, None, None
    draft = float(match.group(1))
    if draft <= 0:
        return load, None, "吃水必须大于0米。"
    return load, draft, None


def _parse_datetime(raw: str) -> str:
    normalized = raw.strip().replace("：", ":")
    formats = (
        "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M",
        "%Y年%m月%d日%H:%M", "%Y年%m月%d日%H点%M分",
        "%Y年%m月%d日%H点",
    )
    for pattern in formats:
        try:
            return datetime.strptime(normalized, pattern).replace(tzinfo=CHINA_TZ).isoformat()
        except ValueError:
            pass
    raise ValueError("时间必须包含完整年月日和时分。")


def _datetimes(text: str) -> tuple[str | None, str | None, list[str]]:
    expression = (
        r"(?:20\d{2}-\d{1,2}-\d{1,2}[ T]\d{1,2}:\d{2}|"
        r"20\d{2}年\d{1,2}月\d{1,2}日\d{1,2}(?::\d{2}|：\d{2}|点(?:\d{1,2}分)?))"
    )
    departure = arrival = None
    warnings: list[str] = []
    for match in re.finditer(expression, text):
        context_before = text[max(0, match.start() - 8):match.start()]
        context_after = text[match.end():match.end() + 8]
        try:
            value = _parse_datetime(match.group(0))
        except ValueError as exc:
            warnings.append(str(exc))
            continue
        if re.search(r"出发|启航|开航", context_after):
            departure = value
        elif re.search(r"到达|抵达|截止|前到", context_after):
            arrival = value
        elif re.search(r"出发|启航|开航", context_before):
            departure = value
        elif re.search(r"到达|抵达|截止", context_before):
            arrival = value
        else:
            warnings.append(f"未判断时间“{match.group(0)}”是出发还是到达时间，未自动填入。")
    if re.search(r"今天|明天|后天|上午|下午|晚上", text) and not (departure or arrival):
        warnings.append("相对时间需要明确日期和24小时制时间，系统未自动推断。")
    return departure, arrival, warnings


def parse_voyage_request(text: str, *, aliases: dict[str, str] | None = None) -> ParseResult:
    if not isinstance(text, str) or not text.strip():
        request = VoyageRequest()
        return ParseResult(
            request, Status.INVALID_INPUT, questions=["请输入非空的航行任务。"]
        )
    aliases = aliases or load_aliases()
    origin, destination = _places(text, aliases)
    soc, soc_error = _soc(text)
    duration, duration_error = _duration(text)
    load, draft, draft_error = _load_and_draft(text)
    departure, arrival, time_warnings = _datetimes(text)
    errors = [item for item in (soc_error, duration_error, draft_error) if item]
    request = VoyageRequest(
        origin=origin,
        destination=destination,
        departure_at=departure,
        arrival_deadline=arrival,
        max_duration_h=duration,
        soc_initial=soc,
        load_state=load,
        draft_m=draft,
    )
    if errors:
        return ParseResult(request, Status.INVALID_INPUT, questions=errors, warnings=time_warnings)
    checked = validate(request)
    return ParseResult(
        request=request,
        status=checked.status,
        missing_fields=checked.missing_fields,
        questions=checked.questions,
        warnings=time_warnings,
    )
