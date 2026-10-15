"""DE/rand/1/bin 差分进化搜索（任务书第 4 节第 2 步）。

* 在合法速度边界初始化种群，完成差分变异、交叉与选择；
* 随机种子显式保存，同一配置与种子可复现；
* 越界候选采用固定修复规则（bounce-back 修复 + 可选拒绝），规则记录在结果里；
* 硬约束以"可行优先"处理，最终候选仍须独立复核，不能仅凭罚值低认定安全；
* 记录迭代轨迹、可行个体比例、违反约束统计、预测次数与耗时。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from research.contracts import (
    CandidateTrajectory,
    Phase,
    SearchTrace,
    SpeedOptimizationRequest,
    stable_hash,
)
from research.tspeed.eval import (
    CandidateEvaluation,
    DEEvaluationContext,
    EnergyModelSpec,
    evaluate_candidate,
)
from research.tspeed.trajectory import fixed_speed_floor, speed_ceiling

ALGORITHM_VERSION = "tspeed-de-rand-1-bin/1.0"

# 论文复现实验起点：种群 50、最多 500 代、F=0.5、CR=0.9。
# 最终配置须按本项目 LSTM 推理耗时、解稳定性与重规划要求确定。
PAPER_REPRODUCTION = {"population": 50, "max_generations": 500,
                      "mutation_factor": 0.5, "crossover_rate": 0.9}


@dataclass(frozen=True)
class DEConfig:
    """DE 配置；``seeds`` 支持多随机种子稳定性实验。"""

    population: int = PAPER_REPRODUCTION["population"]
    max_generations: int = PAPER_REPRODUCTION["max_generations"]
    mutation_factor: float = PAPER_REPRODUCTION["mutation_factor"]
    crossover_rate: float = PAPER_REPRODUCTION["crossover_rate"]
    seeds: tuple[int, ...] = (20260925,)
    repair: str = "bounce_back"
    stagnation_generations: int | None = None
    variant: str = "DE/rand/1/bin"

    def __post_init__(self) -> None:
        if self.population < 4:
            raise ValueError("种群规模至少为 4，才能构成 rand/1 差分向量")
        if self.max_generations < 1:
            raise ValueError("max_generations 至少为 1")
        if not 0.0 <= self.mutation_factor <= 2.0:
            raise ValueError("变异系数 F 必须在 [0, 2] 内")
        if not 0.0 <= self.crossover_rate <= 1.0:
            raise ValueError("交叉率 CR 必须在 [0, 1] 内")
        object.__setattr__(self, "seeds", tuple(int(seed) for seed in self.seeds))
        if not self.seeds:
            raise ValueError("至少给出一个随机种子")
        if self.repair not in ("bounce_back", "clip", "reject"):
            raise ValueError("repair 只允许 bounce_back / clip / reject")
        if self.stagnation_generations is not None and self.stagnation_generations < 1:
            raise ValueError("stagnation_generations 必须为正整数")

    @property
    def max_evaluations(self) -> int:
        return self.population * (self.max_generations + 1)

    def config_hash(self) -> str:
        return stable_hash({
            "variant": self.variant,
            "population": self.population,
            "max_generations": self.max_generations,
            "mutation_factor": self.mutation_factor,
            "crossover_rate": self.crossover_rate,
            "seeds": list(self.seeds),
            "repair": self.repair,
            "stagnation_generations": self.stagnation_generations,
        })


DEFAULT_DE_CONFIG = DEConfig()


@dataclass
class DERunResult:
    """单次 DE 运行结果。"""

    seed: int
    best: CandidateEvaluation | None
    feasible_count: int
    evaluations: int
    generations_run: int
    runtime_ms: float
    best_objective_by_generation: tuple[float | None, ...]
    feasible_fraction_by_generation: tuple[float, ...]
    stopped_reason: str
    diagnostic_candidates: tuple[CandidateEvaluation, ...] = ()


@dataclass
class DESearchResult:
    """一次搜索（可含多随机种子）的汇总。"""

    best: CandidateEvaluation | None
    runs: tuple[DERunResult, ...]
    config: DEConfig
    context: DEEvaluationContext
    trace: SearchTrace
    evaluations: int
    runtime_ms: float
    seeds_used: tuple[int, ...]
    objective_by_seed: dict[int, float | None]
    feasible_fraction_by_seed: dict[int, float]

    @property
    def found(self) -> bool:
        return self.best is not None


def effective_bounds(request: SpeedOptimizationRequest) -> list[tuple[float, float]]:
    """决策变量的**有效**边界（按航段顺序，只含可优化航段）。

    链式口径修正后（下一段入口 = 上一段的**实际**出口速度，含限速下限夹取），
    本段目标航速只需满足本段限速：前段限速更窄时，船舶以实际出口速度进入
    本段后再加/减速到目标航速，这是物理过程而非违规。因此边界取本段自身
    限速与全局范围的交集，不再用相邻段上限收缩搜索域（旧的求交会把
    "前段低速通过、本段再加速"的合法方案排除在搜索域外）。

    预检真正的衔接冲突：上一段的减速下限（限速下限与固定航速取严）高于
    本段限速上限时，任何决策都会让本段入口越界，属于输入约束冲突，显式报错。
    """
    segments = list(request.segments)
    grid = request.time_grid
    for prev, current in zip(segments, segments[1:]):
        floor_prev = fixed_speed_floor(prev, grid)
        high_current = speed_ceiling(current, grid)
        if floor_prev > high_current + 1e-9:
            raise ValueError(
                f"{prev.segment_id}→{current.segment_id}: 前段减速目标下限 "
                f"{floor_prev} km/h 高于后段限速上限 {high_current} km/h，"
                "任何航速决策都无法满足衔接（约束冲突，须由上游解决）")
    bounds: list[tuple[float, float]] = []
    for segment in segments:
        if not segment.optimizable:
            continue
        low = grid.min_speed_kmh
        high = grid.max_speed_kmh
        if segment.min_speed_kmh is not None:
            low = max(low, segment.min_speed_kmh)
        if segment.max_speed_kmh is not None:
            high = min(high, segment.max_speed_kmh)
        if low > high:
            raise ValueError(
                f"{segment.segment_id}: 航段限速 [{segment.min_speed_kmh}, "
                f"{segment.max_speed_kmh}] 与全局范围 [{grid.min_speed_kmh}, "
                f"{grid.max_speed_kmh}] 无交集，无法生成可行航速")
        bounds.append((low, high))
    if not bounds:
        raise ValueError("没有可优化航段：全部航速固定，DE 无需搜索")
    return bounds


def _bounds(request: SpeedOptimizationRequest) -> tuple[list[float], list[float]]:
    """按航段限速与相邻衔接给出决策变量边界（固定航段不参与）。"""
    pairs = effective_bounds(request)
    return [low for low, _ in pairs], [high for _, high in pairs]


def _repair(value: float, low: float, high: float, rule: str) -> float:
    """越界修复：bounce_back 反射回边界内，clip 截断，reject 交给调用方拒绝。"""
    if low <= value <= high:
        return value
    span = high - low
    if rule == "clip" or span <= 0:
        return min(max(value, low), high)
    if rule == "reject":
        return min(max(value, low), high)
    # bounce_back：对越界量做反射，保证最终落在 [low, high]。
    offset = (value - low) % (2.0 * span)
    return low + (offset if offset <= span else 2.0 * span - offset)


def _init_population(rng: random.Random, lows: list[float], highs: list[float],
                     size: int) -> list[list[float]]:
    """在合法边界内初始化种群。

    显式包含两个角点个体：**全取下限**与**全取上限**。限速下限一侧在能量目标下
    往往是强解；若种群只随机撒点、每个个体仅注入单维端点，这个角点可能完全不在
    搜索起点上（实测过：DE 因此收敛到比"各段最低速"基线差约 18% 的解）。
    其余个体均匀随机，并保证互不相同。
    """
    population: list[list[float]] = [list(lows)]
    corner_count = 1
    if size > 1:
        population.append(list(highs))
        corner_count = 2
    while len(population) < size:
        population.append([rng.uniform(low, high) for low, high in zip(lows, highs)])
    # 单维端点覆盖：让每一维的上下限都被探索到；只改非角点个体，
    # 否则会把上面的角点覆盖掉（全上限个体正是被这一步改坏的）。
    if size > corner_count:
        for axis, (low, high) in enumerate(zip(lows, highs)):
            population[corner_count + axis % (size - corner_count)][axis] = low
            population[corner_count + (axis + 1) % (size - corner_count)][axis] = high
    deduped: list[list[float]] = []
    seen: set[tuple[float, ...]] = set()
    for candidate in population:
        key = tuple(round(value, 9) for value in candidate)
        if key in seen:
            for _ in range(64):
                replacement = [rng.uniform(low, high)
                               for low, high in zip(lows, highs)]
                key = tuple(round(value, 9) for value in replacement)
                if key not in seen:
                    candidate = replacement
                    break
        seen.add(key)
        deduped.append(candidate)
    return deduped


def _better(left: CandidateEvaluation | None, right: CandidateEvaluation | None) -> bool:
    """可行优先排序：可行解永远优于不可行解；同组内按目标值比较。"""
    if left is None:
        return False
    if right is None:
        return True
    if left.feasible != right.feasible:
        return left.feasible
    left_value = left.objective_value
    right_value = right.objective_value
    if left_value is None and right_value is None:
        return left.total_duration_s < right.total_duration_s
    if left_value is None:
        return False
    if right_value is None:
        return True
    if left_value != right_value:
        return left_value < right_value
    return left.total_duration_s < right.total_duration_s


def _sort_key(evaluation: CandidateEvaluation) -> tuple:
    return (0 if evaluation.feasible else 1,
            evaluation.objective_value if evaluation.objective_value is not None else float("inf"),
            evaluation.total_duration_s)


def run_differential_evolution(request: SpeedOptimizationRequest, *,
                               spec: EnergyModelSpec,
                               environment_ref: str, history_ref: str,
                               config: DEConfig = DEFAULT_DE_CONFIG,
                               parent_call_id: str | None = None
                               ) -> DESearchResult:
    """在给定请求上执行 DE 搜索；批量提交种群的预测请求。"""
    lows, highs = _bounds(request)
    dimension = len(lows)
    config_hash = config.config_hash()
    context = DEEvaluationContext(
        request=request, spec=spec, environment_ref=environment_ref,
        history_ref=history_ref, config_hash=config_hash,
        parent_call_id=parent_call_id)
    started = time.perf_counter()
    runs: list[DERunResult] = []
    objective_by_seed: dict[int, float | None] = {}
    feasible_fraction_by_seed: dict[int, float] = {}
    for seed in config.seeds:
        run = _run_once(request, context=context, config=config, seed=seed,
                        lows=lows, highs=highs, dimension=dimension)
        runs.append(run)
        objective_by_seed[seed] = (run.best.objective_value if run.best else None)
        feasible_fraction_by_seed[seed] = (
            run.feasible_fraction_by_generation[-1]
            if run.feasible_fraction_by_generation else 0.0)
    runtime_ms = (time.perf_counter() - started) * 1000.0

    best: CandidateEvaluation | None = None
    for run in runs:
        if _better(run.best, best):
            best = run.best
    primary = runs[0]
    diagnostic_candidates: list[CandidateEvaluation] = []
    if best is None:
        for run in runs:
            for item in run.diagnostic_candidates:
                if all(existing.candidate_id != item.candidate_id
                       for existing in diagnostic_candidates):
                    diagnostic_candidates.append(item)
    trace = SearchTrace(
        trace_id="trace-" + stable_hash({
            "input": request.input_hash(), "config": config_hash,
            "seeds": list(config.seeds), "model": spec.model_version
            or spec.predictor.model_version})[:16],
        seed=config.seeds[0],
        population=config.population,
        generations_requested=config.max_generations,
        generations_run=primary.generations_run,
        evaluations=sum(run.evaluations for run in runs),
        best_objective_by_generation=primary.best_objective_by_generation,
        feasible_fraction_by_generation=primary.feasible_fraction_by_generation,
        violation_counts=dict(sorted(context.violations.items())),
        restarts=len(config.seeds) - 1,
        cache_hits=context.cache_hits,
        diagnostic_candidates=tuple(item.as_diagnostic_candidate()
                                    for item in diagnostic_candidates[:10]),
        note=("DE 为启发式算法，不做全局最优保证；"
              f"修复规则={config.repair}，变体={config.variant}"),
    )
    return DESearchResult(
        best=best, runs=tuple(runs), config=config, context=context, trace=trace,
        evaluations=sum(run.evaluations for run in runs), runtime_ms=runtime_ms,
        seeds_used=config.seeds, objective_by_seed=objective_by_seed,
        feasible_fraction_by_seed=feasible_fraction_by_seed)


def _run_once(request: SpeedOptimizationRequest, *, context: DEEvaluationContext,
              config: DEConfig, seed: int, lows: list[float], highs: list[float],
              dimension: int) -> DERunResult:
    rng = random.Random(seed)
    started = time.perf_counter()
    population = _init_population(rng, lows, highs, config.population)
    evaluated = [evaluate_candidate(vector, context=context, batch_id=f"de-{seed}")
                 for vector in population]
    evaluations = len(evaluated)
    best_objective: list[float | None] = []
    feasible_fraction: list[float] = []
    best = min(evaluated, key=_sort_key)
    generations_run = 0
    stopped_reason = "max_generations_reached"
    stagnant = 0
    previous_best_value = best.objective_value if best.feasible else None

    for generation in range(config.max_generations):
        generations_run = generation + 1
        # 精英保留：把当前最优个体放进本轮试验向量，避免最优解被随机漂移丢弃，
        # 同时让重复候选命中评估缓存。
        elite_index = min(range(len(evaluated)), key=lambda position: _sort_key(evaluated[position]))
        trials: list[list[float]] = []
        for index in range(config.population):
            if index == elite_index:
                trials.append(list(population[elite_index]))
                continue
            pool = [candidate for position, candidate in enumerate(population)
                    if position != index]
            if len(pool) < 3:
                trials.append(list(population[index]))
                continue
            a, b, c = rng.sample(pool, 3)
            trial: list[float] = []
            forced = rng.randrange(dimension)
            for axis in range(dimension):
                if axis == forced or rng.random() < config.crossover_rate:
                    mutant = a[axis] + config.mutation_factor * (b[axis] - c[axis])
                    value = _repair(mutant, lows[axis], highs[axis], config.repair)
                    if config.repair == "reject" and not lows[axis] <= mutant <= highs[axis]:
                        value = population[index][axis]
                else:
                    value = population[index][axis]
                trial.append(min(max(value, lows[axis]), highs[axis]))
            trials.append(trial)
        trial_evaluations = [evaluate_candidate(vector, context=context,
                                                batch_id=f"de-{seed}-g{generation}")
                             for vector in trials]
        evaluations += len(trial_evaluations)
        for index, trial in enumerate(trial_evaluations):
            if _better(trial, evaluated[index]):
                evaluated[index] = trial
                population[index] = list(trial.speed_vector)
        best = min(evaluated, key=_sort_key)
        feasible_count = sum(1 for item in evaluated if item.feasible)
        feasible_fraction.append(feasible_count / len(evaluated))
        best_objective.append(best.objective_value if best.feasible else None)
        current_value = best.objective_value if best.feasible else None
        if current_value is not None and (
                previous_best_value is None or current_value < previous_best_value - 1e-12):
            previous_best_value = current_value
            stagnant = 0
        else:
            stagnant += 1
        if config.stagnation_generations is not None \
                and stagnant >= config.stagnation_generations:
            stopped_reason = f"stagnation_{config.stagnation_generations}_generations"
            break

    diagnostics: tuple[CandidateEvaluation, ...] = ()
    if not best.feasible:
        diagnostics = tuple(sorted(evaluated, key=_sort_key)[:5])
    return DERunResult(
        seed=seed,
        best=best if best.feasible else None,
        feasible_count=sum(1 for item in evaluated if item.feasible),
        evaluations=evaluations,
        generations_run=generations_run,
        runtime_ms=(time.perf_counter() - started) * 1000.0,
        best_objective_by_generation=tuple(best_objective),
        feasible_fraction_by_generation=tuple(feasible_fraction),
        stopped_reason=stopped_reason,
        diagnostic_candidates=diagnostics,
    )


def candidate_trajectory(evaluation: CandidateEvaluation) -> CandidateTrajectory:
    """把已评估候选还原为 v2 轨迹（供最终核验与 FDP 复核复用）。

    逐区间写入 ``planned_speed_kmh``（区间平均速度）与 ``target_speed_kmh``
    （该段决策变量），使核验阶段能精确复原决策向量。
    """
    from research.contracts import TrajectoryPoint

    points = []
    for fact in evaluation.expanded.segment_facts:
        target = fact.cruise_speed_kmh
        for interval in fact.sampled:
            points.append(TrajectoryPoint(
                segment_id=fact.segment_id, phase=interval.phase,
                start_offset_s=round(interval.start_offset_s, 9),
                duration_s=round(interval.duration_s, 9),
                planned_speed_kmh=round(interval.planned_speed_kmh, 9),
                target_speed_kmh=(round(target, 9) if target is not None else None),
                interval_kind=interval.kind))
        if fact.waiting_duration_s > 0:
            points.append(TrajectoryPoint(
                segment_id=fact.segment_id, phase=Phase.WAITING.value,
                start_offset_s=round(fact.sailing_duration_s + fact.start_offset_s, 9),
                duration_s=round(fact.waiting_duration_s, 9),
                planned_speed_kmh=0.0, interval_kind="waiting"))
    return CandidateTrajectory(candidate_id=evaluation.candidate_id,
                               trajectory=tuple(points))
