"""工具入口验收：v2 结果工件、失败语义、FDP 反馈消费与独立核验。"""

from __future__ import annotations

import dataclasses
import json

from research.contracts import (
    ManagementFeedback,
    OutcomeCode,
    SourceRef,
    stable_hash,
)
from research.tests import fixtures as f
from research.tspeed import DEConfig
from research.tspeed.tool import SimulationScenario, apply_management_feedback, run_speed_optimization


def _scenario(*, demand: bool = True, demand_limit_kw: float | None = None,
              energy_budget_kwh: float | None = None, grid=None) -> SimulationScenario:
    active = grid or f.time_grid()
    spec = f.model_spec(grid=active,
                        demand_spec=f.demand_spec() if demand else None,
                        demand_limit_kw=demand_limit_kw,
                        energy_budget_kwh=energy_budget_kwh)
    return SimulationScenario(
        scenario_id="scenario.de-acceptance",
        environment_ref="scenario.env-001",
        history_ref="history.ref-001",
        model_spec=spec,
        source_refs=(f.DOC,),
        assumptions=("功率曲线为验收用解析函数",),
        note="验收情景")


def _small_request(**kwargs):
    request = f.request(**kwargs)
    return dataclasses.replace(request, segments=request.segments[:5],
                               current_speed_kmh=7.0)


def test_success_returns_v2_result_and_envelope():
    request = _small_request()
    config = DEConfig(population=14, max_generations=8, seeds=(101,))
    run, result = run_speed_optimization(request, scenario=_scenario(),
                                        config=config, call_id="call-1")
    assert run.outcome is OutcomeCode.OK
    assert run.execution_status == "completed"
    assert result.search_status == "candidate_found"
    assert result.selected_candidate_id
    assert result.selected_speed_vector
    assert result.constraint_checks, "必须给出逐项检查"
    assert result.all_checks_passed, "选定方案必须全部检查通过"
    assert result.objective_terms is not None
    assert result.objective_terms.energy_scope in ("motor", "bus")
    assert result.segment_summary, "必须给出逐航段汇总"
    assert result.search_trace is not None
    assert result.seed == 101
    assert result.generations >= 1
    assert result.evaluations > 0
    assert result.runtime_ms > 0.0
    # 信封字段完整
    payload = json.loads(run.to_json())
    assert payload["call_id"] == "call-1"
    assert payload["snapshot_id"] == request.snapshot_id
    assert payload["schema_version"]


def test_selected_plan_reports_bus_side_when_available():
    request = _small_request()
    config = DEConfig(population=14, max_generations=8, seeds=(103,))
    _, result = run_speed_optimization(request, scenario=_scenario(demand=True),
                                       config=config)
    assert result.bus_side_available is True
    assert result.selected_demand_ref
    assert result.demand_config_hash
    assert result.objective_terms.energy_scope == "bus"
    names = {check.name for check in result.constraint_checks}
    assert {"time", "speed", "acceleration", "distance",
            "energy_conservation"} <= names


def test_motor_only_scenario_is_labelled_and_not_claimed_safe():
    request = _small_request()
    config = DEConfig(population=12, max_generations=6, seeds=(107,))
    scenario = _scenario(demand=False)
    _, result = run_speed_optimization(request, scenario=scenario, config=config)
    assert result.bus_side_available is False
    assert result.selected_demand_ref is None
    assert result.objective_terms.energy_scope == "motor"
    assert any("电机侧对照实验" in note for note in result.assumptions), \
        "母线侧无依据时必须如实标注，不得宣称 SOC 安全"


def test_unsupported_objective_returns_not_supported():
    """契约层已拒绝未实现目标；这里核验工具层的同一条失败语义。"""
    from research.contracts import SegmentConstraint, SpeedOptimizationRequest

    try:
        SpeedOptimizationRequest(segments=(SegmentConstraint(
            segment_id="S01", distance_km=5.0, min_speed_kmh=4.0, max_speed_kmh=12.0),),
            time_grid=f.time_grid(), objective="min_cost")
    except ValueError:
        pass
    else:
        raise AssertionError("契约层必须拒绝未实现目标")

    request = _small_request()
    object.__setattr__(request, "objective", "min_cost")  # 绕过构造校验以核验工具层
    config = DEConfig(population=8, max_generations=2, seeds=(109,))
    run, result = run_speed_optimization(request, scenario=_scenario(), config=config)
    assert run.outcome is OutcomeCode.NOT_SUPPORTED
    assert run.execution_status == "completed", "不支持是明确的工程结果，不是运行异常"
    assert result.search_status == "input_invalid"
    assert {item.code for item in run.diagnostics} >= {"objective_not_supported"}


def test_deadline_failure_returns_search_unresolved():
    request = _small_request(remaining_time_s=900.0)
    config = DEConfig(population=12, max_generations=5, seeds=(113,))
    run, result = run_speed_optimization(request, scenario=_scenario(), config=config)
    assert run.outcome is OutcomeCode.SEARCH_UNRESOLVED
    assert run.execution_status == "completed", "工程结果不等于执行失败"
    assert result.search_status == "search_unresolved"
    assert result.selected_candidate_id is None
    assert result.search_trace is not None
    assert result.search_trace.diagnostic_candidates, \
        "未解决时必须保留诊断候选，但不得放入选定字段"
    codes = {item.code for item in run.diagnostics}
    assert "search_unresolved" in codes
    detail = " ".join(item.detail or "" for item in run.diagnostics)
    assert "无解" not in detail.replace("不得表述为数学无解", ""), \
        "不得把搜索失败表述为数学无解"


