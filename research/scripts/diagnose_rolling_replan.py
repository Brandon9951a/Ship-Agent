"""诊断脚本（PR #11 完善任务 2）：定位剩余 9 段滚动重规划的失败原因。

要求（工具构建完善要求.md 三.2）：固定剩余 9 段输入、当前速度、剩余时间和随机种子，
输出首个失败约束、违反统计、可行个体比例与诊断候选。若预算内仍未找到方案，
保持 search_unresolved，不声称数学无解。

用法::

    python -m research.scripts.diagnose_rolling_replan
"""

from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path

from research.tests import fixtures as f
from research.tspeed import DEConfig
from research.tspeed.search import effective_bounds, run_differential_evolution
from research.tspeed.trajectory import expand_trajectory, segment_limits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="定位滚动重规划失败")
    parser.add_argument("--output-dir", default="artifacts/tspeed-v2")
    parser.add_argument("--start-index", type=int, default=8,
                        help="从第几段开始作为剩余航程（0 基）")
    parser.add_argument("--current-speed", type=float, default=7.5)
    parser.add_argument("--population", type=int, default=24)
    parser.add_argument("--generations", type=int, default=20)
    parser.add_argument("--seeds", default="20260925,1,7,42,2026")
    args = parser.parse_args(argv)
    seeds = tuple(int(item) for item in args.seeds.split(","))

    base = f.request()
    remaining = dataclasses.replace(base, segments=base.segments[args.start_index:],
                                    current_speed_kmh=args.current_speed)
    distance_left = sum(segment.distance_km for segment in remaining.segments)
    low_speed = remaining.time_grid.min_speed_kmh
    budget_s = distance_left / max(low_speed, 1.0) * 3600.0 * 1.8
    remaining = dataclasses.replace(remaining, remaining_time_s=budget_s)

    spec = f.model_spec(grid=remaining.time_grid, demand_spec=f.demand_spec())
    segments = list(remaining.segments)
    grid = remaining.time_grid

    report: dict = {
        "input": {
            "segments": [segment.segment_id for segment in segments],
            "optimizable": list(remaining.optimizable_segment_ids),
            "current_speed_kmh": args.current_speed,
            "distance_km": distance_left,
            "remaining_time_s": budget_s,
            "remaining_time_h": budget_s / 3600.0,
            "seeds": list(seeds),
        },
        "segment_bounds": [
            {"segment_id": segment.segment_id,
             "fixed_speed_kmh": segment.fixed_speed_kmh,
             "node_min_kmh": segment.min_speed_kmh,
             "node_max_kmh": segment.max_speed_kmh,
             "effective_limits": segment_limits(segment, grid),
             "waiting_duration_s": segment.waiting_duration_s}
            for segment in segments],
        "runs": {},
    }

    # 边界求解是否本身就会报"约束冲突"
    try:
        bounds = effective_bounds(remaining)
        report["effective_bounds_ok"] = True
        report["effective_bounds"] = [
            {"segment_id": segment.segment_id, "low": low, "high": high}
            for segment, (low, high) in zip(
                [s for s in segments if s.optimizable], bounds)]
    except ValueError as exc:
        report["effective_bounds_ok"] = False
        report["effective_bounds_error"] = str(exc)

    any_found = False
    for seed in seeds:
        config = DEConfig(population=args.population,
                          max_generations=args.generations, seeds=(seed,))
        search = run_differential_evolution(
            remaining, spec=spec, environment_ref=spec.environment_ref,
            history_ref=spec.history_ref, config=config)
        run = search.runs[0]
        entry = {
            "found": search.best is not None,
            "evaluations": search.evaluations,
            "runtime_ms": search.runtime_ms,
            "generations_run": search.trace.generations_run,
            "violation_counts": dict(sorted(search.trace.violation_counts.items())),
            "feasible_fraction_by_generation": [
                round(value, 4)
                for value in search.trace.feasible_fraction_by_generation[:5]],
            "feasible_fraction_final": (
                search.trace.feasible_fraction_by_generation[-1]
                if search.trace.feasible_fraction_by_generation else None),
            "distinct_candidates": len(search.context.cache),
        }
        if search.best is not None:
            any_found = True
            entry["objective"] = search.best.objective_value
            entry["energy_kwh"] = search.best.energy_kwh
            entry["duration_s"] = search.best.total_duration_s
            entry["nominal_vector"] = [round(v, 4) for v in search.best.speed_vector]
            entry["violations"] = list(search.best.violations)
        else:
            # 首个失败约束 + 诊断候选明细
            first_codes = list(search.trace.violation_counts.keys())
            entry["first_failure_code"] = first_codes[0] if first_codes else None
            entry["diagnostic_candidates"] = [
                {
                    "candidate_id": item["candidate_id"],
                    "violations": item["violations"],
                    "objective": item["objective_value"],
                    "energy_kwh": item["energy_kwh"],
                    "duration_s": item["total_duration_s"],
                    "speed_vector": [round(v, 4) for v in item["speed_vector"]],
                }
                for item in search.trace.diagnostic_candidates[:5]]
            # 该候选为什么不可行：逐段展开事实
            if run.diagnostic_candidates:
                candidate = run.diagnostic_candidates[0]
                expanded = expand_trajectory(
                    candidate.speed_vector, request=remaining,
                    max_step_s=grid.verification_step_s)
                entry["worst_candidate_segments"] = [
                    {"segment_id": fact.segment_id,
                     "entry_kmh": round(fact.entry_speed_kmh, 4),
                     "exit_kmh": round(fact.exit_speed_kmh, 4),
                     "cruise_kmh": (None if fact.cruise_speed_kmh is None
                                    else round(fact.cruise_speed_kmh, 4)),
                     "sailing_duration_s": round(fact.sailing_duration_s, 2),
                     "waiting_duration_s": fact.waiting_duration_s,
                     "distance_residual_km": fact.distance_residual_km,
                     "phase_kinds": [phase.kind for phase in fact.phases]}
                    for fact in expanded.segment_facts]
                entry["worst_candidate_total_duration_s"] = expanded.total_duration_s
                entry["worst_candidate_violation_codes"] = list(expanded.violation_codes)
        report["runs"][f"seed_{seed}"] = entry

    report["conclusion"] = {
        "any_seed_found": any_found,
        "if_unresolved": ("保持 search_unresolved；不得表述为数学无解"
                          if not any_found else "本预算内找到方案"),
    }

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    path = output / "rolling_replan_diagnosis.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"剩余段: {report['input']['segments']}")
    print(f"剩余距离 {distance_left:.2f} km，期限 {budget_s/3600:.2f} h，"
          f"当前速度 {args.current_speed} km/h")
    for seed, entry in report["runs"].items():
        print(f"  {seed}: found={entry['found']} 违规统计="
              f"{entry['violation_counts']} 可行比例={entry['feasible_fraction_final']}")
        if not entry["found"]:
            print(f"      首个失败约束: {entry.get('first_failure_code')}")
            for item in entry.get("diagnostic_candidates", [])[:2]:
                print(f"      候选 {item['candidate_id']}: {item['violations'][:3]}")
    print(f"报告已写入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
