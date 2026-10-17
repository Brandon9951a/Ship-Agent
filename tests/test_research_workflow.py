"""LangGraph research flow: success and explicit stop conditions."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from research.demand.artifact_store import DemandArtifactStore
from research.tests import fixtures as f
from research.tspeed import DEConfig
from research.tspeed.tenergy_v2_bridge import TenergyV2Predictor
from research.tspeed.tool import SimulationScenario
from research.workflow import run_research_workflow
from scripts.example_tenergy_v2 import load_fixture
from scripts.fdp_research_cases import build_request
from tools.tenergy_v2 import JsonArtifactStore, TenergyV2


ROOT = Path(__file__).resolve().parents[1]
CONFIG = DEConfig(population=8, max_generations=3, seeds=(211,))


def _case(tmp_path, *, demand_spec=None, template=None):
    request = f.request()
    request = replace(request, segments=(replace(request.segments[1], distance_km=0.5),),
                      current_speed_kmh=7.0)
    demand_spec = demand_spec if demand_spec is not None else f.demand_spec(auxiliary_kw=5.0)
    spec = f.model_spec(grid=request.time_grid, demand_spec=demand_spec)
    scenario = SimulationScenario(
        scenario_id=request.scenario_id, environment_ref=spec.environment_ref,
        history_ref=spec.history_ref, model_spec=spec,
        assumptions=("具名解析功率研究情景",))
    template = template or build_request("success")
    template = replace(template, snapshot_id=request.snapshot_id,
                       scenario_id=request.scenario_id,
                       request_revision=request.request_revision)
    return dict(speed_request=request, scenario=scenario,
                fdp_template=template, demand_store=DemandArtifactStore(tmp_path / "demand"),
                conversion_source=(demand_spec.conversion.source_ref
                                   if demand_spec.conversion else f.AUX_ASSUMPTION),
                auxiliary_source=demand_spec.auxiliary.source_ref, de_config=CONFIG)


def test_research_graph_success_is_explicitly_nonformal(tmp_path):
    result = run_research_workflow(**_case(tmp_path))
    assert result["outcome_code"] == "research_feasible"
    assert result["research_feasible"] is True
    assert result["speed_outcome"] == "ok"
    assert result["fdp_outcome"] == "ok"
    assert result["selected_demand_ref"]
    assert "不构成实船" in result["note"]


def test_research_graph_stops_when_conversion_basis_missing(tmp_path):
    result = run_research_workflow(**_case(tmp_path, demand_spec=f.motor_side_only_spec()))
    assert result["outcome_code"] == "needs_input"
    assert result["speed_outcome"] is None
    assert result["fdp_outcome"] is None


def test_research_graph_keeps_fdp_rejection(tmp_path):
    template = build_request("success")
    template = replace(template,
                       battery_1=replace(template.battery_1, min_power_kw=0.0, max_power_kw=1.0),
                       battery_2=replace(template.battery_2, min_power_kw=0.0, max_power_kw=1.0))
    result = run_research_workflow(**_case(tmp_path, template=template))
    assert result["outcome_code"] == "infeasible_under_assumptions"
    assert result["research_feasible"] is False
    assert result["speed_outcome"] == "ok"
    assert result["fdp_outcome"] == "infeasible_under_assumptions"


def test_research_graph_stops_on_corrupted_demand_artifact(tmp_path):
    class CorruptStore(DemandArtifactStore):
        def get(self, artifact_id):
            raise ValueError("demand artifact content hash mismatch")

    case = _case(tmp_path)
    case["demand_store"] = CorruptStore(tmp_path / "corrupt-demand")
    result = run_research_workflow(**case)
    assert result["outcome_code"] == "invalid_input"
    assert result["speed_outcome"] == "ok"
    assert result["fdp_outcome"] is None
    assert "hash mismatch" in result["diagnostics"][0]["detail"]


def _tenergy_case(tmp_path, usage):
    case = _case(tmp_path)
    store = JsonArtifactStore(tmp_path / "energy")
    fixture = load_fixture(store)
    bridge = TenergyV2Predictor(
        TenergyV2(ROOT / "models/tenergy-lstm-v5", store), store,
        history_ref=fixture["history_ref"], environment_ref=None,
        snapshot_id=fixture["snapshot_id"], scenario_id=fixture["scenario_id"],
        model_version=fixture["model_version"], task_id=fixture["task_id"], usage=usage)
    request = replace(case["speed_request"], task_id=fixture["task_id"],
                      snapshot_id=fixture["snapshot_id"],
                      scenario_id=fixture["scenario_id"], history_ref=bridge.history_id,
                      environment_scenario_ref=bridge.environment_id)
    spec = replace(case["scenario"].model_spec, predictor=bridge,
                   history_ref=bridge.history_id,
                   environment_ref=bridge.environment_id,
                   model_version=bridge.model_version, integration_rule=bridge.integration_rule,
                   max_step_s=5.0)
    case["speed_request"] = request
    case["scenario"] = replace(case["scenario"], scenario_id=fixture["scenario_id"],
                               history_ref=bridge.history_id,
                               environment_ref=bridge.environment_id, model_spec=spec)
    case["fdp_template"] = replace(case["fdp_template"],
                                   snapshot_id=fixture["snapshot_id"],
                                   scenario_id=fixture["scenario_id"])
    return case


def test_research_graph_stops_on_unapproved_planning_model(tmp_path):
    result = run_research_workflow(**_tenergy_case(tmp_path, "planning"))
    assert result["outcome_code"] == "model_unapproved"
    assert result["speed_outcome"] is None


def test_research_graph_stops_when_voyage_exceeds_lstm_horizon(tmp_path):
    result = run_research_workflow(**_tenergy_case(tmp_path, "research"))
    assert result["outcome_code"] == "model_out_of_domain"
    assert result["speed_outcome"] is None
