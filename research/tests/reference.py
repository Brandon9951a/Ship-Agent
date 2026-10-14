"""DE 的对照求解器：在离散航速网格上求**精确**最优（不做近似）。

物理模型（与 ``expand_trajectory`` 完全一致）：航段 ``i`` 的能量只取决于
``(入口速度, 目标航速_i, 出口速度)``，其中

* 出口速度 = 下一段的入口速度；下一段是固定航段时取其固定航速；
* 末个可优化航段之后若还有固定航段，则以该固定航段的固定航速作为出口；
* 末段（无后继）出口速度为 0（默认停车）或该航段的通过速度下限。

因此采用「分段代价表 + 状态为连续两段目标航速的 DP」即为精确最优。

三套实现互为验证：
* ``exhaustive_optimum``：小规模穷举（≤4 可优化航段），作为基准；
* ``exact_grid_optimum``：分段代价表的 DP，用于完整 17 航段场景；
* DP 内部在可穷举规模上自动与穷举比对，防止对照求解器自身出错而误判 DE。
"""

from __future__ import annotations

import itertools

from research.tspeed.predictor import AnalyticPowerPredictor
from research.tspeed.trajectory import (
    fixed_speed_floor,
    plan_segment_phases,
    sample_phases,
)

INF = float("inf")


def segment_energy_and_duration(segment, *, entry_kmh: float, cruise_kmh: float,
                                exit_kmh: float, grid, predictor: AnalyticPowerPredictor,
                                max_step_s: float | None = None) -> tuple[float, float]:
    """复算单个航段的 (电机侧能量 kWh, 历时 s)；与展开器使用同一物理模型。"""
    phases, violations = plan_segment_phases(
        segment, cruise_speed_kmh=cruise_kmh, entry_speed_kmh=entry_kmh,
        exit_speed_kmh=exit_kmh, grid=grid, fixed=False,
        distance_tolerance_km=grid.distance_tolerance_km)
    if violations:
        return INF, INF
    sampled = sample_phases(phases, start_offset_s=0.0,
                            step_s=grid.prediction_step_s,
                            max_step_s=(grid.prediction_step_s if max_step_s is None
                                        else max_step_s))
    energy = 0.0
    duration = 0.0
    for interval in sampled:
        energy += predictor.power_kw(interval.planned_speed_kmh) * interval.duration_s / 3600.0
        duration += interval.duration_s
    return energy, duration


