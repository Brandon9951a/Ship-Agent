"""契约层验收：字段边界、失败语义映射、序列化与哈希稳定性。"""

from __future__ import annotations

import json

from research.contracts import (
    CONTRACT_SCHEMA_VERSION,
    ArtifactRef,
    Diagnostic,
    EnergyBatchRequest,
    ExecutionStatus,
    ManagementFeedback,
    OutcomeCode,
    SourceRef,
    SpeedOptimizationResult,
    TimeGridSpec,
    ToolRun,
    execution_status_for,
    finish_run,
    iso_from_offset,
    plan_offset_seconds,
    search_status_for,
    stable_hash,
    start_run,
)
from research.tests import fixtures as f


def test_source_ref_kind_guard():
    try:
        SourceRef(source_id="x", kind="guess")
    except ValueError:
        return
    raise AssertionError("未知 kind 必须被拒绝")


def test_source_ref_assumption_flag():
    assumption = SourceRef(source_id="a", kind="assumption")
    document = SourceRef(source_id="d", kind="document")
    assert assumption.usable_as_evidence is False
    assert document.usable_as_evidence is True


def test_diagnostic_requires_stage_and_code():
    for field in ("stage", "code"):
        values = {"stage": "search", "code": "no_candidate"}
        values[field] = ""
        try:
            Diagnostic(**values)
        except ValueError:
            continue
        raise AssertionError(f"空 {field} 必须被拒绝")


def test_diagnostic_interval_order():
    Diagnostic(stage="s", code="c", time_interval_s=(0.0, 1.0))
    try:
        Diagnostic(stage="s", code="c", time_interval_s=(2.0, 1.0))
    except ValueError:
        return
    raise AssertionError("区间结束早于开始必须被拒绝")


def test_artifact_ref_hash_shape():
    ArtifactRef(artifact_id="a", sha256="0" * 64, row_count=3,
                schema_version=CONTRACT_SCHEMA_VERSION)
    try:
        ArtifactRef(artifact_id="a", sha256="abc", row_count=3,
                    schema_version=CONTRACT_SCHEMA_VERSION)
    except ValueError:
        return
    raise AssertionError("非 64 位十六进制哈希必须被拒绝")


def test_tool_run_outcome_mapping():
    assert execution_status_for(OutcomeCode.OK) is ExecutionStatus.COMPLETED
    assert execution_status_for(OutcomeCode.SEARCH_UNRESOLVED) \
        is ExecutionStatus.COMPLETED, "搜索未解决是工程结果，不是执行失败"
    assert execution_status_for(OutcomeCode.NEEDS_INPUT) is ExecutionStatus.COMPLETED, \
        "缺参是正常返回的工程结果，不是执行失败"
    assert execution_status_for(OutcomeCode.TOOL_ERROR) is ExecutionStatus.FAILED
    assert search_status_for(OutcomeCode.OK).value == "candidate_found"
    assert search_status_for(OutcomeCode.SEARCH_UNRESOLVED).value == "search_unresolved"
    assert search_status_for(OutcomeCode.MODEL_OUT_OF_DOMAIN).value == "model_out_of_domain"
    assert search_status_for(OutcomeCode.TOOL_ERROR).value == "tool_error"


def test_tool_run_ok_requires_payload():
    envelope = start_run("Tspeed", call_id="c1")
    try:
        finish_run(envelope, outcome=OutcomeCode.OK)
    except ValueError:
        return
    raise AssertionError("outcome=ok 缺少 payload 必须被拒绝")


def test_tool_run_requires_call_id():
    envelope = start_run("Tspeed", call_id="")
    try:
        finish_run(envelope, outcome=OutcomeCode.TOOL_ERROR)
    except ValueError:
        return
    raise AssertionError("缺少 call_id 必须被拒绝")


def test_tool_run_serialises_to_json():
    envelope = start_run("Tspeed", call_id="c1", input_hash=stable_hash({"a": 1}))
    run = finish_run(envelope, outcome=OutcomeCode.SEARCH_UNRESOLVED,
                     diagnostics=[Diagnostic(stage="search", code="budget_exhausted",
                                             retryable=True)])
    text = run.to_json()
    decoded = json.loads(text)
    assert decoded["call_id"] == "c1"
    assert decoded["outcome_code"] == "search_unresolved"
    assert decoded["execution_status"] == "completed"
    assert decoded["diagnostics"][0]["code"] == "budget_exhausted"


