"""Cross-PR checks using the real Tenergy artifact API and FDP handoff."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import os

import pytest

from research.contracts import CandidateTrajectory, EnergyBatchRequest, TrajectoryPoint
from research.demand.artifact_store import DemandArtifactStore
from research.demand.builder import build_power_demand
from research.demand.fdp_bridge import prepare_fdp_request
from research.tests import fixtures as f
from research.tspeed import DEConfig
from research.tspeed.eval import DEEvaluationContext, evaluate_candidate
from research.tspeed.lstm_adapter import AdapterContractError
from research.tspeed.predictor import EvaluationGrid
from research.tspeed.profile import TrajectoryProfile
from research.tspeed.tenergy_v2_bridge import TenergyV2Predictor
from research.tspeed.tool import SimulationScenario, run_speed_optimization
from research.tspeed.verification import verify_candidate
from scripts.example_tenergy_v2 import load_fixture
from scripts.fdp_research_cases import build_request
from tools.tenergy_v2 import JsonArtifactStore, TenergyV2
from tools.tmanagement_fdp import run_management_fdp


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-path regression")
def test_selected_demand_survives_windows_path_longer_than_max_path(tmp_path):
    from research.contracts import PowerDemandPoint, PowerDemandSeries
    directory = tmp_path / ("a" * 75) / ("b" * 75) / ("c" * 75)
    assert len(str(directory.resolve())) > 260
    store = DemandArtifactStore(directory)
    series = PowerDemandSeries(series_id="long-path", points=(PowerDemandPoint(
        phase="sailing", start_offset_s=0, duration_s=5, motor_power_kw=10,
        propulsion_bus_kw=10, auxiliary_bus_kw=0, demand_bus_kw=10,
        conversion_model_ref="named"),), conversion_model_ref="named",
        conversion_config_hash="0" * 64, bus_side_available=True)
    ref = store.put(series)
    try:
        loaded = store.get(ref.artifact_id)
        assert loaded.points == series.points
        assert loaded.demand_energy_kwh() == pytest.approx(10 * 5 / 3600)
        assert loaded.artifact_ref.sha256 == ref.sha256
    finally:
        # Remove the exact test file via the same extended path; no recursive
        # cleanup or change to the workstation's registry is required.
        (store.directory / f"{ref.artifact_id}.json").unlink()
        cursor = store.directory
        for _ in range(3):
            cursor.rmdir()
            cursor = cursor.parent


def _bridge_request(raw: dict, bridge: TenergyV2Predictor) -> EnergyBatchRequest:
    phase = {"moving": "sailing", "waiting": "waiting", "stopped": "berthing"}
    candidates = tuple(CandidateTrajectory(
        item["candidate_id"], tuple(TrajectoryPoint(
            segment_id=point["segment_id"], phase=phase[point["phase"]],
            start_offset_s=point["start_offset_s"], duration_s=point["duration_s"],
            planned_speed_kmh=point["planned_speed_kmh"],
            environment_ref=bridge.environment_id)
            for point in item["trajectory"])) for item in raw["candidates"])
    return EnergyBatchRequest(
        candidates=candidates, history_ref=bridge.history_id,
        environment_scenario_ref=bridge.environment_id,
        model_version=bridge.model_version, integration_rule=bridge.integration_rule,
        task_id=raw["task_id"], request_revision=raw["request_revision"],
        snapshot_id=raw["snapshot_id"], scenario_id=raw["scenario_id"],
        call_id="integration-batch")


def test_real_tenergy_artifacts_are_read_and_model_limits_propagate(tmp_path):
    store = JsonArtifactStore(tmp_path / "energy")
    raw = load_fixture(store)
    predictor = TenergyV2(ROOT / "models/tenergy-lstm-v5", store)
    bridge = TenergyV2Predictor(
        predictor, store, history_ref=raw["history_ref"], environment_ref=None,
        snapshot_id=raw["snapshot_id"], scenario_id=raw["scenario_id"],
        model_version=raw["model_version"], task_id=raw["task_id"])
    grid = EvaluationGrid(5, 5, {"S1": 0.1, "S2": 0.2})
    bridge_request = _bridge_request(raw, bridge)
    result = bridge.predict(bridge_request, grid=grid).by_candidate()
    assert result["short"].ok
    assert len(result["short"].power_points) == 1
    assert result["short"].artifact_ref is not None
    assert result["short"].energies_match()
    assert result["longer"].ok
    assert result["out-of-domain"].outcome_code == "model_out_of_domain"
    assert result["with-wait"].outcome_code == "needs_input"
    assert result["long-horizon"].outcome_code == "model_out_of_domain"
    with pytest.raises(AdapterContractError, match="request_revision"):
        bridge.predict(replace(bridge_request, request_revision="unknown"), grid=grid)
    with pytest.raises(AdapterContractError, match="task_id"):
        bridge.predict(replace(bridge_request, task_id="another-task"), grid=grid)

    planning = TenergyV2Predictor(
        predictor, store, history_ref=raw["history_ref"], environment_ref=None,
        snapshot_id=raw["snapshot_id"], scenario_id=raw["scenario_id"],
        model_version=raw["model_version"], task_id=raw["task_id"], usage="planning")
    one = replace(_bridge_request(raw, planning), candidates=(_bridge_request(raw, planning).candidates[0],))
    assert planning.predict(one, grid=grid).results[0].outcome_code == "model_unapproved"


def test_selected_tspeed_demand_is_persisted_and_handed_to_fdp(tmp_path):
    speed_request = f.request()
    short_leg = replace(speed_request.segments[1], distance_km=0.5)
    speed_request = replace(speed_request, segments=(short_leg,),
                            current_speed_kmh=7.0)
    demand_spec = f.demand_spec(auxiliary_kw=5.0)
    model_spec = f.model_spec(grid=speed_request.time_grid, demand_spec=demand_spec)
    scenario = SimulationScenario(
        scenario_id="scenario.integration", environment_ref=model_spec.environment_ref,
        history_ref=model_spec.history_ref, model_spec=model_spec,
        assumptions=("具名解析功率与电池参数研究情景",))
    store = DemandArtifactStore(tmp_path / "demand")
    speed_run, speed_result = run_speed_optimization(
        speed_request, scenario=scenario,
        config=DEConfig(population=8, max_generations=3, seeds=(211,)),
        demand_store=store)
    assert speed_run.ok
    assert speed_result.selected_demand_ref is not None
    selected = store.get(speed_result.selected_demand_ref)
    assert selected.artifact_ref.row_count == len(selected.points)
    assert selected.conversion_config_hash == speed_result.demand_config_hash
    assert speed_result.objective_terms.energy_scope == "bus"
    assert speed_result.objective_terms.energy_kwh == pytest.approx(
        selected.demand_energy_kwh())
    assert speed_result.objective_terms.energy_kwh != pytest.approx(
        speed_result.total_energy_kwh)

    template = replace(build_request("success"),
                       snapshot_id=speed_run.snapshot_id,
                       scenario_id=speed_run.scenario_id,
                       request_revision=speed_run.request_revision)
    source = demand_spec.conversion.source_ref
    request = prepare_fdp_request(
        speed_result=speed_result, speed_run=speed_run, demand_store=store,
        template=template, conversion_source=source,
        auxiliary_source=demand_spec.auxiliary.source_ref)
    assert request.selected_demand_ref == speed_result.selected_demand_ref
    assert len(request.demand_series) == len(selected.points)
    assert all(point.demand_source.source_id.endswith(speed_result.selected_demand_ref)
               for point in request.demand_series)
    assert run_management_fdp(request)["outcome_code"] in (
        "ok", "grid_unresolved", "infeasible_under_assumptions")

    artifact = store.directory / f"{speed_result.selected_demand_ref}.json"
    artifact.write_bytes(artifact.read_bytes() + b" ")
    with pytest.raises(ValueError, match="content hash mismatch"):
        prepare_fdp_request(speed_result=speed_result, speed_run=speed_run,
                            demand_store=store, template=template,
                            conversion_source=source,
                            auxiliary_source=demand_spec.auxiliary.source_ref)


def test_search_stage_enforces_bus_power_with_nonempty_demand():
    request = f.request()
    request = replace(request, segments=(replace(request.segments[1], distance_km=0.5),),
                      current_speed_kmh=7.0)
    spec = f.model_spec(grid=request.time_grid, demand_spec=f.demand_spec(),
                        demand_limit_kw=1.0)
    context = DEEvaluationContext(request=request, spec=spec,
                                  environment_ref=spec.environment_ref,
                                  history_ref=spec.history_ref, config_hash="search-limit-regression")
    outcome = evaluate_candidate((8.0,), context=context)
    assert outcome.demand is not None
    assert outcome.demand.demand_energy_kwh() is not None
    assert "power_limit_exceeded" in outcome.violations
    assert outcome.objective_value == pytest.approx(
        outcome.demand.demand_energy_kwh()
        + request.speed_penalty_lambda * outcome.penalty)
    profile = TrajectoryProfile.from_expansion(outcome.expanded)
    full = spec.predictor.predict_profile(profile, candidate_id="full-check",
                                          grid=spec.grid, keep_power_points=True)
    selected = build_power_demand(full.to_envelope(), spec=spec.demand_spec,
                                  series_id="full-check")
    assert outcome.demand.demand_energy_kwh() == pytest.approx(selected.demand_energy_kwh())
    assert outcome.demand.peak_demand_kw() == pytest.approx(selected.peak_demand_kw())


def test_tspeed_evaluator_consumes_real_tenergy_power_artifact(tmp_path):
    store = JsonArtifactStore(tmp_path / "energy")
    raw = load_fixture(store)
    bridge = TenergyV2Predictor(
        TenergyV2(ROOT / "models/tenergy-lstm-v5", store), store,
        history_ref=raw["history_ref"], environment_ref=None,
        snapshot_id=raw["snapshot_id"], scenario_id=raw["scenario_id"],
        model_version=raw["model_version"], task_id=raw["task_id"])
    distance = 10.0 * 20.0 / 3600.0
    base = f.request()
    segment = replace(base.segments[1], distance_km=distance,
                      min_speed_kmh=10.0, max_speed_kmh=10.0)
    request = replace(base, segments=(segment,), current_speed_kmh=10.0,
                      task_id=raw["task_id"],
                      snapshot_id=raw["snapshot_id"], scenario_id=raw["scenario_id"],
                      history_ref=bridge.history_id,
                      environment_scenario_ref=bridge.environment_id)
    spec = replace(f.model_spec(grid=request.time_grid,
                                demand_spec=f.demand_spec(auxiliary_kw=5.0)),
                   predictor=bridge, history_ref=bridge.history_id,
                   environment_ref=bridge.environment_id,
                   model_version=bridge.model_version,
                   integration_rule=bridge.integration_rule, max_step_s=5.0,
                   grid=EvaluationGrid(5.0, 5.0, {segment.segment_id: distance}))
    context = DEEvaluationContext(request=request, spec=spec,
                                  environment_ref=bridge.environment_id,
                                  history_ref=bridge.history_id, config_hash="real-lstm-bridge")
    evaluated = evaluate_candidate((10.0,), context=context)
    assert evaluated.outcome.ok
    assert evaluated.outcome.artifact_ref is not None
    assert evaluated.demand is not None and len(evaluated.demand.points) == 4
    candidate = CandidateTrajectory(evaluated.candidate_id,
                                    evaluated.expanded.to_trajectory_points())
    verification = verify_candidate(candidate, request=request, spec=spec)
    assert verification.passed
    assert verification.demand is not None
    assert verification.demand.input_energy_ref == verification.energy_envelope.artifact_ref.artifact_id
