"""生成联调样例 JSON（合法/非法）并给出说明。

样例由本仓库代码现场生成，避免手写数字与实现脱节：

* ``valid/``：可直接被 v2 契约接受；
* ``invalid/``：故意违反一条约定，附 ``_violation`` 说明该例证明了什么。

用法::

    python -m research.scripts.make_samples --output-dir research/schemas/examples
"""

from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path

from research.contracts import (
    EnergyBatchRequest,
    ManagementFeedback,
    SourceRef,
    SourceRef as _SourceRef,
)
from research.tests import fixtures as f
from research.tspeed import DEConfig
from research.tspeed.eval import DEEvaluationContext, evaluate_candidate
from research.tspeed.search import candidate_trajectory
from research.tspeed.tool import SimulationScenario, run_speed_optimization

DOC = SourceRef(source_id="doc.豫交投001参数", kind="document",
                locator="docs/团队资料/04_船舶与航线/豫交投001参数.docx",
                confirmed=True)


def _small_request(segments: int = 5):
    request = f.request()
    return dataclasses.replace(request, segments=request.segments[:segments],
                               current_speed_kmh=7.0)


def _scenario(request, *, demand: bool = True, limit_kw: float | None = None):
    spec = f.model_spec(grid=request.time_grid,
                        demand_spec=f.demand_spec() if demand else None,
                        demand_limit_kw=limit_kw)
    return SimulationScenario(
        scenario_id="scenario.de-acceptance",
        environment_ref=spec.environment_ref, history_ref=spec.history_ref,
        model_spec=spec, source_refs=(DOC,),
        assumptions=("功率曲线为验收用解析函数", "母线侧效率与辅助负载为具名研究假设"),
        note="联调样例")


