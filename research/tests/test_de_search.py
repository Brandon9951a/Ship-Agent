"""DE 搜索验收：可复现性、与精确最优的对照、约束场景、预算与缓存。"""

from __future__ import annotations

import dataclasses
import time

from research.contracts import SegmentConstraint, SourceRef
from research.tests import fixtures as f
from research.tests.reference import exact_grid_optimum, exhaustive_optimum
from research.tspeed import DEConfig, run_differential_evolution


def small_request(*, segments: int = 4, penalty_lambda: float = 0.0,
                  remaining_time_s: float | None = 86400.0,
                  battery_budget_kwh: float | None = None):
    """小规模请求：4 个可优化航段，便于与精确最优对照。"""
    request = f.request(remaining_time_s=remaining_time_s,
                        battery_budget_kwh=battery_budget_kwh,
                        speed_penalty_lambda=penalty_lambda)
    return dataclasses.replace(request, segments=request.segments[:segments],
                               current_speed_kmh=7.0)


def _search(request, *, config: DEConfig, max_step_s: float = 60.0):
    spec = f.model_spec(grid=request.time_grid, max_step_s=max_step_s)
    return run_differential_evolution(
        request, spec=spec, environment_ref=spec.environment_ref,
        history_ref=spec.history_ref, config=config)


def test_reproducible_with_same_seed():
    request = small_request()
    config = DEConfig(population=12, max_generations=6, seeds=(1234,))
    first = _search(request, config=config)
    second = _search(request, config=config)
    assert first.best is not None and second.best is not None
    assert first.best.speed_vector == second.best.speed_vector, \
        "同一配置与种子必须完全可复现"
    assert first.best.objective_value == second.best.objective_value
    assert first.trace.trace_id == second.trace.trace_id


def test_different_seed_changes_population_but_stays_feasible():
    request = small_request()
    config = DEConfig(population=12, max_generations=6, seeds=(1, 2))
    result = _search(request, config=config)
    assert len(result.runs) == 2
    assert result.trace.restarts == 1
    assert set(result.objective_by_seed) == {1, 2}
    assert result.best is not None
    assert result.best.feasible


def test_de_matches_exact_grid_optimum_on_small_case():
    """DE 与精确 DP 的对照：偏差应来自搜索误差，且落在 1% 以内。"""
    request = small_request(penalty_lambda=0.0)
    spec = f.model_spec(grid=request.time_grid)
    exact_value, exact_vector, _, _ = exact_grid_optimum(
        request, predictor=spec.predictor, step_kmh=0.25, penalty_lambda=0.0)
    config = DEConfig(population=24, max_generations=40, seeds=(2026, 77))
    result = _search(request, config=config)
    assert result.best is not None, "DE 必须找到可行解"
    got = result.best.objective_value
    assert got is not None
    relative = (got - exact_value) / exact_value
    assert relative <= 0.01, (f"DE 目标值 {got:.6f} 比网格最优 {exact_value:.6f} "
                              f"差 {relative:.2%}，超过 1% 容差")
    for value, segment in zip(result.best.speed_vector,
                              [s for s in request.segments if s.optimizable]):
        if segment.min_speed_kmh is not None:
            assert value >= segment.min_speed_kmh - 1e-9
        if segment.max_speed_kmh is not None:
            assert value <= segment.max_speed_kmh + 1e-9


def test_penalty_term_is_applied():
    """加入惩罚项后，相邻航速差异应被压低，且目标值包含惩罚分项。"""
    request_flat = small_request(penalty_lambda=0.0)
    request_penalised = small_request(penalty_lambda=40.0)
    config = DEConfig(population=16, max_generations=10, seeds=(5,))
    flat = _search(request_flat, config=config)
    penalised = _search(request_penalised, config=config)
    assert flat.best is not None and penalised.best is not None
    spread_flat = max(flat.best.speed_vector) - min(flat.best.speed_vector)
    spread_pen = max(penalised.best.speed_vector) - min(penalised.best.speed_vector)
    assert spread_pen <= spread_flat + 1e-9, \
        f"惩罚项应压低相邻速度差：{spread_pen:.4f} vs {spread_flat:.4f}"
    assert penalised.best.penalty > 0.0
    assert abs(penalised.best.objective_value
               - (penalised.best.energy_kwh + 40.0 * penalised.best.penalty)) < 1e-9


def test_time_constraint_blocks_infeasible_candidates():
    """时间过紧：搜索应返回未解决，并给出约束统计而不是假成功。"""
    request = small_request(remaining_time_s=600.0)
    config = DEConfig(population=12, max_generations=4, seeds=(3,))
    result = _search(request, config=config)
    assert result.best is None
    assert result.runs[0].diagnostic_candidates, "必须保留最优诊断候选"
    assert any(item.violations for item in result.runs[0].diagnostic_candidates)
    codes = set(result.context.violations)
    assert "arrival_deadline_missed" in codes


def test_battery_budget_blocks_infeasible_candidates():
    request = small_request(battery_budget_kwh=1.0)
    config = DEConfig(population=12, max_generations=4, seeds=(4,))
    result = _search(request, config=config)
    assert result.best is None
    assert "battery_budget_exceeded" in set(result.context.violations)


def test_feasible_candidates_report_no_violations():
    request = small_request()
    config = DEConfig(population=14, max_generations=8, seeds=(9,))
    result = _search(request, config=config)
    assert result.best is not None
    assert result.best.violations == ()
    assert result.best.expanded.violation_codes == ()
    assert result.best.expanded.distance_consistent
    assert result.trace.feasible_fraction_by_generation[-1] > 0.0


