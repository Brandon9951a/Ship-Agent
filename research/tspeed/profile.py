"""DE 搜索期的轻量轨迹剖面。

DE 每代要评估数十个候选，若每次都把上万个区间实例化为 v2 对象，构造与哈希
开销会远超数值计算本身。``TrajectoryProfile`` 只保存判分所需的标量列，
在**核验与落盘**路径上仍使用完整 v2 工件（``TrajectoryPoint`` / ``PowerPoint``）。
"""

from __future__ import annotations

from dataclasses import dataclass

from research.tspeed.trajectory import ExpandedTrajectory


@dataclass(frozen=True)
class TrajectoryProfile:
    """按区间顺序保存的标量列；与 ``ExpandedTrajectory`` 一一对应。"""

    segment_ids: tuple[str, ...]
    phases: tuple[str, ...]
    starts: tuple[float, ...]
    durations: tuple[float, ...]
    speeds: tuple[float | None, ...]
    kinds: tuple[str, ...] = ()
    targets: tuple[float | None, ...] = ()

    def __post_init__(self) -> None:
        length = len(self.segment_ids)
        for name in ("phases", "starts", "durations", "speeds"):
            if len(getattr(self, name)) != length:
                raise ValueError(f"剖面列 {name} 长度不一致")
        for name in ("kinds", "targets"):
            column = getattr(self, name)
            if column and len(column) != length:
                raise ValueError(f"剖面列 {name} 长度不一致")
        if any(duration <= 0 for duration in self.durations):
            raise ValueError("剖面区间的 duration_s 必须大于 0")

    def __len__(self) -> int:
        return len(self.segment_ids)

    @property
    def interval_count(self) -> int:
        return len(self.segment_ids)

    @property
    def total_duration_s(self) -> float:
        return sum(self.durations)

    def segment_order(self) -> tuple[str, ...]:
        ordered: list[str] = []
        for segment_id in self.segment_ids:
            if segment_id not in ordered:
                ordered.append(segment_id)
        return tuple(ordered)

    @classmethod
    def from_expansion(cls, expanded: ExpandedTrajectory) -> TrajectoryProfile:
        """由展开结果构建剖面；等待阶段一并纳入，与核验路径保持同一阶段集合。"""
        ordered = expanded.ordered_intervals()
        if not ordered:
            raise ValueError("展开结果为空，无法构建轨迹剖面")
        targets_by_segment = {fact.segment_id: fact.cruise_speed_kmh
                              for fact in expanded.segment_facts}
        return cls(
            segment_ids=tuple(segment_id for segment_id, _ in ordered),
            phases=tuple(interval.phase for _, interval in ordered),
            starts=tuple(interval.start_offset_s for _, interval in ordered),
            durations=tuple(interval.duration_s for _, interval in ordered),
            speeds=tuple(interval.planned_speed_kmh for _, interval in ordered),
            kinds=tuple(interval.kind for _, interval in ordered),
            targets=tuple(targets_by_segment.get(segment_id)
                          for segment_id, _ in ordered))
