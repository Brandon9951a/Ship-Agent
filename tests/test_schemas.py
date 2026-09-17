"""Contract tests use synthetic arithmetic, not measured vessel performance."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from schemas.types import (
    ConstraintCheck, DataContext, EnergyResult, InfeasibleType, ManagementPlan,
    OptimizationResult, Segment, SocPoint, SourceRef, Status, ToolResponse,
    VesselState, VoyagePlan, VoyageRequest,
)
from schemas.validate import SchemaValidationError, parse_request, require_valid, validate


@pytest.fixture
def voyage_request():
    return VoyageRequest(
        origin="测试港A", destination="测试港B",
        departure_at="2026-09-18T09:00:00+08:00",
        max_duration_h=2.0, soc_initial=0.9, load_state="测试载况",
    )


@pytest.fixture
def plan(voyage_request):
    source = SourceRef("synthetic-test-only", "assumption", note="仅测试算术")
    vessel = VesselState(
        "synthetic-vessel", capacity_kwh=100.0, soc_initial=0.9,
        soc_min=0.2, soc_alarm=0.3, max_power_kw=20.0, auxiliary_power_kw=0.0,
        sources={name: source for name in (
            "capacity_kwh", "soc_initial", "soc_min", "max_power_kw", "auxiliary_power_kw"
        )},
    )
    segment = Segment("s1", "测试港A", "测试港B", 1.0, max_speed_kmh=20.0,
                      waiting_h=0.0, source=source)
    energy = EnergyResult("s1", 10.0, 10.0, 0.1, 1.0, "total", "synthetic",
                          peak_power_kw=10.0)
    optimization = OptimizationResult(
        True, "total", [energy], 1.0, 0.1, "2026-09-18T09:06:00+08:00",
        checks=[ConstraintCheck(name, True) for name in ("time", "speed", "power")],
    )
    management = ManagementPlan(
        safe=True, soc_initial=0.9, soc_final=0.89, soc_min=0.2,
        capacity_kwh=100.0, available_energy_kwh=70.0, required_energy_kwh=1.0,
        auxiliary_energy_kwh=0.0, charge_required_kwh=0.0,
        soc_trajectory=[SocPoint("s1", 0.89)], checks=[ConstraintCheck("soc", True)],
    )
    return VoyagePlan(voyage_request, Status.OK, DataContext(vessel, "test-route"),
                      [segment], optimization, management)


def test_sample_request_is_valid():
    path = Path(__file__).resolve().parents[1] / "configs/examples/voyage_request.json"
    voyage_request, result = parse_request(json.loads(path.read_text(encoding="utf-8")))
    assert result.valid
    assert voyage_request.soc_initial == 0.85


def test_full_plan_round_trip(plan):
    encoded = json.loads(json.dumps(plan.to_dict(), ensure_ascii=False, allow_nan=False))
    decoded = VoyagePlan.from_dict(encoded)
    assert decoded == plan
    assert encoded["status"] == "ok"
    assert validate(decoded).valid


def test_partial_request_retains_unknown_values():
    voyage_request, result = parse_request({"origin": "测试港A"})
    assert voyage_request.origin == "测试港A"
    assert voyage_request.soc_initial is None
    assert result.status == Status.NEED_CLARIFICATION
    assert "soc_initial" in result.missing_fields
    assert "destination" in result.missing_fields
    assert any("初始 SOC" in question for question in result.questions)


@pytest.mark.parametrize("soc", [-0.1, 1.1, 85.0])
def test_soc_percentage_or_out_of_range_is_invalid(voyage_request, soc):
    result = validate(replace(voyage_request, soc_initial=soc))
    assert result.status == Status.INVALID_INPUT
    assert any(issue.field == "soc_initial" for issue in result.issues)


@pytest.mark.parametrize("soc", [True, "0.85", float("nan"), float("inf")])
def test_malformed_soc_is_not_silently_coerced(voyage_request, soc):
    payload = voyage_request.to_dict()
    payload["soc_initial"] = soc
    decoded, result = parse_request(payload)
    assert decoded is None
    assert result.status == Status.INVALID_INPUT


def test_unknown_field_is_rejected(voyage_request):
    payload = voyage_request.to_dict()
    payload["soc_intial"] = 0.85
    with pytest.raises(SchemaValidationError):
        VoyageRequest.from_dict(payload)


@pytest.mark.parametrize("value", ["2026-09-18T09:00:00", "tomorrow"])
def test_time_requires_timezone(voyage_request, value):
    assert validate(replace(voyage_request, departure_at=value)).status == Status.INVALID_INPUT


def test_deadline_before_departure_is_invalid(voyage_request):
    value = replace(voyage_request, arrival_deadline="2026-09-18T08:59:00+08:00")
    assert validate(value).status == Status.INVALID_INPUT


def test_absolute_deadline_requires_departure(voyage_request):
    value = replace(voyage_request, departure_at=None, arrival_deadline="2026-09-18T10:00:00+08:00")
    assert "departure_at" in validate(value).missing_fields


def test_relative_duration_can_omit_departure_without_inventing_eta(voyage_request):
    value = replace(voyage_request, departure_at=None)
    assert validate(value).valid
    assert value.departure_at is None


def test_negative_distance_is_invalid(plan):
    assert validate(replace(plan.segments[0], distance_km=-1)).status == Status.INVALID_INPUT


def test_unknown_wait_and_limit_are_not_zero(plan):
    segment = replace(plan.segments[0], waiting_h=None, max_speed_kmh=None)
    result = validate(segment)
    assert result.status == Status.NEED_CLARIFICATION
    assert set(result.missing_fields) == {"max_speed_kmh", "waiting_h"}
    assert segment.waiting_h is None


def test_vessel_limits_require_sources(plan):
    state = replace(plan.data.vessel, soc_min=None, sources={})
    result = validate(state)
    assert "soc_min" in result.missing_fields
    assert "sources.capacity_kwh" in result.missing_fields
    assert state.soc_min is None


def test_energy_identity_is_checked(plan):
    energy = replace(plan.optimization.energy_results[0], energy_kwh=2.0)
    assert validate(energy).status == Status.INVALID_INPUT


def test_optimization_totals_are_checked(plan):
    optimization = replace(plan.optimization, total_energy_kwh=20)
    assert validate(optimization).status == Status.INVALID_INPUT


def test_infeasible_result_is_valid_but_not_a_success():
    result = OptimizationResult(False, "total", infeasible_type=InfeasibleType.TIME,
                                reason="仅用于测试的时间不可行")
    assert validate(result).valid
    assert result.feasible is False


def test_infeasible_result_cannot_contain_success_plan(plan):
    optimization = replace(plan.optimization, feasible=False,
                           infeasible_type=InfeasibleType.TIME, reason="不可行")
    assert validate(optimization).status == Status.INVALID_INPUT


@pytest.mark.parametrize("checks", [
    [], [ConstraintCheck("soc", None)], [ConstraintCheck("soc", False)]
])
def test_safe_management_requires_known_passing_checks(plan, checks):
    result = validate(replace(plan.management, checks=checks))
    assert not result.valid


def test_soc_below_limit_cannot_be_safe(plan):
    management = replace(plan.management, soc_final=0.1, soc_trajectory=[SocPoint("s1", 0.1)])
    assert validate(management).status == Status.INVALID_INPUT


def test_required_energy_must_fit_budget(plan):
    assert validate(replace(plan.management, required_energy_kwh=80)).status == Status.INVALID_INPUT


def test_success_requires_management(plan):
    assert not validate(replace(plan, management=None)).valid


def test_unsafe_management_cannot_be_reported_as_success(plan):
    value = replace(plan, management=replace(plan.management, safe=False))
    assert validate(value).status == Status.INVALID_INPUT


def test_unfinished_hard_check_blocks_success(plan):
    optimization = replace(plan.optimization, checks=[
        ConstraintCheck("time", True), ConstraintCheck("speed", True), ConstraintCheck("power", None)
    ])
    assert not validate(replace(plan, optimization=optimization)).valid


def test_route_order_is_checked(plan):
    energy = replace(plan.optimization.energy_results[0], segment_id="wrong")
    optimization = replace(plan.optimization, energy_results=[energy])
    assert validate(replace(plan, optimization=optimization)).status == Status.INVALID_INPUT


def test_request_and_vessel_soc_must_match(plan):
    data = replace(plan.data, vessel=replace(plan.data.vessel, soc_initial=0.8))
    assert validate(replace(plan, data=data)).status == Status.INVALID_INPUT


def test_eta_needs_departure(plan):
    assert not validate(replace(plan, request=replace(plan.request, departure_at=None))).valid


def test_propulsion_scope_requires_auxiliary_energy(plan):
    energy = replace(plan.optimization.energy_results[0], energy_scope="propulsion")
    optimization = replace(plan.optimization, energy_scope="propulsion", energy_results=[energy])
    management = replace(plan.management, auxiliary_energy_kwh=None)
    value = replace(plan, optimization=optimization, management=management)
    assert "management.auxiliary_energy_kwh" in validate(value).missing_fields


def test_total_scope_does_not_add_auxiliary_energy_twice(plan):
    management = replace(plan.management, auxiliary_energy_kwh=0.5)
    assert validate(replace(plan, management=management)).valid


def test_tool_clarification_includes_questions():
    response = ToolResponse("Tdata", Status.NEED_CLARIFICATION,
                            missing_fields=["soc_initial"], questions=["初始 SOC 是多少？"])
    assert validate(response).valid
    assert not validate(replace(response, questions=[])).valid


def test_failed_tool_never_passes_as_ok():
    response = ToolResponse("Tenergy", Status.FAILED, reason="测试异常")
    assert validate(response).valid
    assert response.status == Status.FAILED
    assert not validate(replace(response, status=Status.OK)).valid


def test_require_valid_raises_for_incomplete_request():
    with pytest.raises(SchemaValidationError) as error:
        require_valid(VoyageRequest())
    assert error.value.result.status == Status.NEED_CLARIFICATION


def test_actual_duration_limit_overrides_claimed_passing_check(plan):
    limited_request = replace(plan.request, max_duration_h=0.05)
    assert validate(replace(plan, request=limited_request)).status == Status.INVALID_INPUT


def test_actual_deadline_overrides_claimed_passing_check(plan):
    limited_request = replace(plan.request, arrival_deadline="2026-09-18T09:05:00+08:00")
    assert validate(replace(plan, request=limited_request)).status == Status.INVALID_INPUT


def test_duration_must_match_distance_and_waiting(plan):
    segment = replace(plan.segments[0], distance_km=2.0)
    assert validate(replace(plan, segments=[segment])).status == Status.INVALID_INPUT


def test_peak_power_limit_is_checked(plan):
    energy = replace(plan.optimization.energy_results[0], peak_power_kw=30.0)
    optimization = replace(plan.optimization, energy_results=[energy])
    assert validate(replace(plan, optimization=optimization)).status == Status.INVALID_INPUT


def test_unknown_peak_power_blocks_final_success(plan):
    energy = replace(plan.optimization.energy_results[0], peak_power_kw=None)
    optimization = replace(plan.optimization, energy_results=[energy])
    result = validate(replace(plan, optimization=optimization))
    assert "optimization.energy_results.peak_power_kw" in result.missing_fields


def test_management_cannot_change_adopted_capacity(plan):
    management = replace(plan.management, capacity_kwh=200.0)
    assert validate(replace(plan, management=management)).status == Status.INVALID_INPUT


def test_management_cannot_relax_adopted_soc_limit(plan):
    management = replace(plan.management, soc_min=0.1)
    assert validate(replace(plan, management=management)).status == Status.INVALID_INPUT


def test_soc_trajectory_must_match_selected_segments(plan):
    management = replace(plan.management, soc_trajectory=[SocPoint("wrong", 0.89)])
    assert validate(replace(plan, management=management)).status == Status.INVALID_INPUT