def test_stable_hash_is_order_insensitive_for_keys():
    assert stable_hash({"a": 1, "b": 2}) == stable_hash({"b": 2, "a": 1})
    assert stable_hash({"a": 1}) != stable_hash({"a": 2})


def test_energy_batch_rejects_duplicate_candidates_and_empty_identity():
    from research.contracts import CandidateTrajectory, TrajectoryPoint

    point = TrajectoryPoint(segment_id="S01", phase="sailing", start_offset_s=0.0,
                            duration_s=10.0, planned_speed_kmh=8.0)
    candidate = CandidateTrajectory(candidate_id="c1", trajectory=(point,))
    try:
        EnergyBatchRequest(candidates=(candidate, candidate), history_ref="h",
                           environment_scenario_ref="e", model_version="m",
                           integration_rule="r")
    except ValueError:
        pass
    else:
        raise AssertionError("重复候选 ID 必须被拒绝")
    for field in ("history_ref", "environment_scenario_ref", "model_version",
                  "integration_rule"):
        kwargs = {"candidates": (candidate,), "history_ref": "h",
                  "environment_scenario_ref": "e", "model_version": "m",
                  "integration_rule": "r"}
        kwargs[field] = "  "
        try:
            EnergyBatchRequest(**kwargs)
        except ValueError:
            continue
        raise AssertionError(f"空 {field} 必须被拒绝（不得用未知占位）")


def test_energy_batch_cache_key_covers_environment():
    from research.contracts import CandidateTrajectory, TrajectoryPoint

    point = TrajectoryPoint(segment_id="S01", phase="sailing", start_offset_s=0.0,
                            duration_s=10.0, planned_speed_kmh=8.0)
    candidate = CandidateTrajectory(candidate_id="c1", trajectory=(point,))
    base = EnergyBatchRequest(candidates=(candidate,), history_ref="h",
                              environment_scenario_ref="env-A", model_version="m",
                              integration_rule="r")
    other = EnergyBatchRequest(candidates=(candidate,), history_ref="h",
                               environment_scenario_ref="env-B", model_version="m",
                               integration_rule="r")
    assert base.cache_key() != other.cache_key(), \
        "相同速度向量在不同水文情景下不得误命中缓存"


def test_trajectory_point_requires_positive_duration():
    from research.contracts import TrajectoryPoint

    try:
        TrajectoryPoint(segment_id="S01", phase="sailing", start_offset_s=0.0,
                        duration_s=0.0, planned_speed_kmh=8.0)
    except ValueError:
        return
    raise AssertionError("duration_s 必须大于 0")


def test_time_grid_validation():
    TimeGridSpec(prediction_step_s=5.0, verification_step_s=5.0,
                 max_acceleration_mps2=0.15, max_deceleration_mps2=0.2,
                 min_speed_kmh=3.0, max_speed_kmh=13.0)
    try:
        TimeGridSpec(prediction_step_s=0.0, verification_step_s=5.0,
                     max_acceleration_mps2=0.15, max_deceleration_mps2=0.2,
                     min_speed_kmh=3.0, max_speed_kmh=13.0)
    except ValueError:
        return
    raise AssertionError("非正预测步长必须被拒绝")


def test_request_requires_source_for_limits():
    from research.contracts import SpeedOptimizationRequest, SegmentConstraint

    segment = SegmentConstraint(segment_id="S01", distance_km=5.0, min_speed_kmh=4.0,
                                max_speed_kmh=12.0)
    grid = f.time_grid()
    import dataclasses

    try:
        SpeedOptimizationRequest(segments=(segment,), time_grid=grid,
                                 power_limit_kw=500.0)
    except ValueError:
        pass
    else:
        raise AssertionError("给出功率上限必须同时给出来源")
    try:
        SpeedOptimizationRequest(segments=(segment,), time_grid=grid,
                                 battery_budget_kwh=100.0)
    except ValueError:
        pass
    else:
        raise AssertionError("给出电池预算必须同时给出来源")
    del dataclasses


