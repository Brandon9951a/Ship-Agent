"""把 DE 决策向量（各可优化航段目标航速）展开为可执行的连续轨迹。

接口 v2 第 1 节：每个样本表示 ``[start_offset_s, start_offset_s + duration_s)``，
``duration_s > 0``。速度不能瞬时跳变，跨段过渡必须展开为满足加减速上限的斜坡，
否则该候选不是可执行方案。

展开分两步：
1. ``plan_segment_phases`` 用部分斜坡/完整斜坡求解析的物理阶段（加速、巡航、减速）；
2. ``sample_phases`` 把物理阶段采样到预测时间网格上，使每个预测区间的速度近似恒定，
   从而让"预测区间"与"积分区间"一致（接口 v2 第 4 节）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from research.contracts import Phase, SegmentConstraint, TimeGridSpec

TOLERANCE = 1e-9
SPEED_TOLERANCE_KMH = 1e-9


@dataclass(frozen=True)
class PhaseSpan:
    """展开后的一个物理阶段；加速/减速阶段为恒加速斜坡。"""

    kind: str  # "accel" | "cruise" | "decel"
    duration_s: float
    entry_speed_kmh: float
    exit_speed_kmh: float

    @property
    def mean_speed_kmh(self) -> float:
        return (self.entry_speed_kmh + self.exit_speed_kmh) / 2.0

    @property
    def distance_km(self) -> float:
        return self.mean_speed_kmh * self.duration_s / 3600.0

    @property
    def acceleration_mps2(self) -> float:
        """带符号加速度；正为加速、负为减速（速度在减小）。"""
        if self.duration_s <= 0:
            return 0.0
        return (self.exit_speed_kmh - self.entry_speed_kmh) / 3.6 / self.duration_s

    @property
    def magnitude_mps2(self) -> float:
        return abs(self.acceleration_mps2)

    def speed_at_fraction(self, fraction: float) -> float:
        if self.kind == "cruise":
            return self.entry_speed_kmh
        return self.entry_speed_kmh + (self.exit_speed_kmh - self.entry_speed_kmh) * fraction


@dataclass(frozen=True)
class SampledInterval:
    """采样到预测网格上的区间；速度在该区间内近似恒定。"""

    start_offset_s: float
    duration_s: float
    planned_speed_kmh: float
    kind: str
    phase: str
    acceleration_mps2: float

    @property
    def end_offset_s(self) -> float:
        return self.start_offset_s + self.duration_s


@dataclass(frozen=True)
class Violation:
    """展开阶段发现的问题；不进入目标函数，只进入诊断与可行性判定。"""

    code: str
    segment_id: str | None
    detail: str
    observed: float | None = None
    limit: float | None = None
    unit: str | None = None


@dataclass(frozen=True)
class SegmentFact:
    """展开后的事实值，用于距离/时间一致性复核。"""

    segment_id: str
    requested_distance_km: float
    phases: tuple[PhaseSpan, ...]
    sampled: tuple[SampledInterval, ...]
    entry_speed_kmh: float
    exit_speed_kmh: float
    cruise_speed_kmh: float | None
    waiting_duration_s: float
    waiting_rule: str | None
    start_offset_s: float
    distance_tolerance_km: float

    @property
    def sailing_duration_s(self) -> float:
        return sum(phase.duration_s for phase in self.phases)

    @property
    def sailing_distance_km(self) -> float:
        return sum(phase.distance_km for phase in self.phases)

    @property
    def distance_residual_km(self) -> float:
        return self.sailing_distance_km - self.requested_distance_km

    @property
    def duration_s(self) -> float:
        return self.sailing_duration_s + self.waiting_duration_s

    @property
    def end_offset_s(self) -> float:
        return self.start_offset_s + self.duration_s

    @property
    def distance_consistent(self) -> bool:
        return abs(self.distance_residual_km) <= self.distance_tolerance_km


@dataclass(frozen=True)
class ExpandedTrajectory:
    """展开结果：可提交 Tenergy 的区间、事实值与违规列表。"""

    speed_vector: tuple[float, ...]
    optimizable_segment_ids: tuple[str, ...]
    segment_facts: tuple[SegmentFact, ...]
    violations: tuple[Violation, ...]
    prediction_step_s: float

    @property
    def intervals(self) -> tuple[SampledInterval, ...]:
        collected: list[SampledInterval] = []
        for fact in self.segment_facts:
            collected.extend(fact.sampled)
        return tuple(collected)

    @property
    def sailing_duration_s(self) -> float:
        return sum(fact.sailing_duration_s for fact in self.segment_facts)

    @property
    def waiting_duration_s(self) -> float:
        return sum(fact.waiting_duration_s for fact in self.segment_facts)

    @property
    def total_duration_s(self) -> float:
        return self.sailing_duration_s + self.waiting_duration_s

    @property
    def declared_distance_km(self) -> float:
        return sum(fact.sailing_distance_km for fact in self.segment_facts)

    @property
    def requested_distance_km(self) -> float:
        return sum(fact.requested_distance_km for fact in self.segment_facts)

    @property
    def distance_residual_km(self) -> float:
        return self.declared_distance_km - self.requested_distance_km

    @property
    def distance_consistent(self) -> bool:
        return all(fact.distance_consistent for fact in self.segment_facts)

    @property
    def violation_codes(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.code for item in self.violations))

    def segment_fact(self, segment_id: str) -> SegmentFact:
        for fact in self.segment_facts:
            if fact.segment_id == segment_id:
                return fact
        raise KeyError(segment_id)

    def ordered_intervals(self) -> list[tuple[str, SampledInterval]]:
        """按时间顺序返回 ``(segment_id, 区间)``，等待阶段计为其所属航段的区间。

        这是"轨迹 → 区间"的唯一来源：判分路径（轻量剖面）与核验路径（v2 工件）
        都从这里生成，避免两条路径包含不同的阶段集合。
        """
        collected: list[tuple[str, SampledInterval]] = []
        for fact in self.segment_facts:
            for interval in fact.sampled:
                collected.append((fact.segment_id, interval))
            if fact.waiting_duration_s > 0:
                collected.append((fact.segment_id, SampledInterval(
                    start_offset_s=fact.sailing_duration_s + fact.start_offset_s,
                    duration_s=fact.waiting_duration_s,
                    planned_speed_kmh=0.0, kind="waiting",
                    phase=Phase.WAITING.value, acceleration_mps2=0.0)))
        return collected

    def to_trajectory_points(self, *, environment_ref: str | None = None):
        """输出 v2 轨迹区间；等待阶段速度写 0，不做未知占位。

        ``planned_speed_kmh`` 是该区间的平均航速（斜坡上不等于目标航速），
        ``target_speed_kmh`` 是该航段的决策变量。
        """
        from research.contracts import TrajectoryPoint

        points: list[TrajectoryPoint] = []
        for fact in self.segment_facts:
            target = fact.cruise_speed_kmh
            for interval in fact.sampled:
                points.append(TrajectoryPoint(
                    segment_id=fact.segment_id,
                    phase=interval.phase,
                    start_offset_s=round(interval.start_offset_s, 9),
                    duration_s=round(interval.duration_s, 9),
                    planned_speed_kmh=round(interval.planned_speed_kmh, 9),
                    environment_ref=environment_ref,
                    target_speed_kmh=(round(target, 9) if target is not None else None),
                    interval_kind=interval.kind))
            if fact.waiting_duration_s > 0:
                points.append(TrajectoryPoint(
                    segment_id=fact.segment_id,
                    phase=Phase.WAITING.value,
                    start_offset_s=round(fact.sailing_duration_s + fact.start_offset_s, 9),
                    duration_s=round(fact.waiting_duration_s, 9),
                    planned_speed_kmh=0.0,
                    environment_ref=environment_ref,
                    interval_kind="waiting"))
        return tuple(points)

    def audit_acceleration(self, grid: TimeGridSpec) -> list[Violation]:
        """独立复核每个区间的平均加速度是否越过采用上限。

        复核按区间**自身**的速度变化方向选择上限（加速用加速上限、减速用减速
        上限），并允许相对浮点容差，避免把 0.20000000000000004 判成越界。
        """
        issues: list[Violation] = []
        for fact in self.segment_facts:
            for interval in fact.sampled:
                if interval.kind == "cruise" or interval.duration_s <= 0:
                    continue
                # 用相邻切片的速度差判断方向，不依赖存储的符号
                if interval.kind == "accel":
                    limit = grid.max_acceleration_mps2
                elif interval.kind == "decel":
                    limit = grid.max_deceleration_mps2
                else:
                    limit = max(grid.max_acceleration_mps2,
                                grid.max_deceleration_mps2)
                rate = abs(interval.acceleration_mps2)
                if rate > max(limit * (1.0 + 1e-9), limit + 1e-9):
                    issues.append(Violation(
                        code="acceleration_exceeded", segment_id=fact.segment_id,
                        detail=("区间平均加速度超过采用上限"
                                if interval.kind == "accel" else
                                "区间平均减速度超过采用上限"),
                        observed=rate, limit=limit, unit="m/s^2"))
        return issues

    def audit_continuity(self) -> list[Violation]:
        """复核时间轴无重叠、无未解释间隙。"""
        issues: list[Violation] = []
        expected = 0.0
        for fact in self.segment_facts:
            if abs(fact.start_offset_s - expected) > 1e-6:
                issues.append(Violation(
                    code="time_discontinuity", segment_id=fact.segment_id,
                    detail="航段时间轴与上一航段不连续",
                    observed=fact.start_offset_s, limit=expected, unit="s"))
            expected = fact.end_offset_s
        return issues


def _ramp_time_s(from_kmh: float, to_kmh: float, rate_mps2: float) -> float:
    if rate_mps2 <= 0:
        raise ValueError("加减速上限必须为正")
    return abs(to_kmh - from_kmh) / 3.6 / rate_mps2


def _peak_speed_for_distance(distance_km: float, entry_kmh: float, exit_kmh: float,
                             accel_mps2: float, decel_mps2: float) -> float:
    """求恰好覆盖给定距离的峰值航速（加速→减速，无巡航段）。

    ``d(v) = (v²-entry²)/(2·a) + (v²-exit²)/(2·d)``（米制换算后）对 v 单调递增，
    取正根即最短可行峰值；无正根时回落到较大的端点速度。
    """
    scale = 1000.0 / 3600.0  # km/h → m/s
    entry_mps = entry_kmh * scale
    exit_mps = exit_kmh * scale
    distance_m = distance_km * 1000.0
    a = accel_mps2
    d = decel_mps2
    quadratic = 1.0 / (2.0 * a) + 1.0 / (2.0 * d)
    constant = -(entry_mps ** 2) / (2.0 * a) - (exit_mps ** 2) / (2.0 * d) - distance_m
    discriminant = -4.0 * quadratic * constant
    if discriminant < 0:
        return max(entry_kmh, exit_kmh)
    peak_mps = math.sqrt(discriminant) / (2.0 * quadratic)
    return peak_mps / scale


def segment_limits(segment: SegmentConstraint, grid: TimeGridSpec
                   ) -> tuple[float, float]:
    """该航段在整段轨迹内允许出现的 (最低, 最高) 航速。

    只使用全局范围与该航段的限速下限/上限；**不含固定航速**——固定航速是设计
    巡航速度，过渡斜坡可以高于或低于它（进出港必然如此）。
    """
    low = grid.min_speed_kmh
    high = grid.max_speed_kmh
    if segment.min_speed_kmh is not None:
        low = max(low, segment.min_speed_kmh)
    if segment.max_speed_kmh is not None:
        high = min(high, segment.max_speed_kmh)
    return low, high


def fixed_speed_floor(segment: SegmentConstraint, grid: TimeGridSpec) -> float:
    """该航段减速斜坡的目标下限：限速下限与固定航速取较严者。

    固定航速不是可以突破限速的许可：减速斜坡不得把航速压到本航段限速下限之下。
    """
    low, _ = segment_limits(segment, grid)
    if segment.fixed_speed_kmh is not None:
        low = max(low, segment.fixed_speed_kmh)
    return low


def speed_ceiling(segment: SegmentConstraint, grid: TimeGridSpec) -> float:
    """该航段的可用速度上限（只由限速上限决定，固定航速除外）。"""
    _, high = segment_limits(segment, grid)
    return high


def segment_handoff_floor(segment: SegmentConstraint, next_segment: SegmentConstraint | None,
                          grid: TimeGridSpec) -> float:
    """本段与下一段之间的交接速度下限 = 两侧减速目标下限的较大者。

    交接速度既要满足本段（不得低于本段的限速下限/固定航速），也要满足下一段
    （不得低于下一段限速下限）。取较大者即可同时满足两侧，且不会展开出低于
    任一段限速下限的"通过速度"。
    """
    floor = fixed_speed_floor(segment, grid)
    if next_segment is not None:
        floor = max(floor, fixed_speed_floor(next_segment, grid))
    return floor


def plan_segment_phases(segment: SegmentConstraint, *, cruise_speed_kmh: float,
                        entry_speed_kmh: float, exit_speed_kmh: float,
                        grid: TimeGridSpec, fixed: bool,
                        distance_tolerance_km: float | None = None,
                        clamp_exit_to_floor: bool = True,
                        exit_floor: float | None = None
                        ) -> tuple[list[PhaseSpan], list[Violation]]:
    """规划单个航段的加速→巡航→减速阶段，距离按实际速度积分。

    ``clamp_exit_to_floor=False`` 用于终点停车：靠泊必然把航速降到 0，
    此时不应再被航段限速下限夹住。``exit_floor`` 为交接速度下限（默认为本段
    减速目标下限），由调用方按相邻两段给出。
    """
    tolerance = (grid.distance_tolerance_km if distance_tolerance_km is None
                 else distance_tolerance_km)
    violations: list[Violation] = []
    low, high = segment_limits(segment, grid)
    if low > high + TOLERANCE:
        raise ValueError(f"{segment.segment_id}: 航段限速与全局速度范围无交集")
    floor = fixed_speed_floor(segment, grid)
    decel_floor = floor if exit_floor is None else max(floor, exit_floor)
    # 入段斜坡必须从上一段的真实出口速度起步，该速度可能低于本段限速下限
    # （前一段限速更宽）：这是物理过程，不是违规。真正不能突破的是本段限速上限。
    if entry_speed_kmh > high + TOLERANCE:
        violations.append(Violation(
            code="entry_speed_above_limit", segment_id=segment.segment_id,
            detail="入段速度高于该航段限速上限，本段内无法满足限速",
            observed=entry_speed_kmh, limit=high, unit="km/h"))

    target = min(max(cruise_speed_kmh, floor), high) if not fixed else floor
    if cruise_speed_kmh > high + TOLERANCE:
        violations.append(Violation(
            code="target_speed_above_limit", segment_id=segment.segment_id,
            detail="目标航速超过该航段限速上限（不静默夹住，只如实报告）",
            observed=cruise_speed_kmh, limit=high, unit="km/h"))
    if entry_speed_kmh > high + TOLERANCE:
        violations.append(Violation(
            code="entry_speed_above_limit", segment_id=segment.segment_id,
            detail="入段速度高于该航段限速上限，本段内无法满足限速",
            observed=entry_speed_kmh, limit=high, unit="km/h"))
    entry = min(max(entry_speed_kmh, 0.0), high)
    # 减速斜坡的目标速度不得低于本航段可用下限，否则会展开出低于限速下限的
    # "通过速度"。终点停车时例外（靠泊必然降到 0）。
    exit_speed = (min(max(exit_speed_kmh, decel_floor), high) if clamp_exit_to_floor
                  else max(min(exit_speed_kmh, high), 0.0))
    if entry < 0 or exit_speed < 0:
        raise ValueError("航速不得为负")

    max_accel = grid.max_acceleration_mps2
    max_decel = grid.max_deceleration_mps2

    def ramp_phases(peak: float) -> list[PhaseSpan]:
        return [_ramp("ramp_in", entry, peak), PhaseSpan("cruise", 0.0, peak, peak),
                _ramp("ramp_out", peak, exit_speed)]

    def _ramp(kind: str, from_kmh: float, to_kmh: float) -> PhaseSpan:
        """按实际速度变化方向生成斜坡阶段，保证"加速/减速"名称与符号一致。"""
        duration = _ramp_time_s(from_kmh, to_kmh,
                               max_accel if to_kmh >= from_kmh else max_decel)
        return PhaseSpan(kind, duration, from_kmh, to_kmh)

    accel_time = _ramp_time_s(entry, target,
                              max_accel if target >= entry else max_decel)
    decel_time = _ramp_time_s(target, exit_speed,
                              max_accel if exit_speed >= target else max_decel)
    accel_distance = (entry + target) / 2.0 * accel_time / 3600.0
    decel_distance = (target + exit_speed) / 2.0 * decel_time / 3600.0
    cruise_distance = segment.distance_km - accel_distance - decel_distance

    if cruise_distance < -tolerance:
        # 距离不足以完成完整斜坡：求恰好覆盖该距离的峰值航速（一次加速 + 一次减速）
        peak = _peak_speed_for_distance(segment.distance_km, entry, exit_speed,
                                        max_accel, max_decel)
        capped = min(max(peak, max(entry, exit_speed)), high)
        phases = [phase for phase in ramp_phases(capped)
                  if phase.duration_s > TOLERANCE]
        covered = sum(phase.distance_km for phase in phases)
        if peak < low - TOLERANCE:
            violations.append(Violation(
                code="distance_too_short_for_speed_limit", segment_id=segment.segment_id,
                detail="按加减速上限展开时，该航段距离无法达到限速下限",
                observed=peak, limit=low, unit="km/h"))
        if peak > high + TOLERANCE:
            violations.append(Violation(
                code="speed_limit_exceeded_for_distance", segment_id=segment.segment_id,
                detail="覆盖该航段距离所需峰值航速超过限速上限",
                observed=peak, limit=high, unit="km/h"))
        residual = segment.distance_km - covered
        if abs(residual) > tolerance:
            violations.append(Violation(
                code="distance_unreachable_with_speed_limits",
                segment_id=segment.segment_id,
                detail="在限速与加减速上限下，该航段距离无法精确覆盖",
                observed=residual, limit=tolerance, unit="km"))
        return phases, violations

    cruise_time = cruise_distance / target * 3600.0 if target > 0 else 0.0
    phases = [_ramp("accel" if target >= entry else "decel", entry, target),
              PhaseSpan("cruise", cruise_time, target, target),
              _ramp("decel" if exit_speed <= target else "accel", target, exit_speed)]
    return [phase for phase in phases if phase.duration_s > TOLERANCE], violations


def sample_phases(phases: list[PhaseSpan], *, start_offset_s: float,
                  step_s: float, max_step_s: float) -> list[SampledInterval]:
    """把物理阶段采样到预测网格：每个区间速度近似恒定，首尾严格对齐。

    采样步长取 ``min(step_s, max_step_s)``。斜坡阶段按片数等分，残余时间自动
    并入最后一片，因此区间总时长与阶段总时长严格相等：既不产生未解释间隙，
    也不会让末尾速度偏离阶段末速度。相邻片的速度均值严格衔接，避免了
    "逐片累加浮点误差导致的距离偏差"。
    """
    if step_s <= 0 or max_step_s <= 0:
        raise ValueError("预测步长必须为正")
    step = min(step_s, max_step_s)
    intervals: list[SampledInterval] = []
    offset = start_offset_s
    for phase in phases:
        duration = phase.duration_s
        if duration <= TOLERANCE:
            continue
        if phase.kind == "cruise":
            remaining = duration
            while remaining > TOLERANCE:
                take = min(step, remaining)
                intervals.append(SampledInterval(
                    start_offset_s=offset, duration_s=take,
                    planned_speed_kmh=phase.entry_speed_kmh,
                    kind="cruise", phase=Phase.SAILING.value, acceleration_mps2=0.0))
                offset += take
                remaining -= take
            continue
        # 斜坡按步长切片；每片时长 = step，最后一片吸收残余时间，
        # 因此每片加速度仍等于阶段加速度（不会因摊薄残余而被放大）。
        count = max(1, math.floor(duration / step + TOLERANCE))
        slice_duration = min(step, duration / count)
        rate = phase.acceleration_mps2
        for index in range(count):
            start_fraction = (index * slice_duration) / duration
            end_fraction = min(((index + 1) * slice_duration) / duration, 1.0)
            from_speed = phase.speed_at_fraction(start_fraction)
            to_speed = phase.speed_at_fraction(end_fraction)
            intervals.append(SampledInterval(
                start_offset_s=offset,
                duration_s=(duration - slice_duration * (count - 1)
                            if index == count - 1 else slice_duration),
                planned_speed_kmh=(from_speed + to_speed) / 2.0,
                kind=phase.kind, phase=Phase.SAILING.value,
                acceleration_mps2=rate))
            offset += intervals[-1].duration_s
    return intervals


def expand_trajectory(speed_vector, *, request, time_grid: TimeGridSpec | None = None,
                      max_step_s: float = 60.0):
    """按航段顺序展开完整轨迹。

    已走过的航段必须由调用方从 ``request.segments`` 中裁掉；本函数不接受
    "已执行航段"，从而不会回滚已发生的时间与能量。
    """
    grid = time_grid or request.time_grid
    vector = tuple(float(value) for value in speed_vector)
    optimizable = request.optimizable_segment_ids
    if len(vector) != len(optimizable):
        raise ValueError(
            f"决策向量长度 {len(vector)} 与可优化航段数 {len(optimizable)} 不一致")
    for value in vector:
        if not math.isfinite(value):
            raise ValueError("决策向量必须是有限数值")
    targets = dict(zip(optimizable, vector))

    facts: list[SegmentFact] = []
    violations: list[Violation] = []
    offset = 0.0
    if request.current_speed_kmh is None:
        raise ValueError("展开轨迹必须给出当前航速 current_speed_kmh；不得默认为 0")
    entry_speed = request.current_speed_kmh
    for index, segment in enumerate(request.segments):
        fixed = not segment.optimizable
        next_segment = request.segments[index + 1] if index + 1 < len(request.segments) else None
        # 交接速度下限：两侧减速目标下限取较大者，避免展开出低于任一段限速下限的
        # "通过速度"（进出港与船闸的限速往往与相邻巡航段不同）。
        handoff_floor = segment_handoff_floor(segment, next_segment, grid)
        if next_segment is None:
            # 终点：默认减速停车；显式关闭时按该航段的通过速度处理。
            if grid.decelerate_to_stop_at_end:
                exit_speed = 0.0
            else:
                exit_speed = fixed_speed_floor(segment, grid)
        elif next_segment.optimizable:
            # 出口速度 = 下一段目标航速（不得越过本段限速上限），再抬到交接下限。
            exit_speed = min(max(targets[next_segment.segment_id], handoff_floor),
                             speed_ceiling(segment, grid))
        else:
            exit_speed = next_segment.fixed_speed_kmh or 0.0
        target = segment.fixed_speed_kmh if fixed else targets[segment.segment_id]
        phases, segment_violations = plan_segment_phases(
            segment, cruise_speed_kmh=target, entry_speed_kmh=entry_speed,
            exit_speed_kmh=exit_speed, grid=grid, fixed=fixed,
            distance_tolerance_km=grid.distance_tolerance_km,
            clamp_exit_to_floor=not (next_segment is None
                                     and grid.decelerate_to_stop_at_end),
            exit_floor=handoff_floor)
        sampled = sample_phases(phases, start_offset_s=offset,
                                step_s=grid.prediction_step_s, max_step_s=max_step_s)
        waiting = segment.waiting_duration_s if segment.waiting_rule else 0.0
        fact = SegmentFact(
            segment_id=segment.segment_id,
            requested_distance_km=segment.distance_km,
            phases=tuple(phases),
            sampled=tuple(sampled),
            entry_speed_kmh=entry_speed,
            exit_speed_kmh=exit_speed,
            cruise_speed_kmh=(target if any(p.kind == "cruise" for p in phases) else None),
            waiting_duration_s=waiting,
            waiting_rule=segment.waiting_rule,
            start_offset_s=offset,
            distance_tolerance_km=grid.distance_tolerance_km,
        )
        if not fact.distance_consistent:
            violations.append(Violation(
                code="distance_mismatch", segment_id=segment.segment_id,
                detail="展开轨迹的积分距离与航段距离不一致",
                observed=fact.distance_residual_km,
                limit=grid.distance_tolerance_km, unit="km"))
        facts.append(fact)
        violations.extend(segment_violations)
        offset = fact.end_offset_s
        # 链式口径：本段入口速度 = 上一段的**实际出口速度**。出口速度已被安排为
        # "下一段目标航速与交接下限取较大者"，因此下一段只需一段入段斜坡，
        # 不会重复减速、漏掉加速，也不会从低于限速下限的速度起步。
        entry_speed = exit_speed

    from research.tspeed.trajectory import ExpandedTrajectory  # noqa: F401  (同模块类型)

    return ExpandedTrajectory(
        speed_vector=vector,
        optimizable_segment_ids=optimizable,
        segment_facts=tuple(facts),
        violations=tuple(violations),
        prediction_step_s=min(grid.prediction_step_s, max_step_s),
    )
