"""Run named research fixtures through the isolated three-tool LangGraph flow.

Usage: python -m research.scripts.run_three_tool_research --case success
Outputs are simulation evidence only and are never formal vessel plans.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

from research.demand.artifact_store import DemandArtifactStore
from research.tests import fixtures as f
from research.tspeed import DEConfig
from research.tspeed.tool import SimulationScenario
from research.workflow import run_research_workflow
from scripts.fdp_research_cases import build_request


CASES = ("success", "missing_conversion", "fdp_reject", "model_unapproved",
         "model_out_of_domain")
ROOT = Path(__file__).resolve().parents[2]


def build_case(case: str, artifacts: Path) -> dict:
    request = f.request()
    request = replace(request, segments=(replace(request.segments[1], distance_km=0.5),),
                      current_speed_kmh=7.0)
    demand_spec = (f.motor_side_only_spec() if case == "missing_conversion"
                   else f.demand_spec(auxiliary_kw=5.0))
    spec = f.model_spec(grid=request.time_grid, demand_spec=demand_spec)
    scenario = SimulationScenario(
        scenario_id=request.scenario_id, environment_ref=spec.environment_ref,
        history_ref=spec.history_ref, model_spec=spec,
        assumptions=("具名解析功率和电池参数研究情景",))
    template = build_request("success")
    template = replace(template, snapshot_id=request.snapshot_id,
                       scenario_id=request.scenario_id,
                       request_revision=request.request_revision)
    if case == "fdp_reject":
        template = replace(
            template,
            battery_1=replace(template.battery_1, min_power_kw=0.0, max_power_kw=1.0),
            battery_2=replace(template.battery_2, min_power_kw=0.0, max_power_kw=1.0))
    if case in ("model_unapproved", "model_out_of_domain"):
        from research.tspeed.tenergy_v2_bridge import TenergyV2Predictor
        from scripts.example_tenergy_v2 import load_fixture
        from tools.tenergy_v2 import JsonArtifactStore, TenergyV2

        energy_store = JsonArtifactStore(artifacts / "tenergy")
        fixture = load_fixture(energy_store)
        predictor = TenergyV2Predictor(
            TenergyV2(ROOT / "models/tenergy-lstm-v5", energy_store), energy_store,
            history_ref=fixture["history_ref"], environment_ref=None,
            snapshot_id=fixture["snapshot_id"], scenario_id=fixture["scenario_id"],
            model_version=fixture["model_version"], task_id=fixture["task_id"],
            usage="planning" if case == "model_unapproved" else "research")
        request = replace(request, task_id=fixture["task_id"],
                          snapshot_id=fixture["snapshot_id"],
                          scenario_id=fixture["scenario_id"],
                          history_ref=predictor.history_id,
                          environment_scenario_ref=predictor.environment_id)
        spec = replace(spec, predictor=predictor, history_ref=predictor.history_id,
                       environment_ref=predictor.environment_id,
                       model_version=predictor.model_version,
                       integration_rule=predictor.integration_rule, max_step_s=5.0)
        scenario = replace(scenario, scenario_id=fixture["scenario_id"],
                           environment_ref=predictor.environment_id,
                           history_ref=predictor.history_id, model_spec=spec)
        template = replace(template, snapshot_id=fixture["snapshot_id"],
                           scenario_id=fixture["scenario_id"])
    return {
        "speed_request": request, "scenario": scenario,
        "fdp_template": template,
        "demand_store": DemandArtifactStore(artifacts / "demand"),
        "conversion_source": (demand_spec.conversion.source_ref if demand_spec.conversion
                              else f.AUX_ASSUMPTION),
        "auxiliary_source": demand_spec.auxiliary.source_ref,
        "de_config": DEConfig(population=8, max_generations=3, seeds=(211,)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=CASES, default="success")
    parser.add_argument("--artifacts", type=Path,
                        default=ROOT / "artifacts/three-tool-research")
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    summary = run_research_workflow(**build_case(args.case, args.artifacts))
    print(json.dumps({"case": args.case, **summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