class StructuredPlan:
    """请求的结构化视图：头段 / 可优化段 / 尾段与边界速度。"""

    def __init__(self, request, predictor: AnalyticPowerPredictor,
                 max_step_s: float | None = None) -> None:
        self.request = request
        self.predictor = predictor
        self.grid = request.time_grid
        # 默认与判分路径使用同一时间离散化，使对照结果可直接比较。
        self.max_step_s = (request.time_grid.prediction_step_s
                           if max_step_s is None else max_step_s)
        segments = list(request.segments)
        indices = [index for index, segment in enumerate(segments)
                   if segment.optimizable]
        if not indices:
            raise ValueError("没有可优化航段")
        self.first, self.last = indices[0], indices[-1]
        if indices != list(range(self.first, self.last + 1)):
            raise ValueError("固定航段与可优化航段必须分段连续，暂不支持交错排列")
        entry = request.current_speed_kmh
        if entry is None:
            raise ValueError("必须给出当前航速")
        self.head = segments[:self.first]
        self.optimizable = segments[self.first:self.last + 1]
        self.tail = segments[self.last + 1:]
        for segment in self.head + self.tail:
            if segment.fixed_speed_kmh is None:
                raise ValueError(f"{segment.segment_id}: 固定航段必须有固定航速")
        self.head_entry_speed = entry
        self.waiting_duration = sum(segment.waiting_duration_s or 0.0
                                    for segment in segments if segment.waiting_rule)

    @property
    def count(self) -> int:
        return len(self.optimizable)

    def entry_speed_of_first(self) -> tuple[float, float, float]:
        """返回 (入口速度, 头段能量, 头段历时)。

        只有最后一个头段航段的出口速度依赖第一个目标航速，故它不在头段结算；
        若没有头段，入口速度即当前航速。
        """
        if not self.head:
            return self.head_entry_speed, 0.0, 0.0
        cursor = self.head_entry_speed
        energy = 0.0
        duration = 0.0
        for index, segment in enumerate(self.head[:-1]):
            target = segment.fixed_speed_kmh
            next_speed = self.head[index + 1].fixed_speed_kmh
            segment_energy, segment_duration = segment_energy_and_duration(
                segment, entry_kmh=cursor, cruise_kmh=target, exit_kmh=next_speed,
                grid=self.grid, predictor=self.predictor, max_step_s=self.max_step_s)
            energy += segment_energy
            duration += segment_duration
            cursor = next_speed
        # 最后一个头段的出口速度 = 第一个可优化航段的目标航速（由调用方传入）
        return cursor, energy, duration

    def head_segment(self):
        return self.head[-1] if self.head else None

    def tail_exit_speed(self) -> float:
        """末个可优化航段的出口速度。"""
        if self.tail:
            return self.tail[0].fixed_speed_kmh
        if self.grid.decelerate_to_stop_at_end:
            return 0.0
        return fixed_speed_floor(self.optimizable[-1], self.grid)

    def tail_energy_duration(self, *, entry_speed: float) -> tuple[float, float]:
        """尾段（固定航段链）的能量与历时；入口速度 = 末个可优化航段的出口速度。"""
        if not self.tail:
            return 0.0, 0.0
        cursor = entry_speed
        energy = 0.0
        duration = 0.0
        for index, segment in enumerate(self.tail):
            target = segment.fixed_speed_kmh
            if index + 1 < len(self.tail):
                following = self.tail[index + 1].fixed_speed_kmh
            elif self.grid.decelerate_to_stop_at_end:
                following = fixed_speed_floor(segment, self.grid)
            else:
                following = fixed_speed_floor(segment, self.grid)
            segment_energy, segment_duration = segment_energy_and_duration(
                segment, entry_kmh=cursor, cruise_kmh=target, exit_kmh=following,
                grid=self.grid, predictor=self.predictor, max_step_s=self.max_step_s)
            energy += segment_energy
            duration += segment_duration
            cursor = following
        return energy, duration


def grid_values(low: float, high: float, step_kmh: float) -> list[float]:
    count = int(round((high - low) / step_kmh))
    values = [low + index * step_kmh for index in range(count + 1)]
    if values[-1] < high - 1e-9:
        values.append(high)
    return values


def speed_options(plan: StructuredPlan, step_kmh: float) -> list[list[float]]:
    grid = plan.grid
    options: list[list[float]] = []
    for segment in plan.optimizable:
        low = max(grid.min_speed_kmh,
                  segment.min_speed_kmh if segment.min_speed_kmh is not None
                  else grid.min_speed_kmh)
        high = min(grid.max_speed_kmh,
                   segment.max_speed_kmh if segment.max_speed_kmh is not None
                   else grid.max_speed_kmh)
        options.append(grid_values(low, high, step_kmh))
    return options


def _evaluate_vector(request, vector, *, predictor: AnalyticPowerPredictor,
                     penalty_lambda: float, max_step_s: float | None = None):
    """按展开器口径完整复算一个决策向量的 (objective, energy, duration)。"""
    plan = StructuredPlan(request, predictor, max_step_s)
    entry, head_energy, head_duration = plan.entry_speed_of_first()
    head_segment = plan.head_segment()
    energy = head_energy
    duration = head_duration
    speeds = list(vector)
    if head_segment is not None:
        # 头段最后一个固定航段的出口速度 = 第一个目标航速
        segment_energy, segment_duration = segment_energy_and_duration(
            head_segment, entry_kmh=entry, cruise_kmh=head_segment.fixed_speed_kmh,
            exit_kmh=speeds[0], grid=plan.grid, predictor=predictor,
            max_step_s=max_step_s)
        if segment_energy == INF:
            return None
        energy += segment_energy
        duration += segment_duration
    for index, cruise in enumerate(speeds):
        # 入口速度 = 上一段的**目标航速**（展开器口径：上一段的出口速度就是它）
        if index == 0:
            previous = (head_segment.fixed_speed_kmh if head_segment is not None
                        else entry)
        else:
            previous = speeds[index - 1]
        if index + 1 < len(speeds):
            following = speeds[index + 1]
        else:
            following = plan.tail_exit_speed()
        segment_energy, segment_duration = segment_energy_and_duration(
            plan.optimizable[index], entry_kmh=previous, cruise_kmh=cruise,
            exit_kmh=following, grid=plan.grid, predictor=predictor,
            max_step_s=max_step_s)
        if segment_energy == INF:
            return None
        energy += segment_energy
        duration += segment_duration
    tail_energy, tail_duration = plan.tail_energy_duration(
        entry_speed=plan.tail_exit_speed())
    energy += tail_energy
    duration += tail_duration
    penalty = penalty_lambda * sum(
        (left - right) ** 2 for left, right in zip(speeds, speeds[1:])) \
        / max(len(speeds) - 1, 1)
    return energy + penalty, energy, duration


