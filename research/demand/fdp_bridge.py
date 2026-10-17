"""Research-only handoff from a persisted Tspeed demand to Tmanagement FDP."""

from __future__ import annotations

from dataclasses import replace
from math import isclose

from research.contracts import SourceRef as ResearchSourceRef
from research.contracts import SpeedOptimizationResult, ToolRun
from research.demand.artifact_store import DemandArtifactStore
from schemas.research_management import BatteryManagementRequest, PowerDemandPoint
from schemas.types import SourceRef
from tools.tmanagement_fdp import validate_request


def _legacy_source(source: ResearchSourceRef) -> SourceRef:
    return SourceRef(source.source_id, source.kind, source.locator,
                     source.confirmed, source.note)


def prepare_fdp_request(*, speed_result: SpeedOptimizationResult,
                        speed_run: ToolRun, demand_store: DemandArtifactStore,
                        template: BatteryManagementRequest,
                        conversion_source: ResearchSourceRef,
                        auxiliary_source: ResearchSourceRef) -> BatteryManagementRequest:
    """Load/verify exactly the demand selected by Tspeed and populate FDP.

    Only a named simulation is supported here. Measured/replay provenance and
    model approval need a separately ratified production contract.
    """
    if not speed_run.ok or speed_result.search_status != "candidate_found":
        raise ValueError("Tspeed has no verified selected candidate")
    if template.source_mode != "simulation":
        raise ValueError("cross-tool FDP handoff currently supports simulation only")
    if speed_result.selected_candidate_id is None or speed_result.selected_demand_ref is None:
        raise ValueError("Tspeed did not return a selected demand artifact")
    if (template.snapshot_id != speed_run.snapshot_id or
            template.scenario_id != speed_run.scenario_id or
            template.request_revision != speed_run.request_revision):
        raise ValueError("FDP template does not match Tspeed snapshot/scenario/revision")
    series = demand_store.get(speed_result.selected_demand_ref)
    if series.artifact_ref is None or series.artifact_ref.artifact_id != speed_result.selected_demand_ref:
        raise ValueError("selected demand artifact identity mismatch")
    if series.series_id != f"demand-{speed_result.selected_candidate_id}-verify":
        raise ValueError("selected demand series does not belong to the selected trajectory")
    if (series.conversion_config_hash != speed_result.demand_config_hash or
            not series.bus_side_available or not speed_result.bus_side_available):
        raise ValueError("selected demand conversion is missing or changed")
    source_ids = {source.source_id for source in series.source_refs}
    if (conversion_source.source_id not in source_ids or
            auxiliary_source.source_id not in source_ids):
        raise ValueError("conversion and auxiliary sources must be carried by the selected demand")
    if series.artifact_ref.row_count != len(series.points) or not series.points:
        raise ValueError("selected demand row count is invalid")
    points = []
    expected_start = 0.0
    artifact_source = SourceRef(f"artifact:{series.artifact_ref.artifact_id}", "tool",
                                locator=str(demand_store.directory), confirmed=False,
                                note="Content-addressed Tspeed research demand")
    for point in series.points:
        if not isclose(point.start_offset_s, expected_start, rel_tol=0, abs_tol=1e-6):
            raise ValueError("selected demand time axis has a gap/overlap")
        expected_start = point.end_offset_s
        if (point.demand_bus_kw is None or point.motor_power_kw is None or
                point.propulsion_bus_kw is None or point.auxiliary_bus_kw is None or
                point.segment_id is None or point.conversion_model_ref != series.conversion_model_ref):
            raise ValueError("selected demand has unknown components or a conversion mismatch")
        if not isclose(point.propulsion_bus_kw + point.auxiliary_bus_kw,
                       point.demand_bus_kw, rel_tol=1e-8, abs_tol=1e-6):
            raise ValueError("selected demand components do not sum to bus demand")
        phase = "docking" if point.phase == "berthing" else point.phase
        if phase not in ("sailing", "waiting", "docking"):
            raise ValueError("selected demand contains an FDP-unsupported phase")
        points.append(PowerDemandPoint(
            segment_id=point.segment_id, phase=phase,
            start_offset_s=point.start_offset_s, duration_s=point.duration_s,
            demand_bus_kw=point.demand_bus_kw, demand_source=artifact_source,
            conversion_model_ref=series.conversion_model_ref,
            conversion_source=_legacy_source(conversion_source),
            motor_power_kw=point.motor_power_kw,
            propulsion_bus_kw=point.propulsion_bus_kw,
            auxiliary_bus_kw=point.auxiliary_bus_kw,
            component_source=_legacy_source(auxiliary_source)))
    request = replace(template,
                      selected_candidate_id=speed_result.selected_candidate_id,
                      selected_demand_ref=series.artifact_ref.artifact_id,
                      demand_series=points,
                      parent_call_id=speed_run.call_id,
                      assumptions=list(template.assumptions) + [{
                          "assumption_id": "tspeed-research-demand",
                          "value": series.note or "Named research demand; not vessel operating evidence",
                      }])
    issues = validate_request(request)
    if issues:
        first = issues[0]
        raise ValueError(f"FDP request is invalid: {first['field_ref']} ({first['code']})")
    return request
