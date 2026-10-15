"""PR #11 完善任务的回归测试：规则基线与滚动重规划。

对应《工具构建完善要求》三.1、三.2 两项负面结果的定位结论：

* 三.1：DE 曾比"各段最低速"规则基线差约 18%，根因是初始种群缺少
  **全取下限**角点个体——该角点恰是能量目标下的最优解。修复后 DE 与基线一致。
* 三.2：剩余 9 段滚动重规划曾返回 search_unresolved，根因是首段入口速度被
  按限速上限夹取（当前速度是请求边界条件，不是决策变量）。修复后可解。
"""

from __future__ import annotations

import dataclasses
import random

from research.tests import fixtures as f
from research.tspeed import DEConfig
from research.tspeed.eval import DEEvaluationContext, evaluate_candidate
from research.tspeed.search import (
    _init_population,
    effective_bounds,
    run_differential_evolution,
)
from research.tspeed.tool import SimulationScenario, run_speed_optimization


def _remaining_request(*, start_index: int = 8, current_speed_kmh: float = 7.5):
    base = f.request()
    remaining = dataclasses.replace(base, segments=base.segments[start_index:],
                                   current_speed_kmh=current_speed_kmh)
    distance = sum(segment.distance_km for segment in remaining.segments)
    budget = distance / max(remaining.time_grid.min_speed_kmh, 1.0) * 3600.0 * 1.8
    return dataclasses.replace(remaining, remaining_time_s=budget)


def test_initial_population_contains_both_corners():
    """种群必须显式包含全取下限与全取上限两个角点。"""
    lows = [4.0, 6.0, 5.0, 8.0]
    highs = [9.0, 12.0, 11.0, 13.0]
    population = _init_population(random.Random(7), lows, highs, 12)
    assert len(population) == 12
    assert any(all(abs(a - b) < 1e-12 for a, b in zip(individual, lows))
               for individual in population), "缺少全取下限角点"
    assert any(all(abs(a - b) < 1e-12 for a, b in zip(individual, highs))
               for individual in population), "缺少全取上限角点"
    keys = [tuple(round(value, 9) for value in individual)
            for individual in population]
    assert len(set(keys)) == len(keys), "种群个体必须互不相同"


def test_de_reaches_the_all_min_corner_baseline():
    """规则基线的同约束对照：DE 至少要与"各段最低速"基线一样好。

    这是三.1 的回归测试。该角点在有效边界内且可行时，DE 必须找到它
    （修复前实测比它差约 18%）。
    """
    request = f.request()
    spec = f.model_spec(grid=request.time_grid, demand_spec=f.demand_spec())
    bounds = effective_bounds(request)
    all_min = tuple(low for low, _ in bounds)

    context = DEEvaluationContext(
        request=request, spec=spec, environment_ref=spec.environment_ref,
        history_ref=spec.history_ref, config_hash="baseline-regression")
    baseline = evaluate_candidate(all_min, context=context)
    assert baseline.feasible, "各段最低速基线在验收情景下应可行"
    baseline_objective = baseline.objective_value
    assert baseline_objective is not None

    search = run_differential_evolution(
        request, spec=spec, environment_ref=spec.environment_ref,
        history_ref=spec.history_ref,
        config=DEConfig(population=24, max_generations=20, seeds=(20260925,)))
    assert search.best is not None, "DE 必须找到可行解"
    assert search.best.objective_value <= baseline_objective * 1.001, (
        f"DE 目标值 {search.best.objective_value:.4f} 明显差于规则基线 "
        f"{baseline_objective:.4f}")
    # 该角点本身就在初始种群中，故应被精确命中
    assert abs(search.best.objective_value - baseline_objective) < 1e-6, \
        "全取下限角点在初始种群内，DE 应精确命中该解"


def test_de_matches_baseline_across_seeds():
    """多种子下 DE 不应出现"某些种子明显劣于规则基线"的不稳定。"""
    request = f.request()
    spec = f.model_spec(grid=request.time_grid, demand_spec=f.demand_spec())
    bounds = effective_bounds(request)
    context = DEEvaluationContext(
        request=request, spec=spec, environment_ref=spec.environment_ref,
        history_ref=spec.history_ref, config_hash="baseline-multiseed")
    baseline_objective = evaluate_candidate(
        tuple(low for low, _ in bounds), context=context).objective_value
    assert baseline_objective is not None

    for seed in (1, 7, 42):
        search = run_differential_evolution(
            request, spec=spec, environment_ref=spec.environment_ref,
            history_ref=spec.history_ref,
            config=DEConfig(population=16, max_generations=10, seeds=(seed,)))
        assert search.best is not None, f"seed={seed} 未找到可行解"
        assert search.best.objective_value <= baseline_objective * 1.001, \
            f"seed={seed} 目标值 {search.best.objective_value:.4f} 差于基线"


def test_rolling_replan_returns_candidate_on_remaining_segments():
    """三.2 的回归测试：剩余 9 段、真实当前速度、按剩余航程折算的期限。"""
    remaining = _remaining_request()
    spec = f.model_spec(grid=remaining.time_grid, demand_spec=f.demand_spec())
    scenario = SimulationScenario(
        scenario_id="rolling-regression", environment_ref=spec.environment_ref,
        history_ref=spec.history_ref, model_spec=spec)
    run, result = run_speed_optimization(
        remaining, scenario=scenario,
        config=DEConfig(population=24, max_generations=20, seeds=(20260925,)),
        call_id="rolling-regression")
    assert result.search_status == "candidate_found", (
        f"剩余 9 段滚动重规划应可解，实际 {result.search_status}，"
        f"诊断 {[item.code for item in run.diagnostics]}")
    assert result.all_checks_passed, \
        f"未通过的检查：{[c.name for c in result.constraint_checks if not c.passed]}"
    assert result.total_energy_kwh is not None
    assert result.total_duration_s is not None


def test_rolling_replan_first_segment_keeps_real_current_speed():
    """首段入口必须等于真实当前速度（边界条件，不得被限速夹取）。"""
    remaining = _remaining_request(current_speed_kmh=7.5)
    spec = f.model_spec(grid=remaining.time_grid, demand_spec=f.demand_spec())
    scenario = SimulationScenario(
        scenario_id="rolling-boundary", environment_ref=spec.environment_ref,
        history_ref=spec.history_ref, model_spec=spec)
    run, result = run_speed_optimization(
        remaining, scenario=scenario,
        config=DEConfig(population=16, max_generations=10, seeds=(1,)),
        call_id="rolling-boundary")
    assert run.outcome_code == "ok"
    first_segment_id = result.selected_trajectory[0]["segment_id"]
    assert first_segment_id == remaining.segments[0].segment_id
    # 逐航段展开事实里，首段入口速度必须是请求给定的当前速度
    assert result.segment_summary[0]["entry_speed_kmh"] == 7.5