def _dump_raw(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")


# 样例文件保留的时序区间条数：首尾各若干条，避免 0.9MB 的巨型 JSON 影响阅读。
SAMPLE_HEAD_TAIL = 6
_TIME_SERIES_KEYS = ("points", "power_points", "trajectory", "selected_trajectory")


def trim_series(payload, *, keep: int = SAMPLE_HEAD_TAIL, _depth: int = 0):
    """裁剪样例中的长篇时序，并在**列表内部**标注裁剪数量。

    标注不能作为"伪区间"混进时序数组（否则会破坏逐点字段结构），
    而是与数组并列的 ``<键名>_omitted_rows`` 字段。
    """
    if _depth > 8:
        return payload
    if isinstance(payload, list):
        return [trim_series(item, keep=keep, _depth=_depth + 1) for item in payload]
    if not isinstance(payload, dict):
        return payload
    result = {}
    for key, value in payload.items():
        if key in _TIME_SERIES_KEYS and isinstance(value, list) \
                and len(value) > 2 * keep:
            omitted = len(value) - 2 * keep
            result[key] = ([trim_series(item, keep=keep, _depth=_depth + 1)
                            for item in value[:keep]]
                           + [trim_series(item, keep=keep, _depth=_depth + 1)
                              for item in value[-keep:]])
            result[f"{key}_omitted_rows"] = omitted
            result[f"{key}_sample_note"] = (
                "样例裁剪：只保留首尾各若干条；完整时序见运行产出的工件，"
                "不得用省略明细的总计数冒充完整过程")
            continue
        result[key] = trim_series(value, keep=keep, _depth=_depth + 1)
    return result


def _dump_sample(path: Path, payload: object, *, trim: bool = True) -> None:
    _dump_raw(path, trim_series(payload) if trim else payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成联调样例 JSON")
    parser.add_argument("--output-dir", default="research/schemas/examples")
    args = parser.parse_args(argv)
    out = Path(args.output_dir)
    request = _small_request()
    config = DEConfig(population=14, max_generations=8, seeds=(101,))

    # 1) 成功：候选 + 完整检查 + 母线需求引用
    run, result = run_speed_optimization(request, scenario=_scenario(request),
                                         config=config, call_id="sample-success")
    _dump_sample(out / "valid" / "speed_optimization_success.tool_run.json", run.to_dict())
    _dump_sample(out / "valid" / "speed_optimization_success.result.json", result.to_dict())

    # 2) 缺转换依据：母线侧为 null，只做电机侧对照实验
    run_motor, result_motor = run_speed_optimization(
        request, scenario=_scenario(request, demand=False),
        config=config, call_id="sample-motor-only")
    _dump_sample(out / "valid" / "speed_optimization_motor_only.result.json",
          result_motor.to_dict())

    # 3) FDP 拒绝：可证明的母线功率上限 → 硬约束
    over_power = _scenario(request, limit_kw=1.0)
    run_power, result_power = run_speed_optimization(
        request, scenario=over_power, config=config, call_id="sample-power-limit")
    _dump_sample(out / "valid" / "speed_optimization_search_unresolved.tool_run.json",
          run_power.to_dict())

    # 4) 需求工件（母线侧可用）+ 能量守恒自检
    from research.demand import build_power_demand, energy_balance_check
    from research.tspeed.profile import TrajectoryProfile

    spec = f.model_spec(grid=request.time_grid, demand_spec=f.demand_spec())
    context = DEEvaluationContext(
        request=request, spec=spec, environment_ref=spec.environment_ref,
        history_ref=spec.history_ref, config_hash="sample")
    vector = tuple(8.0 for _ in request.optimizable_segment_ids)
    evaluation = evaluate_candidate(vector, context=context)
    series = evaluation.demand
    _dump_sample(out / "valid" / "power_demand_series.json", series.to_dict())
    _dump_sample(out / "valid" / "power_demand_energy_balance.json",
          energy_balance_check(series))

    # 5) 批量预测请求与结果（Tenergy 联调夹具）
    from research.tspeed.predictor import AnalyticPowerPredictor

    candidate = candidate_trajectory(evaluation)
    batch_request = EnergyBatchRequest(
        candidates=(candidate,), history_ref=spec.history_ref,
        environment_scenario_ref=spec.environment_ref,
        model_version=spec.predictor.model_version,
        integration_rule=spec.predictor.integration_rule,
        call_id="sample-batch", snapshot_id=request.snapshot_id,
        scenario_id=request.scenario_id)
    batch_result = spec.predictor.predict(batch_request, grid=spec.grid)
    _dump_sample(out / "valid" / "energy_batch_request.json", batch_request.to_dict())
    _dump_sample(out / "valid" / "energy_batch_result.json", batch_result.to_dict())

    # 6) FDP 反馈（两类：可证明上限 / SOC 路径相关）
    _dump_sample(out / "valid" / "management_feedback_provable_limit.json",
          ManagementFeedback(
              selected_candidate_id=candidate.candidate_id,
              selected_demand_ref=series.series_id, battery_config_hash="cfg-hash-1",
              failure_scope="power_upper_bound_provable",
              violated_rule="bus_power_limit",
              first_failure_interval=(120.0, 180.0), demand_kw=520.0,
              deliverable_kw=480.0, soc_1=0.62, soc_2=0.58,
              limit_source_ref=DOC, diagnostic_certainty="provable_for_candidate_family",
              suggested_search_action="tighten_bus_power_limit",
              detail="母线总功率上限对候选族成立").to_dict())
    _dump_sample(out / "valid" / "management_feedback_soc_path.json",
          ManagementFeedback(
              selected_candidate_id=candidate.candidate_id,
              selected_demand_ref=series.series_id, battery_config_hash="cfg-hash-1",
              failure_scope="soc_path_dependent", violated_rule="soc_min",
              first_failure_interval=(3000.0, 3300.0), demand_kw=310.0,
              deliverable_kw=420.0, soc_1=0.19, soc_2=0.41,
              diagnostic_certainty="path_dependent",
              suggested_search_action="reject_candidate").to_dict())

    # 7) 非法样例：各证明一条约定
    invalid_success = run.to_dict()
    invalid_success["outcome_code"] = "ok"
    invalid_success["execution_status"] = "failed"
    invalid_success["_violation"] = ("outcome_code=ok 但 execution_status=failed："
                                     "执行状态与结果码映射不一致")
    _dump_sample(out / "invalid" / "tool_run_ok_but_failed.json", invalid_success)

    invalid_rule = batch_result.to_dict()
    if invalid_rule["results"]:
        invalid_rule["results"][0]["power_points"][0]["motor_energy_kwh"] = 999.0
    invalid_rule["_violation"] = ("motor_energy_kwh 与 predicted_motor_power_kw × "
                                  "duration_s/3600 不一致：预测区间与积分区间必须一致")
    _dump_sample(out / "invalid" / "energy_result_energy_rule_broken.json", invalid_rule)

    invalid_demand = series.to_dict()
    invalid_demand["bus_side_available"] = True
    invalid_demand["points"][0]["demand_bus_kw"] = None
    invalid_demand["_violation"] = ("bus_side_available=true 却存在 demand_bus_kw=null："
                                    "不得用 null 冒充可用母线需求")
    _dump_sample(out / "invalid" / "power_demand_null_but_available.json", invalid_demand)

    invalid_feedback = ManagementFeedback(
        selected_candidate_id="c1", selected_demand_ref="d1",
        battery_config_hash="h", failure_scope="soc_path_dependent",
        violated_rule="soc_min").to_dict()
    invalid_feedback["_violation"] = ("failure_scope=soc_path_dependent 却缺少 limit_source_ref："
                                      "SOC 路径相关失败不得被简化为对所有新轨迹成立的硬限制")
    _dump_sample(out / "invalid" / "management_feedback_soc_as_limit.json", invalid_feedback)

    print(f"样例已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