def test_request_rejects_unsupported_objective():
    from research.contracts import SpeedOptimizationRequest, SegmentConstraint

    segment = SegmentConstraint(segment_id="S01", distance_km=5.0, min_speed_kmh=4.0,
                                max_speed_kmh=12.0)
    try:
        SpeedOptimizationRequest(segments=(segment,), time_grid=f.time_grid(),
                                 objective="min_cost")
    except ValueError:
        return
    raise AssertionError("未实现目标必须在契约层被拒绝")


def test_arrival_time_needs_absolute_origin():
    assert iso_from_offset(None, 3600.0) is None, "无绝对基准时不得给出日历 ETA"
    assert iso_from_offset("2026-09-25T09:00:00+08:00", 3600.0) \
        == "2026-09-25T10:00:00+08:00"
    assert plan_offset_seconds(None, "2026-09-25T10:00:00+08:00") is None
    assert plan_offset_seconds("2026-09-25T09:00:00+08:00",
                              "2026-09-25T10:00:00+08:00") == 3600.0
    try:
        plan_offset_seconds("2026-09-25T09:00:00", "2026-09-25T10:00:00")
    except ValueError:
        return
    raise AssertionError("无时区时刻必须被拒绝")


def test_management_feedback_hard_constraint_rule():
    proof = ManagementFeedback(
        selected_candidate_id="c1", selected_demand_ref="d1", battery_config_hash="h",
        failure_scope="power_upper_bound_provable", violated_rule="bus_power_limit",
        demand_kw=520.0, deliverable_kw=480.0,
        limit_source_ref=SourceRef(source_id="doc.bus", kind="document"))
    assert proof.becomes_hard_constraint is True
    soc_path = ManagementFeedback(
        selected_candidate_id="c1", selected_demand_ref="d1", battery_config_hash="h",
        failure_scope="soc_path_dependent", violated_rule="soc_min")
    assert soc_path.becomes_hard_constraint is False, \
        "SOC 路径相关失败不得转成对所有新轨迹成立的硬限制"
    try:
        ManagementFeedback(
            selected_candidate_id="c1", selected_demand_ref="d1", battery_config_hash="h",
            failure_scope="power_upper_bound_provable", violated_rule="bus_power_limit")
    except ValueError:
        return
    raise AssertionError("声称可证明的功率上限必须给出 limit_source_ref")


def test_selected_result_rejects_failed_checks():
    from research.contracts import ConstraintCheckRecord, ObjectiveTerms

    terms = ObjectiveTerms(energy_kwh=10.0, penalty=1.0, lambda_value=10.0, total=20.0,
                           energy_scope="motor", penalty_definition="J = E + λ·p")
    checks = (ConstraintCheckRecord(name="time", passed=False, actual=100.0, limit=90.0,
                                    unit="s"),)
    try:
        SpeedOptimizationResult(
            search_status="candidate_found", selected_candidate_id="c1",
            selected_trajectory_ref="t", selected_energy_ref=None,
            selected_demand_ref=None, segment_summary=(), total_duration_s=100.0,
            arrival_at=None, objective_terms=terms, constraint_checks=checks,
            search_trace_ref="trace", seed=1, population=50, generations=10,
            evaluations=100, runtime_ms=1.0)
    except ValueError:
        return
    raise AssertionError("存在未通过检查时不得放入选定方案字段")


def test_result_rejects_unknown_search_status():
    try:
        SpeedOptimizationResult(
            search_status="maybe_found", selected_candidate_id=None,
            selected_trajectory_ref=None, selected_energy_ref=None,
            selected_demand_ref=None, segment_summary=(), total_duration_s=None,
            arrival_at=None, objective_terms=None, constraint_checks=(),
            search_trace_ref=None, seed=1, population=10, generations=1,
            evaluations=10, runtime_ms=1.0)
    except ValueError:
        return
    raise AssertionError("未知 search_status 必须被拒绝")


def test_tool_run_accepts_outcome_enum_and_serialises():
    envelope = start_run("Tspeed", call_id="c2")
    run = finish_run(envelope, outcome=OutcomeCode.NOT_SUPPORTED,
                     diagnostics=[Diagnostic(stage="input", code="objective_not_supported")],
                     payload={"search_status": "input_invalid"})
    assert isinstance(run, ToolRun)
    assert run.outcome_code == "not_supported"
    assert run.ok is False
