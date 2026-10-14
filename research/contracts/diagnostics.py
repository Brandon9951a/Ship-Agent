"""v2 诊断项：每条至少包含 stage/code/区间或航段/字段/实测值/限值/单位/来源/可否重试。"""

from __future__ import annotations

from dataclasses import dataclass

from .envelope import ContractModel, SourceRef, _require_finite


@dataclass(frozen=True)
class Diagnostic(ContractModel):
    """接口 v2 第 3 节：未知值为 null，不用空串或 0 代替。"""

    stage: str
    code: str
    detail: str | None = None
    time_interval_s: tuple[float, float] | None = None
    segment_id: str | None = None
    field_ref: str | None = None
    observed: float | str | None = None
    limit: float | str | None = None
    unit: str | None = None
    source_refs: tuple[SourceRef, ...] = ()
    retryable: bool | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.stage, str) or not self.stage.strip():
            raise ValueError("Diagnostic.stage 必须非空")
        if not isinstance(self.code, str) or not self.code.strip():
            raise ValueError("Diagnostic.code 必须非空")
        if self.time_interval_s is not None:
            if len(self.time_interval_s) != 2:
                raise ValueError("time_interval_s 必须是 (start, end)")
            start, end = (_require_finite(item, "time_interval_s", minimum=0)
                          for item in self.time_interval_s)
            if end < start:
                raise ValueError("time_interval_s 的结束不得早于开始")
        object.__setattr__(self, "source_refs", tuple(self.source_refs))

    @property
    def interval_or_segment(self) -> str | None:
        """人类可读定位串，便于报告与排查；两者都未知时为 null。"""
        if self.time_interval_s is not None:
            return f"[{self.time_interval_s[0]:.1f}, {self.time_interval_s[1]:.1f})s"
        return self.segment_id
