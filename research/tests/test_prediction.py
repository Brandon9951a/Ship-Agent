"""预测器路径验收：轻量剖面与 v2 工件数值一致、批量/逐条一致、缓存键与积分口径。"""

from __future__ import annotations

import dataclasses

from research.contracts import (
    CandidateTrajectory,
    EnergyBatchRequest,
    OutcomeCode,
    TrajectoryPoint,
)
from research.tests import fixtures as f
from research.tspeed.profile import TrajectoryProfile
from research.tspeed.trajectory import expand_trajectory


def _setup(speed: float = 8.0, *, segments: int | None = None):
    request = f.request()
    if segments is not None:
        request = dataclasses.replace(request, segments=request.segments[:segments])
    vector = [speed] * len(request.optimizable_segment_ids)
    expanded = expand_trajectory(vector, request=request, max_step_s=60.0)
    spec = f.model_spec(grid=request.time_grid)
    return request, expanded, spec


def _batch(spec, candidate, *, environment_ref: str | None = None) -> EnergyBatchRequest:
    return EnergyBatchRequest(
        candidates=(candidate,), history_ref=spec.history_ref,
        environment_scenario_ref=environment_ref or spec.environment_ref,
        model_version=spec.predictor.model_version,
        integration_rule=spec.predictor.integration_rule,
        call_id="test-call")


def test_profile_and_artifact_paths_agree():
    """判分路径（剖面）与核验路径（v2 工件）必须给出相同能量与峰值。"""
    _, expanded, spec = _setup(8.5)
    profile = TrajectoryProfile.from_expansion(expanded)
    predictor = spec.predictor
    candidate = CandidateTrajectory("cand-A", expanded.to_trajectory_points())
    batch = predictor.predict(_batch(spec, candidate), grid=spec.grid)
    envelope = batch.by_candidate()["cand-A"]
    by_profile = predictor.predict_profile(profile, candidate_id="cand-A",
                                           grid=spec.grid)
    assert envelope.motor_energy_kwh is not None
    assert abs(envelope.motor_energy_kwh - by_profile.motor_energy_kwh) < 1e-9
    assert envelope.peak_motor_power_kw == by_profile.peak_motor_power_kw
    assert len(envelope.power_points) == len(by_profile.power_points)
    for left, right in zip(envelope.power_points, by_profile.power_points):
        assert left.start_offset_s == right.start_offset_s
        assert left.duration_s == right.duration_s
        assert left.planned_speed_kmh == right.planned_speed_kmh
        assert left.phase == right.phase
        assert left.predicted_motor_power_kw == right.predicted_motor_power_kw


def test_waiting_phase_is_included_in_scoring_path():
    """等待阶段必须同时出现在两条路径上（否则辅助能耗会被漏算）。"""
    _, expanded, spec = _setup(8.0, segments=9)
    profile = TrajectoryProfile.from_expansion(expanded)
    assert "waiting" in profile.phases
    candidate = CandidateTrajectory("cand-W", expanded.to_trajectory_points())
    batch = spec.predictor.predict(_batch(spec, candidate), grid=spec.grid)
    envelope = batch.by_candidate()["cand-W"]
    assert any(point.phase == "waiting" for point in envelope.power_points)
    waiting = [point for point in envelope.power_points if point.phase == "waiting"]
    assert all(point.predicted_motor_power_kw == 0.0 for point in waiting)


def test_energy_integration_rule_holds():
    _, expanded, spec = _setup(9.0)
    candidate = CandidateTrajectory("cand-B", expanded.to_trajectory_points())
    envelope = spec.predictor.predict(_batch(spec, candidate),
                                      grid=spec.grid).by_candidate()["cand-B"]
    assert envelope.energies_match(), "逐时能量必须等于 P·Δt/3600"
    recomputed = sum(point.motor_energy_kwh for point in envelope.power_points)
    assert abs(recomputed - envelope.motor_energy_kwh) < 1e-9
    assert envelope.total_summary.total_seconds == sum(
        point.duration_s for point in envelope.power_points)


def test_batch_and_single_candidate_agree():
    _, expanded, spec = _setup(7.0)
    candidate_a = CandidateTrajectory("cand-A", expanded.to_trajectory_points())
    vector_b = [9.0] * len(expanded.optimizable_segment_ids)
    expanded_b = expand_trajectory(vector_b, request=_, max_step_s=60.0)
    candidate_b = CandidateTrajectory("cand-B", expanded_b.to_trajectory_points())
    both = spec.predictor.predict(
        EnergyBatchRequest(candidates=(candidate_a, candidate_b),
                           history_ref=spec.history_ref,
                           environment_scenario_ref=spec.environment_ref,
                           model_version=spec.predictor.model_version,
                           integration_rule=spec.predictor.integration_rule),
        grid=spec.grid)
    alone_a = spec.predictor.predict(_batch(spec, candidate_a), grid=spec.grid)
    result = both.by_candidate()
    assert set(result) == {"cand-A", "cand-B"}
    assert abs(result["cand-A"].motor_energy_kwh
               - alone_a.by_candidate()["cand-A"].motor_energy_kwh) < 1e-9
    both.require_all_candidates(("cand-B", "cand-A")), "顺序不影响候选配对"