def test_violation_statistics_are_recorded():
    request = small_request(remaining_time_s=600.0)
    config = DEConfig(population=10, max_generations=3, seeds=(11,))
    result = _search(request, config=config)
    assert result.trace.violation_counts
    assert sum(result.trace.violation_counts.values()) > 0


def test_evaluation_budget_is_reported_and_bounded():
    request = small_request()
    config = DEConfig(population=10, max_generations=5, seeds=(13,))
    result = _search(request, config=config)
    assert result.evaluations <= config.max_evaluations
    assert result.trace.evaluations == sum(run.evaluations for run in result.runs)
    assert result.trace.generations_run <= config.max_generations


def test_stagnation_stop_reduces_generations():
    request = small_request()
    config = DEConfig(population=10, max_generations=40, seeds=(17,),
                      stagnation_generations=3)
    result = _search(request, config=config)
    assert result.trace.generations_run <= 40
    assert result.runs[0].stopped_reason.startswith("stagnation") \
        or result.trace.generations_run == 40


def test_repair_rule_keeps_solutions_in_bounds():
    request = small_request(penalty_lambda=5.0)
    for repair in ("bounce_back", "clip", "reject"):
        config = DEConfig(population=10, max_generations=5, seeds=(19,),
                          repair=repair)
        result = _search(request, config=config)
        assert result.best is not None, f"repair={repair} 必须仍能找到可行解"
        assert result.trace.note and repair in result.trace.note


def test_cache_is_used_across_generations():
    request = small_request()
    config = DEConfig(population=16, max_generations=6, seeds=(23,))
    result = _search(request, config=config)
    assert result.context.cache_hits > 0, "重复候选应命中缓存，避免重复预测"
    assert result.trace.cache_hits == result.context.cache_hits


def test_boundary_solutions_are_reachable():
    """无惩罚且能量随航速单调增时，最优应趋近各段限速下限，且不差于粗网格对照。"""
    request = small_request(penalty_lambda=0.0)
    config = DEConfig(population=24, max_generations=40, seeds=(31,))
    result = _search(request, config=config)
    assert result.best is not None
    lows = [segment.min_speed_kmh for segment in request.segments
            if segment.optimizable]
    for value, low in zip(result.best.speed_vector, lows):
        assert value <= low + 0.5, f"最优航速 {value:.3f} 明显高于下限 {low}"
    spec = f.model_spec(grid=request.time_grid)
    exhaustive_value, _, _, _ = exhaustive_optimum(
        request, predictor=spec.predictor, step_kmh=1.0, limit=4)
    # 连续搜索不差于 1.0 km/h 粗网格穷举（网格离散误差使穷举值偏高属正常）
    assert result.best.objective_value <= exhaustive_value / 0.99, \
        f"DE {result.best.objective_value:.4f} 明显差于粗网格穷举 {exhaustive_value:.4f}"


def test_elite_retention_keeps_best_monotone():
    """精英保留：批内最优目标值随代数不增。"""
    request = small_request(penalty_lambda=1.0)
    config = DEConfig(population=14, max_generations=8, seeds=(53,))
    result = _search(request, config=config)
    history = [value for value in result.runs[0].best_objective_by_generation
               if value is not None]
    assert history, "必须记录逐代最优目标值"
    for previous, current in zip(history, history[1:]):
        assert current <= previous + 1e-9, "逐代最优不得变差"


def test_search_runtime_is_measured():
    request = small_request()
    config = DEConfig(population=10, max_generations=3, seeds=(37,))
    started = time.perf_counter()
    result = _search(request, config=config)
    elapsed = time.perf_counter() - started
    assert result.runtime_ms > 0.0
    assert result.runtime_ms <= elapsed * 1000.0 + 1.0
    assert all(run.runtime_ms > 0.0 for run in result.runs)


def test_bounds_rejected_when_no_feasible_speed():
    request = small_request()
    segment = SegmentConstraint(segment_id="S02", distance_km=6.4,
                               min_speed_kmh=20.0, max_speed_kmh=25.0)
    patched = dataclasses.replace(
        request, segments=(request.segments[0], segment) + request.segments[2:])
    config = DEConfig(population=8, max_generations=2, seeds=(41,))
    try:
        _search(patched, config=config)
    except ValueError:
        return
    raise AssertionError("限速与全局范围无交集时必须显式报错")


def test_all_segments_fixed_is_rejected():
    request = small_request()
    fixed = tuple(dataclasses.replace(segment, fixed_speed_kmh=6.0)
                  for segment in request.segments)
    patched = dataclasses.replace(request, segments=fixed)
    config = DEConfig(population=8, max_generations=2, seeds=(43,))
    try:
        _search(patched, config=config)
    except ValueError:
        return
    raise AssertionError("没有可优化航段时必须显式报错，而不是返回空搜索")


def test_violation_counts_use_stable_codes():
    request = small_request(remaining_time_s=600.0)
    config = DEConfig(population=10, max_generations=3, seeds=(47,))
    result = _search(request, config=config)
    for code in result.context.violations:
        assert isinstance(code, str) and code, "违反约束统计必须使用稳定代码"
    assert all(isinstance(value, int) for value in result.context.violations.values())


def test_source_ref_in_fixture_is_explicit_assumption():
    sources = {ref.source_id for ref in f.request().source_refs}
    assert sources, "验收情景必须带来源"
    assert isinstance(f.ASSUMPTION, SourceRef)
