"""Tspeed（DE）验收实验：算法正确性、多种子稳定性、耗时预算与约束场景。

用法::

    python -m research.scripts.run_tspeed_experiments --output-dir artifacts/tspeed-v2

只依赖标准库与 ``research`` 包；不联网、不训练模型。所有数字都由本仓库代码
现场算出，不使用导师论文的效果数值。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import platform
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from research.contracts import stable_hash
from research.tests import fixtures as f
from research.tests.reference import exact_grid_optimum, exhaustive_optimum
from research.tspeed import DEConfig
from research.tspeed.tool import SimulationScenario, run_speed_optimization

# 论文复现实验起点（仅作对照配置，不代表本项目冻结参数）
PAPER = {"population": 50, "max_generations": 500, "mutation_factor": 0.5,
         "crossover_rate": 0.9}
# 本次可跑完的配置（按实测耗时确定）
PRACTICAL = {"population": 40, "max_generations": 60, "mutation_factor": 0.5,
             "crossover_rate": 0.9}
STABILITY_SEEDS = (20260925, 1, 7, 42, 2026)


def _scenario(request, *, demand: bool = True, limit_kw: float | None = None,
              step_s: float | None = None) -> SimulationScenario:
    grid = request.time_grid
    spec = f.model_spec(
        grid=grid,
        max_step_s=step_s,
        demand_spec=f.demand_spec() if demand else None,
        demand_limit_kw=limit_kw)
    return SimulationScenario(
        scenario_id="scenario.de-acceptance",
        environment_ref="scenario.env-001",
        history_ref="history.ref-001",
        model_spec=spec,
        source_refs=(f.DOC,),
        assumptions=("功率曲线为验收用解析函数 P=c·v³，非实船标定",
                     "母线侧效率与辅助负载为具名研究假设"),
        note="DE 验收情景")


def experiment_optimality(small_request) -> dict:
    """与精确最优对照（小规模：4 段，DP 状态规模可控）。"""
    spec = f.model_spec(grid=small_request.time_grid)
    results = {}
    for lambda_value in (0.0, 10.0):
        request = dataclasses.replace(small_request,
                                      speed_penalty_lambda=lambda_value)
        started = time.perf_counter()
        exact = exact_grid_optimum(request, predictor=spec.predictor,
                                   step_kmh=0.25)
        exact_ms = (time.perf_counter() - started) * 1000.0
        config = DEConfig(population=24, max_generations=40, seeds=(2026, 77))
        run, result = run_speed_optimization(request, scenario=_scenario(request),
                                             config=config)
        assert result.search_status == "candidate_found", result.search_status
        gap = (result.objective_terms.total - exact[0]) / exact[0]
        results[f"lambda_{lambda_value:g}"] = {
            "exact_objective": exact[0],
            "exact_vector": list(exact[1]),
            "exact_runtime_ms": exact_ms,
            "de_objective": result.objective_terms.total,
            "de_vector": list(result.selected_speed_vector),
            "relative_gap": gap,
            "de_runtime_ms": result.runtime_ms,
            "de_evaluations": result.evaluations,
            "all_checks_passed": result.all_checks_passed,
        }
    return results


def experiment_full_voyage(request, *, population: int, generations: int) -> dict:
    """17 航段完整航程：选定方案、检查项、逐航段汇总与耗时。"""
    config = DEConfig(population=population, max_generations=generations,
                      mutation_factor=PRACTICAL["mutation_factor"],
                      crossover_rate=PRACTICAL["crossover_rate"],
                      seeds=(20260925,))
    run, result = run_speed_optimization(request, scenario=_scenario(request),
                                         config=config, call_id="tspeed-full")
    payload = json.loads(run.to_json())
    return {
        "search_status": result.search_status,
        "outcome_code": payload["outcome_code"],
        "execution_status": payload["execution_status"],
        "selected_speed_vector": list(result.selected_speed_vector),
        "total_energy_kwh": result.total_energy_kwh,
        "total_duration_s": result.total_duration_s,
        "arrival_at": result.arrival_at,
        "bus_side_available": result.bus_side_available,
        "objective_terms": (result.objective_terms.to_dict()
                            if result.objective_terms else None),
        "constraint_checks": [check.to_dict() for check in result.constraint_checks],
        "segment_summary": list(result.segment_summary),
        "generations": result.generations,
        "evaluations": result.evaluations,
        "runtime_ms": result.runtime_ms,
        "ms_per_evaluation": (result.runtime_ms / result.evaluations
                              if result.evaluations else None),
        "diagnostics": [item.to_dict() for item in run.diagnostics],
        "assumptions": list(result.assumptions),
        "search_trace": (
            {
                "trace_id": result.search_trace.trace_id,
                "violation_counts": result.search_trace.violation_counts,
                "feasible_fraction_final": (
                    result.search_trace.feasible_fraction_by_generation[-1]
                    if result.search_trace.feasible_fraction_by_generation else None),
                "restarts": result.search_trace.restarts,
                "cache_hits": result.search_trace.cache_hits,
            } if result.search_trace else None),
    }


def experiment_multi_seed(request, *, population: int, generations: int) -> dict:
    """多随机种子：逐个种子运行，记录目标值分布、可行率与耗时。"""
    samples = []
    for seed in STABILITY_SEEDS:
        config = DEConfig(population=population, max_generations=generations,
                          seeds=(seed,))
        run, result = run_speed_optimization(request, scenario=_scenario(request),
                                             config=config,
                                             call_id=f"tspeed-seed-{seed}")
        samples.append({
            "seed": seed,
            "search_status": result.search_status,
            "outcome_code": run.outcome_code,
            "objective": (result.objective_terms.total
                          if result.objective_terms else None),
            "energy_kwh": result.total_energy_kwh,
            "duration_s": result.total_duration_s,
            "runtime_ms": result.runtime_ms,
            "evaluations": result.evaluations,
            "all_checks_passed": result.all_checks_passed,
        })
    objectives = [item["objective"] for item in samples
                  if item["objective"] is not None]
    summary = {
        "samples": samples,
        "found": len(objectives),
        "attempts": len(samples),
        "feasible_rate": len(objectives) / len(samples) if samples else None,
    }
    if objectives:
        summary.update({
            "objective_min": min(objectives),
            "objective_max": max(objectives),
            "objective_mean": statistics.fmean(objectives),
            "objective_stdev": (statistics.stdev(objectives)
                                if len(objectives) > 1 else 0.0),
            "relative_spread": ((max(objectives) - min(objectives)) / min(objectives)
                                if min(objectives) else None),
        })
    return summary


def experiment_constraint_cases(request) -> dict:
    """约束场景：时间过紧、母线功率上限过紧、母线侧无依据。"""
    cases = {}

    tight = dataclasses.replace(request, remaining_time_s=600.0)
    run, result = run_speed_optimization(
        tight, scenario=_scenario(tight),
        config=DEConfig(population=16, max_generations=6, seeds=(1,)),
        call_id="tspeed-tight-time")
    cases["time_too_tight"] = {
        "outcome_code": run.outcome_code,
        "search_status": result.search_status,
        "selected_candidate_id": result.selected_candidate_id,
        "diagnostic_codes": [item.code for item in run.diagnostics],
    }

    over_power = _scenario(request, limit_kw=1.0)
    run, result = run_speed_optimization(
        request, scenario=over_power,
        config=DEConfig(population=16, max_generations=6, seeds=(1,)),
        call_id="tspeed-power-limit")
    cases["bus_power_limit_too_low"] = {
        "outcome_code": run.outcome_code,
        "search_status": result.search_status,
        "selected_candidate_id": result.selected_candidate_id,
        "diagnostic_codes": [item.code for item in run.diagnostics],
    }

    motor_only = _scenario(request, demand=False)
    run, result = run_speed_optimization(
        request, scenario=motor_only,
        config=DEConfig(population=16, max_generations=6, seeds=(1,)),
        call_id="tspeed-motor-only")
    cases["motor_side_only"] = {
        "outcome_code": run.outcome_code,
        "search_status": result.search_status,
        "bus_side_available": result.bus_side_available,
        "objective_energy_scope": (result.objective_terms.energy_scope
                                   if result.objective_terms else None),
        "assumptions": list(result.assumptions),
    }
    return cases


def experiment_rolling(request) -> dict:
    """滚动重规划：只剩后半程、真实当前速度；已执行部分不回滚。

    剩余期限按剩余航程折算（对地航速下限下所需时间的 1.8 倍），
    而不是沿用整段航程的期限。
    """
    remaining = dataclasses.replace(request, segments=request.segments[8:],
                                    current_speed_kmh=7.5)
    distance_left = sum(segment.distance_km for segment in remaining.segments)
    low_speed = max(remaining.time_grid.min_speed_kmh, 3.0)
    budget_s = distance_left / low_speed * 3600.0 * 1.8
    remaining = dataclasses.replace(remaining, remaining_time_s=budget_s)
    run, result = run_speed_optimization(
        remaining, scenario=_scenario(remaining),
        config=DEConfig(population=24, max_generations=20, seeds=(20260925,)),
        call_id="tspeed-rolling")
    first_interval = (result.selected_trajectory[0] if result.selected_trajectory
                      else None)
    return {
        "outcome_code": run.outcome_code,
        "search_status": result.search_status,
        "segments_replanned": [segment.segment_id for segment in remaining.segments],
        "remaining_distance_km": distance_left,
        "remaining_time_budget_s": budget_s,
        "selected_speed_vector": list(result.selected_speed_vector),
        "total_duration_s": result.total_duration_s,
        "total_energy_kwh": result.total_energy_kwh,
        "first_interval": first_interval,
        "note": ("时间轴自当前位置重新起算；已执行航段不在请求内；"
                 "首段入口速度取真实当前速度 7.5 km/h"),
    }


def experiment_paper_configuration_timing(request) -> dict:
    """论文复现配置的耗时外推（按实测单次评估耗时计算，不实际跑满）。"""
    config = DEConfig(population=PAPER["population"],
                      max_generations=PAPER["max_generations"],
                      mutation_factor=PAPER["mutation_factor"],
                      crossover_rate=PAPER["crossover_rate"],
                      seeds=(1,))
    probe_config = DEConfig(population=20, max_generations=5, seeds=(1,))
    run, result = run_speed_optimization(request, scenario=_scenario(request),
                                         config=probe_config,
                                         call_id="tspeed-timing-probe")
    ms_per_eval = result.runtime_ms / result.evaluations
    return {
        "probe_evaluations": result.evaluations,
        "probe_runtime_ms": result.runtime_ms,
        "ms_per_evaluation": ms_per_eval,
        "paper_max_evaluations": config.max_evaluations,
        "paper_projected_runtime_s": ms_per_eval * config.max_evaluations / 1000.0,
        "paper_projected_runtime_h": ms_per_eval * config.max_evaluations / 3.6e6,
        "paper_config": PAPER,
        "practical_config": PRACTICAL,
        "note": ("按当前纯 Python 后端外推；正式 LSTM 批量推理耗时需另行实测。"
                 "1800s 滚动周期是否可行须以实测耗时为准"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tspeed DE 验收实验")
    parser.add_argument("--output-dir", default="artifacts/tspeed-v2")
    parser.add_argument("--population", type=int, default=PRACTICAL["population"])
    parser.add_argument("--generations", type=int,
                        default=PRACTICAL["max_generations"])
    parser.add_argument("--small-segments", type=int, default=4)
    args = parser.parse_args(argv)

    request = f.request()
    small = dataclasses.replace(request,
                                segments=request.segments[:args.small_segments],
                                current_speed_kmh=7.0,
                                speed_penalty_lambda=0.0)
    started = time.perf_counter()
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": sys.version,
        "platform": platform.platform(),
        "fixture_hash": stable_hash(request.to_dict()),
        "scenario": "scenario.de-acceptance（豫交投001 17 航段研究情景）",
        "evidence_boundary": (
            "验收用解析功率函数 P=c·v³；母线侧效率与辅助负载为具名研究假设；"
            "不是实船实测，也不代表 SOC 安全"),
        "experiments": {},
    }

    print("1/7 精确最优对照（小规模 DP + DE）…")
    report["experiments"]["optimality_small_case"] = experiment_optimality(small)
    print("2/7 完整航程 DE 求解…")
    report["experiments"]["full_voyage"] = experiment_full_voyage(
        request, population=args.population, generations=args.generations)
    print("3/7 多随机种子稳定性…")
    report["experiments"]["multi_seed"] = experiment_multi_seed(
        request, population=24, generations=20)
    print("4/7 约束场景…")
    report["experiments"]["constraint_cases"] = experiment_constraint_cases(request)
    print("5/7 滚动重规划…")
    report["experiments"]["rolling_replan"] = experiment_rolling(request)
    print("6/7 论文配置耗时外推…")
    report["experiments"]["paper_configuration_timing"] = \
        experiment_paper_configuration_timing(request)
    print("7/7 基线对照（规则航速 vs DE）…")
    report["experiments"]["baseline_comparison"] = experiment_baseline(request)

    report["total_runtime_s"] = time.perf_counter() - started
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    path = output / "tspeed_acceptance.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"报告已写入 {path}")
    return 0


def experiment_baseline(request) -> dict:
    """与规则航速基线对照：同一硬约束、同一能量口径下比较。

    为了让"节能"比较公平，额外运行一次 **λ=0（纯最小能耗）** 的 DE，
    与最优规则基线在同一口径下比较；λ>0 的运行只用于说明平滑性取舍，
    不参与节能率计算。
    """
    from research.tspeed.eval import DEEvaluationContext, evaluate_candidate

    spec = f.model_spec(grid=request.time_grid, demand_spec=f.demand_spec())
    context = DEEvaluationContext(
        request=request, spec=spec, environment_ref=spec.environment_ref,
        history_ref=spec.history_ref, config_hash="baseline")
    low_vector = tuple(
        max(request.time_grid.min_speed_kmh,
            segment.min_speed_kmh if segment.min_speed_kmh is not None
            else request.time_grid.min_speed_kmh)
        for segment in request.segments if segment.optimizable)
    high_vector = tuple(
        min(request.time_grid.max_speed_kmh,
            segment.max_speed_kmh if segment.max_speed_kmh is not None
            else request.time_grid.max_speed_kmh)
        for segment in request.segments if segment.optimizable)
    mid_vector = tuple((low + high) / 2.0 for low, high in zip(low_vector, high_vector))
    baselines = {}
    for name, vector in (("all_min_speed", low_vector),
                         ("all_max_speed", high_vector),
                         ("all_mid_speed", mid_vector)):
        evaluation = evaluate_candidate(vector, context=context)
        baselines[name] = {
            "speed_vector": [round(value, 6) for value in vector],
            "feasible": evaluation.feasible,
            "violations": list(evaluation.violations),
            "energy_kwh": evaluation.energy_kwh,
            "objective": evaluation.objective_value,
            "duration_s": evaluation.total_duration_s,
            "motor_energy_kwh": evaluation.energy_kwh,
            "demand_bus_energy_kwh": evaluation.demand_energy_kwh,
        }

    runs = {}
    for name, lambda_value in (("de_min_energy", 0.0),
                               ("de_with_penalty_10", 10.0)):
        tuned = dataclasses.replace(request, speed_penalty_lambda=lambda_value)
        run, result = run_speed_optimization(
            tuned, scenario=_scenario(tuned),
            config=DEConfig(population=24, max_generations=20, seeds=(20260925,)),
            call_id=f"tspeed-baseline-{name}")
        runs[name] = {
            "speed_vector": [round(value, 6)
                             for value in result.selected_speed_vector],
            "feasible": result.search_status == "candidate_found",
            "energy_kwh": result.total_energy_kwh,
            "objective": (result.objective_terms.total
                          if result.objective_terms else None),
            "penalty": (result.objective_terms.penalty
                        if result.objective_terms else None),
            "duration_s": result.total_duration_s,
            "motor_energy_kwh": result.total_energy_kwh,
            "demand_bus_energy_kwh": None,
            "runtime_ms": result.runtime_ms,
            "evaluations": result.evaluations,
        }
    # 对照必须同口径，否则会得出误导性数字。分两块，各自内部严格同口径：
    # (a) 能量口径：同一电池/电机能量 kWh 对 kWh，与 λ 无关；
    # (b) 目标口径：把"双方各自的最优解"放到**同一个 λ** 下评估后比较，
    #     这样差异只能来自解本身，而不是来自两边用了不同的 λ。
    # 早期版本拿 DE 的 λ=0 目标（不含惩罚）去比基线的 λ=10 目标，不对等。
    comparison = None
    if runs["de_min_energy"]["feasible"]:
        candidates = [(key, item) for key, item in baselines.items()
                      if item["feasible"] and item["objective"] is not None]
        if candidates:
            best_key, best = min(candidates, key=lambda pair: pair[1]["objective"])
            energy_improvement = None
            if best["energy_kwh"] and runs["de_min_energy"]["energy_kwh"]:
                energy_improvement = (
                    (best["energy_kwh"] - runs["de_min_energy"]["energy_kwh"])
                    / best["energy_kwh"])

            # 同一 λ 下重算双方最优解的目标值
            shared_lambda = 10.0  # 与 de_with_penalty_10 同一取值
            shared_request = dataclasses.replace(request,
                                                 speed_penalty_lambda=shared_lambda)
            shared_context = DEEvaluationContext(
                request=shared_request, spec=spec,
                environment_ref=spec.environment_ref,
                history_ref=spec.history_ref, config_hash="fair-comparison")
            de_solution = evaluate_candidate(
                tuple(runs["de_min_energy"]["speed_vector"]),
                context=shared_context)
            rule_solution = evaluate_candidate(
                tuple(best["speed_vector"]), context=shared_context)
            objective_improvement = None
            if de_solution.objective_value is not None \
                    and rule_solution.objective_value is not None:
                objective_improvement = (
                    (rule_solution.objective_value - de_solution.objective_value)
                    / rule_solution.objective_value)
            comparison = {
                "energy_scope_comparison": {
                    "de_energy_kwh": runs["de_min_energy"]["energy_kwh"],
                    "best_rule_energy_kwh": best["energy_kwh"],
                    "relative_energy_difference": energy_improvement,
                    "meaning": "同一电机侧/母线能量口径下的能量差（与 λ 无关）",
                },
                "objective_scope_comparison": {
                    "shared_lambda": shared_lambda,
                    "de_solution_objective_at_shared_lambda":
                        de_solution.objective_value,
                    "de_solution_energy_at_shared_lambda": de_solution.energy_kwh,
                    "de_solution_penalty_at_shared_lambda": de_solution.penalty,
                    "rule_solution_objective_at_shared_lambda":
                        rule_solution.objective_value,
                    "rule_solution_energy_at_shared_lambda": rule_solution.energy_kwh,
                    "rule_solution_penalty_at_shared_lambda": rule_solution.penalty,
                    "relative_objective_difference": objective_improvement,
                    "meaning": ("把双方各自的最优解放到同一 λ 下评估后比较；"
                                "差异可能完全来自平滑惩罚项，不等于节能"),
                },
                "best_rule_baseline": best_key,
                "de_speed_vector": runs["de_min_energy"]["speed_vector"],
                "best_rule_speed_vector": best["speed_vector"],
                "note": ("两处差异都只描述当前验收用解析模型的模型内比较，"
                         "不是实船节能率。实际采用哪个 λ 由项目负责人裁决，"
                         "本工具不预设"),
            }
    return {"baselines": baselines, "de_runs": runs, "comparison": comparison}


if __name__ == "__main__":
    raise SystemExit(main())