def test_candidate_order_does_not_change_mapping():
    _, expanded, spec = _setup(7.5)
    a = CandidateTrajectory("cand-A", expanded.to_trajectory_points())
    b = CandidateTrajectory("cand-B", expanded.to_trajectory_points())
    forward = spec.predictor.predict(
        EnergyBatchRequest(candidates=(a, b), history_ref=spec.history_ref,
                           environment_scenario_ref=spec.environment_ref,
                           model_version=spec.predictor.model_version,
                           integration_rule=spec.predictor.integration_rule),
        grid=spec.grid).by_candidate()
    backward = spec.predictor.predict(
        EnergyBatchRequest(candidates=(b, a), history_ref=spec.history_ref,
                           environment_scenario_ref=spec.environment_ref,
                           model_version=spec.predictor.model_version,
                           integration_rule=spec.predictor.integration_rule),
        grid=spec.grid).by_candidate()
    assert forward["cand-A"].motor_energy_kwh == backward["cand-A"].motor_energy_kwh
    assert forward["cand-B"].motor_energy_kwh == backward["cand-B"].motor_energy_kwh


def test_environment_change_does_not_hit_cache():
    """相同速度向量在不同水文情景下不得误命中缓存。"""
    _, expanded, spec = _setup(8.0)
    candidate = CandidateTrajectory("cand-C", expanded.to_trajectory_points())
    baseline = spec.predictor.predict(_batch(spec, candidate), grid=spec.grid)
    other = spec.predictor.predict(_batch(spec, candidate, environment_ref="env-B"),
                                   grid=spec.grid)
    assert baseline.by_candidate()["cand-C"].cache_hit is False
    assert other.by_candidate()["cand-C"].cache_hit is False, \
        "更换环境情景必须重新预测"
    assert other.by_candidate()["cand-C"].motor_energy_kwh \
        == baseline.by_candidate()["cand-C"].motor_energy_kwh, \
        "本验收后端的环境因子相同，故能量应一致；差异只体现在缓存键"


def test_repeated_call_hits_cache():
    _, expanded, spec = _setup(8.0)
    candidate = CandidateTrajectory("cand-D", expanded.to_trajectory_points())
    first = spec.predictor.predict(_batch(spec, candidate), grid=spec.grid)
    second = spec.predictor.predict(_batch(spec, candidate), grid=spec.grid)
    assert first.by_candidate()["cand-D"].cache_hit is False
    assert second.by_candidate()["cand-D"].cache_hit is True
    assert second.cache_hits == 1


def test_unknown_speed_is_reported_not_zeroed():
    points = (
        TrajectoryPoint(segment_id="S01", phase="sailing", start_offset_s=0.0,
                        duration_s=10.0, planned_speed_kmh=None),
        TrajectoryPoint(segment_id="S01", phase="sailing", start_offset_s=10.0,
                        duration_s=10.0, planned_speed_kmh=8.0),
    )
    candidate = CandidateTrajectory("cand-N", points)
    spec = f.model_spec()
    envelope = spec.predictor.predict(_batch(spec, candidate),
                                      grid=spec.grid).by_candidate()["cand-N"]
    assert envelope.ok is False
    assert envelope.outcome_code == OutcomeCode.NEEDS_INPUT.value
    assert envelope.power_points[0].predicted_motor_power_kw is None
    assert "unpredicted" in envelope.power_points[0].quality_flags
    assert envelope.motor_energy_kwh is None, "存在未预测区间时不得给出完整能量"
    assert envelope.coverage.unpredicted_fraction > 0.0


def test_coverage_and_peak_are_reported():
    _, expanded, spec = _setup(10.0)
    candidate = CandidateTrajectory("cand-E", expanded.to_trajectory_points())
    envelope = spec.predictor.predict(_batch(spec, candidate),
                                      grid=spec.grid).by_candidate()["cand-E"]
    assert abs(envelope.coverage.valid_fraction - 1.0) < 1e-12
    assert envelope.coverage.total_seconds > 0
    expected_peak = spec.predictor.power_kw(10.0)
    assert abs(envelope.peak_motor_power_kw - expected_peak) < 1e-9


def test_model_version_and_integration_rule_are_echoed():
    _, expanded, spec = _setup(8.0)
    candidate = CandidateTrajectory("cand-F", expanded.to_trajectory_points())
    batch = spec.predictor.predict(_batch(spec, candidate), grid=spec.grid)
    assert batch.model_version == spec.predictor.model_version
    assert batch.integration_rule == spec.predictor.integration_rule
    assert batch.call_id == "test-call"
