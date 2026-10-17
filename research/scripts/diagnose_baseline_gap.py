"""诊断脚本（PR #11 完善任务 1）：定位 DE 与规则基线的差异来源。

要求（工具构建完善要求.md 三.1）：
让 DE 解和规则基线经过同一轨迹展开、相同功率后端、同一目标函数及独立约束检查。
报告名义速度与实际展开速度、各项约束、能量及平滑惩罚分项、多随机种子和搜索预算。
明确差异来自搜索收敛、边界修复、限速夹取、目标尺度还是基线口径。

用法::

    python -m research.scripts.diagnose_baseline_gap
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import statistics
from pathlib import Path

from research.tests import fixtures as f
from research.tspeed import DEConfig
from research.tspeed.eval import DEEvaluationContext, evaluate_candidate
from research.tspeed.search import effective_bounds, run_differential_evolution
from research.tspeed.trajectory import expand_trajectory


def _expanded_speeds(request, vector):
    """把决策向量展开，报告每段的实际巡航速度与入口/出口速度。"""
    expanded = expand_trajectory(vector, request=request, max_step_s=60.0)
    rows = []
    for fact in expanded.segment_facts:
        rows.append({
            "segment_id": fact.segment_id,
            "nominal_cruise_kmh": fact.cruise_speed_kmh,
            "actual_entry_kmh": fact.entry_speed_kmh,
            "actual_exit_kmh": fact.exit_speed_kmh,
            "sailing_duration_s": fact.sailing_duration_s,
            "waiting_duration_s": fact.waiting_duration_s,
            "distance_km": fact.sailing_distance_km,
            "distance_residual_km": fact.distance_residual_km,
            "phase_kinds": [phase.kind for phase in fact.phases],
        })
    return expanded, rows


def _rule_vectors(request):
    """三种规则基线：各段下限 / 上限 / 中值（只对可优化航段取，固定航段不变）。"""
    segments = [segment for segment in request.segments if segment.optimizable]
    grid = request.time_grid
    lows, highs = [], []
    for segment in segments:
        low = max(grid.min_speed_kmh,
                  segment.min_speed_kmh if segment.min_speed_kmh is not None
                  else grid.min_speed_kmh)
        high = min(grid.max_speed_kmh,
                   segment.max_speed_kmh if segment.max_speed_kmh is not None
                   else grid.max_speed_kmh)
        lows.append(low)
        highs.append(high)
    return {
        "all_min_speed": tuple(lows),
        "all_max_speed": tuple(highs),
        "all_mid_speed": tuple((low + high) / 2.0 for low, high in zip(lows, highs)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="定位 DE 与规则基线的差异")
    parser.add_argument("--output-dir", default="artifacts/tspeed-v2")
    parser.add_argument("--population", type=int, default=24)
    parser.add_argument("--generations", type=int, default=20)
    parser.add_argument("--seeds", default="20260925,1,7,42,2026")
    args = parser.parse_args(argv)
    seeds = tuple(int(item) for item in args.seeds.split(","))

    request = f.request()
    spec = f.model_spec(grid=request.time_grid, demand_spec=f.demand_spec())
    bounds = effective_bounds(request)
    segments = [segment for segment in request.segments if segment.optimizable]
    report: dict = {
        "scenario": "豫交投001 17 航段（12 段可优化）",
        "backend": spec.predictor.model_version,
        "objective_lambda": request.speed_penalty_lambda,
        "search_budget": {"population": args.population,
                          "generations": args.generations, "seeds": list(seeds)},
        "effective_bounds": [
            {"segment_id": segment.segment_id,
             "own_min_kmh": segment.min_speed_kmh,
             "own_max_kmh": segment.max_speed_kmh,
             "effective_min_kmh": low, "effective_max_kmh": high}
            for segment, (low, high) in zip(segments, bounds)],
        "rule_baselines": {},
        "de_runs": {},
        "verdict": {},
    }

    # --- 规则基线：全部经同一评估链（展开 → 预测 → 需求 → 硬约束 → 目标） ---
    context = DEEvaluationContext(
        request=request, spec=spec, environment_ref=spec.environment_ref,
        history_ref=spec.history_ref, config_hash="diagnose-rule")
    for name, vector in _rule_vectors(request).items():
        evaluation = evaluate_candidate(vector, context=context)
        expanded, rows = _expanded_speeds(request, vector)
        # 名义速度 vs 实际展开速度（限速夹取的度量）
        nominal = list(vector)
        actual = [fact.cruise_speed_kmh for fact in expanded.segment_facts
                  if fact.segment_id in {s.segment_id for s in segments}]
        clamped = [round(n - (a if a is not None else n), 6)
                   for n, a in zip(nominal, actual)]
        report["rule_baselines"][name] = {
            "nominal_vector": [round(value, 6) for value in nominal],
            "actual_cruise_kmh": [None if value is None else round(value, 6)
                                  for value in actual],
            "clamped_delta_kmh": clamped,
            "feasible": evaluation.feasible,
            "violations": list(evaluation.violations),
            "energy_kwh": evaluation.energy_kwh,
            "penalty": evaluation.penalty,
            "objective": evaluation.objective_value,
            "duration_s": evaluation.total_duration_s,
            "motor_energy_kwh": evaluation.energy_kwh,
            "demand_bus_energy_kwh": evaluation.demand_energy_kwh,
            "expansion_violations": list(expanded.violation_codes),
            "segment_facts": rows,
        }

    # --- DE：多种子，同一评估链 ---
    objectives = []
    for seed in seeds:
        config = DEConfig(population=args.population,
                          max_generations=args.generations, seeds=(seed,))
        search = run_differential_evolution(
            request, spec=spec, environment_ref=spec.environment_ref,
            history_ref=spec.history_ref, config=config)
        best = search.best
        entry = {
            "seed": seed,
            "found": best is not None,
            "evaluations": search.evaluations,
            "runtime_ms": search.runtime_ms,
            "generations_run": search.trace.generations_run,
            "feasible_fraction_final": (
                search.trace.feasible_fraction_by_generation[-1]
                if search.trace.feasible_fraction_by_generation else None),
            "violation_counts": search.trace.violation_counts,
            "cache_hits": search.trace.cache_hits,
        }
        if best is not None:
            objectives.append(best.objective_value)
            entry.update({
                "nominal_vector": [round(v, 6) for v in best.speed_vector],
                "energy_kwh": best.energy_kwh,
                "penalty": best.penalty,
                "objective": best.objective_value,
                "duration_s": best.total_duration_s,
                "constraint_violations": list(best.violations),
                "expansion_violations": list(best.expanded.violation_codes),
                "distinct_candidates_evaluated": len(search.context.cache),
            })
        report["de_runs"][f"seed_{seed}"] = entry

    # --- 判定：差异属于哪一类 ---
    best_rule_name, best_rule = min(
        ((name, item) for name, item in report["rule_baselines"].items()
         if item["feasible"] and item["objective"] is not None),
        key=lambda pair: pair[1]["objective"])
    de_best = min(objectives) if objectives else None
    verdict: dict = {
        "best_rule_baseline": best_rule_name,
        "best_rule_objective": best_rule["objective"],
        "best_rule_energy_kwh": best_rule["energy_kwh"],
        "best_rule_penalty": best_rule["penalty"],
        "de_best_objective": de_best,
        "de_best_energy_kwh": None,
        "relative_gap": (None if de_best is None
                         else (de_best - best_rule["objective"])
                         / best_rule["objective"]),
        "seed_spread": (None if len(objectives) < 2
                        else (max(objectives) - min(objectives)) / min(objectives)),
        "seed_stdev": (statistics.stdev(objectives)
                       if len(objectives) > 1 else 0.0),
        "clamped_segments_in_rule": [segment_id for segment_id, delta
                                     in zip([s.segment_id for s in segments],
                                            best_rule["clamped_delta_kmh"])
                                     if abs(delta) > 1e-9],
    }
    # DE 是否能到达规则基线解？（把规则向量放进同一评估上下文里比较）
    rule_evaluation = evaluate_candidate(
        tuple(best_rule["nominal_vector"]), context=context)
    verdict["rule_vector_feasible_inside_de_context"] = rule_evaluation.feasible
    verdict["rule_vector_objective"] = rule_evaluation.objective_value
    verdict["rule_vector_in_effective_bounds"] = all(
        low - 1e-9 <= value <= high + 1e-9
        for value, (low, high) in zip(best_rule["nominal_vector"], bounds))
    report["verdict"] = verdict

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    path = output / "baseline_gap_diagnosis.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                    encoding="utf-8")

    print(f"规则基线最优：{best_rule_name}  目标 {best_rule['objective']:.4f}"
          f"（能量 {best_rule['energy_kwh']:.4f} + 惩罚 {best_rule['penalty']:.4f}）")
    print(f"DE 最优：      {de_best if de_best is None else round(de_best, 4)}"
          f"  多种子离散 {verdict['seed_spread']}")
    print(f"相对差距：     {verdict['relative_gap']}")
    print(f"规则向量落在 DE 有效边界内：{verdict['rule_vector_in_effective_bounds']}")
    print(f"规则向量在 DE 上下文里可行：{verdict['rule_vector_feasible_inside_de_context']}")
    print(f"规则解被夹取的段：{verdict['clamped_segments_in_rule']}")
    print(f"报告已写入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