def exhaustive_optimum(request, *, predictor: AnalyticPowerPredictor,
                       step_kmh: float, penalty_lambda: float | None = None,
                       max_step_s: float | None = None, limit: int = 4):
    """小规模穷举（≤ ``limit`` 个可优化航段）：DP 与 DE 的共同基准。"""
    plan = StructuredPlan(request, predictor, max_step_s)
    if plan.count > limit:
        raise ValueError(f"穷举只支持 ≤ {limit} 个可优化航段，当前 {plan.count}")
    penalty_lambda = (request.speed_penalty_lambda if penalty_lambda is None
                      else penalty_lambda)
    best = None
    for combination in itertools.product(*speed_options(plan, step_kmh)):
        result = _evaluate_vector(request, combination, predictor=predictor,
                                  penalty_lambda=penalty_lambda,
                                  max_step_s=max_step_s)
        if result is None:
            continue
        value, energy, duration = result
        if best is None or value < best[0] - 1e-12:
            best = (value, tuple(combination), energy, duration)
    if best is None:
        raise ValueError("穷举范围内没有可行解")
    return best


def exact_grid_optimum(request, *, predictor: AnalyticPowerPredictor,
                       step_kmh: float = 0.5, penalty_lambda: float | None = None,
                       max_step_s: float | None = None, time_limit_s: float | None = None,
                       verify_against_exhaustive: bool = True):
    """分段代价表 + DP 的精确最优，返回 ``(objective, vector, energy, duration)``。"""
    plan = StructuredPlan(request, predictor, max_step_s)
    options = speed_options(plan, step_kmh)
    penalty_lambda = (request.speed_penalty_lambda if penalty_lambda is None
                      else penalty_lambda)
    normaliser = max(plan.count - 1, 1)
    grid = plan.grid

    entry_base, head_energy, head_duration = plan.entry_speed_of_first()
    head_segment = plan.head_segment()
    exit_last = plan.tail_exit_speed()
    tail_energy, tail_duration = plan.tail_energy_duration(entry_speed=exit_last)

    # 首段代价：需要出口速度 = 第二个目标航速（或 exit_last）
    def first_cost(cruise: float, following: float) -> tuple[float, float]:
        energy = head_energy
        duration = head_duration
        entry = entry_base
        if head_segment is not None:
            head_energy_part, head_duration_part = segment_energy_and_duration(
                head_segment, entry_kmh=entry, cruise_kmh=head_segment.fixed_speed_kmh,
                exit_kmh=cruise, grid=grid, predictor=predictor,
                max_step_s=max_step_s)
            if head_energy_part == INF:
                return INF, INF
            energy += head_energy_part
            duration += head_duration_part
            entry = head_segment.fixed_speed_kmh
        energy_part, duration_part = segment_energy_and_duration(
            plan.optimizable[0], entry_kmh=entry, cruise_kmh=cruise,
            exit_kmh=following, grid=grid, predictor=predictor,
            max_step_s=max_step_s)
        if energy_part == INF:
            return INF, INF
        return energy + energy_part, duration + duration_part

    def middle_cost(depth: int, previous: float, cruise: float, following: float):
        return segment_energy_and_duration(
            plan.optimizable[depth], entry_kmh=previous, cruise_kmh=cruise,
            exit_kmh=following, grid=grid, predictor=predictor,
            max_step_s=max_step_s)

    count = plan.count
    if count == 1:
        best = None
        for cruise in options[0]:
            energy, duration = first_cost(cruise, exit_last)
            if energy == INF:
                continue
            candidate = (energy + tail_energy, duration + tail_duration, cruise)
            if best is None or candidate[0] < best[0] - 1e-12:
                best = candidate
        if best is None:
            raise ValueError("网格内没有可行解")
        vector = (best[2],)
        value, duration = best[0], best[1]
    else:
        # 状态 (b, a)：前一段与当前段的目标航速索引
        layer: dict[tuple[int, int], tuple[float, float, tuple[int, int] | None]] = {}
        for b_index, previous in enumerate(options[0]):
            for a_index, cruise in enumerate(options[1]):
                energy, duration = first_cost(previous, cruise)
                if energy == INF:
                    continue
                layer[(b_index, a_index)] = (energy, duration, None)
        if not layer:
            raise ValueError("网格内没有可行解")
        layers = [layer]
        for depth in range(2, count):
            previous_layer = layers[-1]
            current: dict[tuple[int, int],
                          tuple[float, float, tuple[int, int] | None]] = {}
            for (b_index, a_index), (cost, duration, _) in previous_layer.items():
                left = options[depth - 2][b_index]
                middle = options[depth - 1][a_index]
                for c_index, right in enumerate(options[depth]):
                    energy, segment_duration = middle_cost(depth - 1, left, middle, right)
                    if energy == INF:
                        continue
                    penalty = penalty_lambda * (middle - right) ** 2 / normaliser
                    key = (a_index, c_index)
                    candidate = (cost + energy + penalty,
                                 duration + segment_duration, (b_index, a_index))
                    existing = current.get(key)
                    if existing is None or candidate[0] < existing[0] - 1e-12:
                        current[key] = candidate
            if not current:
                raise ValueError("网格内没有可行解")
            layers.append(current)
        # 末段结算：出口速度 = exit_last
        best = None
        for (b_index, a_index), (cost, duration, _) in layers[-1].items():
            left = options[count - 2][b_index]
            right = options[count - 1][a_index]
            energy, segment_duration = middle_cost(count - 1, left, right, exit_last)
            if energy == INF:
                continue
            candidate = (cost + energy + tail_energy,
                         duration + segment_duration + tail_duration,
                         (b_index, a_index))
            if best is None or candidate[0] < best[0] - 1e-12:
                best = candidate
        if best is None:
            raise ValueError("网格内没有可行解")
        value, duration, state = best
        chain: list[tuple[int, int]] = []
        position: tuple[int, int] | None = state
        for depth in range(len(layers), 0, -1):
            if position is None:
                raise ValueError("DP 回溯失败：前驱指针缺失")
            chain.append(position)
            position = layers[depth - 1][position][2]
        chain.reverse()
        speeds = [options[0][chain[0][0]]]
        for depth, (_, a_index) in enumerate(chain, start=1):
            speeds.append(options[depth][a_index])
        vector = tuple(speeds)

    # 自检：用展开器口径复算同一向量，确认对照求解器自身自洽
    recomputed = _evaluate_vector(request, vector, predictor=predictor,
                                  penalty_lambda=penalty_lambda,
                                  max_step_s=max_step_s)
    if recomputed is None:
        raise AssertionError("DP 返回的向量按展开器口径不可行")
    if abs(recomputed[0] - value) > 1e-6:
        raise AssertionError(
            f"DP 目标值 {value:.6f} 与按展开器复算的 {recomputed[0]:.6f} 不一致")
    if time_limit_s is not None and duration > time_limit_s + 1e-6:
        raise ValueError("网格最优解的历时超过时间约束，无法作为对照")
    if verify_against_exhaustive and count <= 4:
        reference = exhaustive_optimum(
            request, predictor=predictor, step_kmh=step_kmh,
            penalty_lambda=penalty_lambda, max_step_s=max_step_s, limit=4)
        if reference[0] < value - 1e-6:
            raise AssertionError(
                f"DP（{value:.6f}）劣于穷举（{reference[0]:.6f}），对照求解器不可用")
    return value, vector, recomputed[1], recomputed[2]
