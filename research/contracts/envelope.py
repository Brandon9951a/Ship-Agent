"""联调接口 v2 的公共基础记录：来源、工件引用、质量标记与稳定哈希。

字段名、单位与枚举直接对应 ``docs/接口/五工具联调接口_v2.md`` 第 1 节。
本模块不填任何业务默认值：未知一律为 ``None``，由调用方给出有来源的取值。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

SOURCE_KINDS = ("document", "user", "measurement", "assumption", "tool")
DATA_MODES = ("measured", "replay", "simulation")
# 质量标记只做“显式标注”，不改变数值。外推、缺测、冲突必须可见。
QUALITY_FLAGS = (
    "extrapolated",
    "interpolated",
    "resampled",
    "missing",
    "unpredicted",
    "conflict",
    "assumption",
    "simulation",
    "coarse_grid",
)

CONTRACT_SCHEMA_VERSION = "research-tools/2.0-draft"


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


class ContractModel:
    """统一的 JSON 序列化基类：``to_dict`` 输出可直接落盘/比对的纯量结构。"""

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return _plain(asdict(self))

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent,
                          sort_keys=False)


def _require_finite(value: float, name: str, *, minimum: float | None = None,
                    maximum: float | None = None, allow_none: bool = False) -> float | None:
    if value is None:
        if allow_none:
            return None
        raise TypeError(f"{name} 不能为 null")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} 必须是有限数值，不能是布尔或文本")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} 必须是有限数值")
    if minimum is not None and number < minimum:
        raise ValueError(f"{name}={number} 低于允许下限 {minimum}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{name}={number} 高于允许上限 {maximum}")
    return number


@dataclass(frozen=True)
class SourceRef(ContractModel):
    """每个采用值关联的来源；``kind`` 区分文档、用户、实测、假设与工具输出。"""

    source_id: str
    kind: Literal["document", "user", "measurement", "assumption", "tool"]
    locator: str | None = None
    confirmed: bool = False
    note: str | None = None
    mode: Literal["measured", "replay", "simulation"] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise ValueError("SourceRef.source_id 必须是非空字符串")
        if self.kind not in SOURCE_KINDS:
            raise ValueError(f"SourceRef.kind 只允许 {SOURCE_KINDS}，得到 {self.kind!r}")
        if self.mode is not None and self.mode not in DATA_MODES:
            raise ValueError(f"SourceRef.mode 只允许 {DATA_MODES}，得到 {self.mode!r}")

    @property
    def usable_as_evidence(self) -> bool:
        """假设类来源只能支撑研究情景，不能支撑实船结论。"""
        return self.kind != "assumption"


def require_source(source: SourceRef | None, name: str) -> SourceRef:
    """缺来源即拒绝相应检查；不使用默认来源代替。"""
    if source is None:
        raise ValueError(f"{name} 缺少来源（source_ref），不能用于正式结论")
    if not isinstance(source, SourceRef):
        raise TypeError(f"{name}.source_ref 必须是 SourceRef")
    return source


@dataclass(frozen=True)
class ArtifactRef(ContractModel):
    """大型时序以工件形式保存，返回摘要与引用，不用总计数冒充明细。"""

    artifact_id: str
    sha256: str
    row_count: int
    schema_version: str
    locator: str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        if not self.artifact_id.strip():
            raise ValueError("ArtifactRef.artifact_id 必须非空")
        if len(self.sha256) != 64 or any(c not in "0123456789abcdef" for c in self.sha256.lower()):
            raise ValueError("ArtifactRef.sha256 必须是 64 位十六进制")
        if not isinstance(self.row_count, int) or isinstance(self.row_count, bool) or self.row_count < 0:
            raise ValueError("ArtifactRef.row_count 必须是非负整数")


@dataclass(frozen=True)
class DataQualityFlag(ContractModel):
    """显式质量标记：区间、标记与说明，未知值为 null。"""

    flag: str
    start_offset_s: float | None = None
    duration_s: float | None = None
    segment_id: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.flag not in QUALITY_FLAGS:
            raise ValueError(f"未知质量标记 {self.flag!r}；允许 {QUALITY_FLAGS}")
        _require_finite(self.start_offset_s, "start_offset_s", minimum=0, allow_none=True)
        _require_finite(self.duration_s, "duration_s", minimum=0, allow_none=True)


def stable_hash(payload: Any) -> str:
    """输入哈希：对规范化 JSON 求 sha256，用于版本关联与缓存键。"""
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(_plain(payload), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def plan_offset_seconds(origin_at: str | None, instant: str | None) -> float | None:
    """把绝对时刻换算为自规划基准的秒偏移；基准缺失时返回 null 而不是猜 0。"""
    if origin_at is None or instant is None:
        return None
    try:
        origin = datetime.fromisoformat(origin_at.replace("Z", "+00:00"))
        moment = datetime.fromisoformat(instant.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("时刻必须是带时区的 ISO 8601") from exc
    if origin.tzinfo is None or moment.tzinfo is None:
        raise ValueError("时刻必须带时区；无绝对时刻时使用 null")
    return (moment - origin).total_seconds()


def iso_from_offset(origin_at: str | None, offset_s: float | None) -> str | None:
    """基准时刻未知时不生成日历时刻（接口 v2 第 1 节）。"""
    if origin_at is None or offset_s is None:
        return None
    origin = datetime.fromisoformat(origin_at.replace("Z", "+00:00"))
    if origin.tzinfo is None:
        raise ValueError("planning_origin_at 必须带时区")
    from datetime import timedelta

    return (origin + timedelta(seconds=float(offset_s))).isoformat()


@dataclass(frozen=True)
class NamedAssumption(ContractModel):
    """具名假设：研究情景必须写明假设 ID 与取值，不用未知值冒充零。"""

    assumption_id: str
    field_ref: str
    value: float | str | None
    unit: str | None = None
    detail: str | None = None
    source_ref: SourceRef | None = None
