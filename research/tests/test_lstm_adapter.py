"""LSTM 适配层验收：结构校验、候选配对、能量口径与一致性自检。"""

from __future__ import annotations

import dataclasses

from research.contracts import CandidateTrajectory, EnergyBatchRequest
from research.tests import fixtures as f
from research.tspeed.lstm_adapter import (
    AdapterContractError,
    LSTMEnergyAdapter,
    PredictionBatchPayload,
    consistency_report,
)
from research.tspeed.profile import TrajectoryProfile
from research.tspeed.trajectory import expand_trajectory


def _candidates(speed_a: float = 8.0, speed_b: float = 9.0):
    request = f.request()
    small = dataclasses.replace(request, segments=request.segments[:3],
                                current_speed_kmh=7.0)
    candidates = []
    for name, speed in (("cand-A", speed_a), ("cand-B", speed_b)):
        vector = [speed] * len(small.optimizable_segment_ids)
        expanded = expand_trajectory(vector, request=small, max_step_s=60.0)
        candidates.append(CandidateTrajectory(name, expanded.to_trajectory_points()))
    return small, tuple(candidates)


class _FakePredictor:
    """最小外部预测器：返回与解析后端同口径的结构化结果。"""

    def __init__(self, *, omit_candidate: str | None = None,
                 break_energy_rule: bool = False) -> None:
        self.omit_candidate = omit_candidate
        self.break_energy_rule = break_energy_rule
        self.calls = 0

    def __call__(self, request: EnergyBatchRequest, grid):
        self.calls += 1
        entries = []
        for candidate in request.candidates:
            if candidate.candidate_id == self.omit_candidate:
                continue
            points = []
            for point in candidate.trajectory:
                if point.phase != "sailing":
                    power = 0.0
                else:
                    power = 0.055 * (point.planned_speed_kmh or 0.0) ** 3
                duration = point.duration_s
                energy = power * duration / 3600.0
                if self.break_energy_rule and points == []:
                    energy = energy + 1.0
                points.append({
                    "segment_id": point.segment_id, "phase": point.phase,
                    "start_offset_s": point.start_offset_s, "duration_s": duration,
                    "planned_speed_kmh": point.planned_speed_kmh,
                    "predicted_motor_power_kw": power, "motor_energy_kwh": energy,
                    "applicability_flag": "in_domain",
                    "target_speed_kmh": point.target_speed_kmh,
                    "interval_kind": point.interval_kind})
            entries.append({"candidate_id": candidate.candidate_id,
                            "outcome_code": "ok", "power_points": points})
        return PredictionBatchPayload(model_version="lstm.fake/0.1",
                                      integration_rule="E = sum(P*dt/3600)",
                                      candidates=entries)


def _request(candidates) -> EnergyBatchRequest:
    return EnergyBatchRequest(
        candidates=candidates, history_ref="history.ref-001",
        environment_scenario_ref="scenario.env-001", model_version="lstm.fake/0.1",
        integration_rule="E = sum(P*dt/3600)")


def test_adapter_returns_candidates_with_matching_ids():
    _, candidates = _candidates()
    grid = f.model_spec().grid
    adapter = LSTMEnergyAdapter(_FakePredictor())
    result = adapter.predict(_request(candidates), grid=grid)
    assert set(result.by_candidate()) == {"cand-A", "cand-B"}
    assert result.model_version == "lstm.fake/0.1"
    for envelope in result.results:
        assert envelope.ok
        assert envelope.energies_match(), "能量必须等于 P·Δt/3600"


def test_adapter_rejects_missing_candidate():
    _, candidates = _candidates()
    grid = f.model_spec().grid
    adapter = LSTMEnergyAdapter(_FakePredictor(omit_candidate="cand-B"))
    try:
        adapter.predict(_request(candidates), grid=grid)
    except ValueError:
        return
    raise AssertionError("缺失候选结果必须被拒绝，不能用其他候选补齐")


def test_adapter_rejects_inconsistent_energy():
    _, candidates = _candidates()
    grid = f.model_spec().grid
    adapter = LSTMEnergyAdapter(_FakePredictor(break_energy_rule=True))
    try:
        adapter.predict(_request(candidates), grid=grid)
    except AdapterContractError:
        return
    raise AssertionError("逐时能量与积分规则不一致必须被拒绝")


def test_adapter_rejects_unknown_candidate_id():
    _, candidates = _candidates()
    grid = f.model_spec().grid

    def extra(request, grid):
        payload = _FakePredictor()(request, grid)
        payload.candidates.append({"candidate_id": "cand-Z", "outcome_code": "ok",
                                   "power_points": []})
        return payload

    adapter = LSTMEnergyAdapter(extra)
    try:
        adapter.predict(_request(candidates), grid=grid)
    except AdapterContractError:
        return
    raise AssertionError("返回未请求的候选必须被拒绝")


def test_adapter_profile_path_matches_artifact_path():
    small, candidates = _candidates()
    grid = f.model_spec().grid
    adapter = LSTMEnergyAdapter(_FakePredictor())
    request = _request(candidates[:1])
    batch = adapter.predict(request, grid=grid)
    expanded = expand_trajectory([8.0] * len(small.optimizable_segment_ids),
                                 request=small, max_step_s=60.0)
    profile = TrajectoryProfile.from_expansion(expanded)
    by_profile = adapter.predict_profile(profile, candidate_id="cand-A", grid=grid)
    artifact = batch.by_candidate()["cand-A"]
    assert by_profile.motor_energy_kwh == artifact.motor_energy_kwh
    assert by_profile.peak_motor_power_kw == artifact.peak_motor_power_kw


def test_consistency_report_flags_identity():
    _, candidates = _candidates()
    grid = f.model_spec().grid
    adapter = LSTMEnergyAdapter(_FakePredictor())
    report = consistency_report(adapter, _request(candidates), grid=grid)
    assert report["all_identical"] is True, report
    assert report["all_energies_match_rule"] is True


def test_adapter_requires_callable():
    try:
        LSTMEnergyAdapter("not-callable")
    except TypeError:
        return
    raise AssertionError("非可调用对象必须被拒绝")


def test_adapter_diagnostics_are_preserved():
    _, candidates = _candidates()
    grid = f.model_spec().grid

    def with_diagnostic(request, grid):
        payload = _FakePredictor()(request, grid)
        payload.candidates[1]["outcome_code"] = "model_out_of_domain"
        payload.candidates[1]["diagnostics"] = [{
            "stage": "predict", "code": "speed_out_of_training_range",
            "segment_id": "S02", "observed": 12.0, "limit": 10.5, "unit": "km/h",
            "retryable": False}]
        return payload

    adapter = LSTMEnergyAdapter(with_diagnostic)
    result = adapter.predict(_request(candidates), grid=grid)
    envelope = result.by_candidate()["cand-B"]
    assert envelope.ok is False
    assert envelope.diagnostics[0].code == "speed_out_of_training_range"
    assert envelope.diagnostics[0].segment_id == "S02"
    assert result.by_candidate()["cand-A"].ok is True, \
        "部分候选失败不得影响其他候选的成功结果"