def test_verification_failure_blocks_executable_plan():
    """母线功率上限过紧：独立核验必须拦下候选，不能返回可执行方案。"""
    request = _small_request()
    config = DEConfig(population=12, max_generations=6, seeds=(127,))
    scenario = dataclasses.replace(_scenario(demand_limit_kw=1.0),
                                   model_spec=f.model_spec(
                                       grid=request.time_grid,
                                       demand_spec=f.demand_spec(),
                                       demand_limit_kw=1.0))
    run, result = run_speed_optimization(request, scenario=scenario, config=config)
    assert run.outcome in (OutcomeCode.SEARCH_UNRESOLVED,
                           OutcomeCode.INFEASIBLE_UNDER_ASSUMPTIONS)
    assert result.selected_candidate_id is None, \
        "不可执行候选不得放入选定方案字段"


def test_feedback_version_mismatch_is_rejected():
    request = _small_request()
    context = dataclasses.replace(request, demand_builder_ref="demand-OTHER")
    feedback = ManagementFeedback(
        selected_candidate_id="c1", selected_demand_ref="demand-EXPECTED",
        battery_config_hash="h", failure_scope="power_upper_bound_provable",
        violated_rule="bus_power_limit", deliverable_kw=400.0,
        limit_source_ref=SourceRef(source_id="doc.bus", kind="document"))
    config = DEConfig(population=10, max_generations=3, seeds=(131,))
    run, _ = run_speed_optimization(context, scenario=_scenario(), config=config,
                                    fdp_feedback=feedback)
    assert run.outcome is OutcomeCode.INVALID_INPUT
    assert {item.code for item in run.diagnostics} >= {"feedback_version_mismatch"}


def test_provable_power_limit_becomes_hard_constraint():
    request = _small_request()
    spec = f.model_spec(grid=request.time_grid, demand_spec=f.demand_spec())
    feedback = ManagementFeedback(
        selected_candidate_id="c1", selected_demand_ref="demand-1",
        battery_config_hash="h", failure_scope="power_upper_bound_provable",
        violated_rule="bus_power_limit", deliverable_kw=420.0,
        limit_source_ref=SourceRef(source_id="doc.bus", kind="document",
                                   confirmed=True))
    updated, diagnostics, notes = apply_management_feedback(request, feedback,
                                                            spec=spec)
    assert updated.demand_limit_kw == 420.0
    assert notes and "硬约束" in notes[0]
    assert {item.code for item in diagnostics} >= {"hard_power_limit_applied"}


def test_soc_path_feedback_is_not_a_hard_constraint():
    request = _small_request()
    spec = f.model_spec(grid=request.time_grid, demand_spec=f.demand_spec())
    feedback = ManagementFeedback(
        selected_candidate_id="c1", selected_demand_ref="demand-1",
        battery_config_hash="h", failure_scope="soc_path_dependent",
        violated_rule="soc_min", soc_1=0.19, soc_2=0.25)
    updated, diagnostics, notes = apply_management_feedback(request, feedback,
                                                            spec=spec)
    assert updated.demand_limit_kw is None, \
        "SOC 路径相关失败不得被简化为对所有新轨迹成立的功率上限"
    assert {item.code for item in diagnostics} >= {"feedback_not_hard_constraint"}
    assert notes


def test_budget_tight_feedback_becomes_hard_constraint():
    request = _small_request()
    spec = f.model_spec(grid=request.time_grid, demand_spec=f.demand_spec(),
                        demand_limit_kw=900.0)
    feedback = ManagementFeedback(
        selected_candidate_id="c1", selected_demand_ref="demand-1",
        battery_config_hash="h", failure_scope="power_upper_bound_provable",
        violated_rule="bus_power_limit", deliverable_kw=300.0,
        limit_source_ref=SourceRef(source_id="doc.bus", kind="document"))
    updated, _, _ = apply_management_feedback(request, feedback, spec=spec)
    assert updated.demand_limit_kw == 300.0, "新上限应取更严格者"


def test_arrival_time_is_null_without_absolute_origin():
    request = _small_request()
    patched = dataclasses.replace(request, planning_origin_at=None)
    config = DEConfig(population=12, max_generations=5, seeds=(137,))
    _, result = run_speed_optimization(patched, scenario=_scenario(), config=config)
    assert result.arrival_at is None, "无绝对基准时不得给出日历 ETA"
    assert result.total_duration_s is not None, "相对历时仍可给出"


def test_call_id_and_config_hash_are_reproducible():
    request = _small_request()
    scenario = _scenario()
    config = DEConfig(population=12, max_generations=5, seeds=(139,))
    first_run, _ = run_speed_optimization(request, scenario=scenario, config=config)
    second_run, _ = run_speed_optimization(request, scenario=scenario, config=config)
    assert scenario.config_hash() == scenario.config_hash()
    assert first_run.config_hash == second_run.config_hash
    assert first_run.input_hash == stable_hash(request.to_dict())


def test_resampling_difference_is_reported():
    grid = f.time_grid(prediction_step_s=30.0, verification_step_s=5.0)
    request = _small_request(grid=grid)
    config = DEConfig(population=12, max_generations=5, seeds=(149,))
    run, result = run_speed_optimization(request, scenario=_scenario(grid=grid),
                                         config=config)
    assert run.outcome is OutcomeCode.OK
    codes = {item.code for item in run.diagnostics}
    assert "resampling_difference" in codes, \
        "粗规划网格与核验精度之间必须报告能量差"
    delta = next(item.observed for item in run.diagnostics
                 if item.code == "resampling_difference")
    assert isinstance(delta, float)
