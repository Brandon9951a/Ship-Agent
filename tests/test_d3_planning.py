from dataclasses import replace

import pytest

from schemas.types import (
    DataConflict, DataContext, EnergyResult, InfeasibleType, ManagementPlan,
    OptimizationResult, ParameterValue, Segment,
    SourceRef, Status, VesselState, VoyageRequest,
)
from schemas.validate import validate
from tools.tenergy import EnergyModel, run_tenergy
from tools.tmanagement import run_tmanagement
from tools.tspeed import run_tspeed


DOC = SourceRef("A-approved-test-fixture", "assumption", "tests", False,
                "仅自动测试，不是实船采用值")
USER = SourceRef("test-request", "user", confirmed=True)
SEGMENTS = [
    Segment("s1", "甲", "乙", 8, max_speed_kmh=6, waiting_h=0, source=DOC),
    Segment("s2", "乙", "丙", 4, max_speed_kmh=6, waiting_h=0, source=DOC),
]
REQUEST = VoyageRequest("甲", "丙", max_duration_h=2.5, soc_initial=.8,
                        load_state="test-only")


def vessel(**changes):
    values = dict(vessel_id="test", capacity_kwh=100, soc_initial=.8, soc_min=.2,
                  soc_alarm=.4, max_power_kw=50, auxiliary_power_kw=2,
                  sources={name: DOC for name in (
                      "capacity_kwh", "soc_initial", "soc_min", "max_power_kw",
                      "auxiliary_power_kw")}, assumptions=["synthetic_test_only"])
    values.update(changes)
    return VesselState(**values)


def candidates(scope="total", speeds=(4, 5)):
    model = EnergyModel("cubic-test", .125, 2, scope, "synthetic_demo")
    response = run_tenergy(SEGMENTS, list(speeds), model)
    assert response.status == Status.OK
    return [EnergyResult.from_dict(item) for item in response.payload["candidate_results"]]


def selected(scope="total", request=REQUEST, current_vessel=None):
    response = run_tspeed(request, SEGMENTS, candidates(scope), current_vessel or vessel())
    assert response.status == Status.OK
    return OptimizationResult.from_dict(response.payload)


def test_normal_grid_search_and_management_are_contract_valid():
    speed = run_tspeed(REQUEST, SEGMENTS, candidates(), vessel())
    assert speed.status == Status.OK and validate(speed).valid
    optimization = OptimizationResult.from_dict(speed.payload)
    assert [item.speed_kmh for item in optimization.energy_results] == [5, 5]
    assert optimization.total_duration_h == pytest.approx(2.4)
    assert {check.name for check in optimization.checks} == {"time", "speed", "power", "soc"}

    management = run_tmanagement(REQUEST, DataContext(vessel(), "test-route"), optimization)
    assert management.status == Status.OK and validate(management).valid
    plan = management.payload
    assert plan["required_energy_kwh"] == pytest.approx(optimization.total_energy_kwh)
    assert plan["auxiliary_energy_kwh"] == pytest.approx(4.8)
    assert plan["soc_final"] == pytest.approx(.8 - optimization.total_energy_kwh / 100)
    assert plan["charge_required_kwh"] == 0
    assert len(plan["soc_trajectory"]) == 2


def test_energy_peak_is_modeled_not_average_substitution():
    result = candidates()[0]
    assert result.power_kw == 10
    assert result.peak_power_kw == 10
    waiting = [Segment("wait", "甲", "乙", 4, max_speed_kmh=6, waiting_h=1, source=DOC)]
    response = run_tenergy(waiting, [4], EnergyModel("demo", .125, 2, "total", "synthetic_demo"))
    item = response.payload["candidate_results"][0]
    assert item["power_kw"] == 6
    assert item["peak_power_kw"] == 10


@pytest.mark.parametrize("voyage_request,missing", [
    (replace(REQUEST, max_duration_h=None), "request.arrival_deadline_or_max_duration_h"),
    (replace(REQUEST, soc_initial=None), "request.soc_initial"),
])
def test_speed_missing_request_constraints_are_questions(voyage_request, missing):
    response = run_tspeed(voyage_request, SEGMENTS, candidates(), vessel())
    assert response.status == Status.NEED_CLARIFICATION
    assert missing in response.missing_fields and validate(response).valid


def test_speed_requires_value_source_and_candidate_peak():
    current = vessel(sources={name: DOC for name in (
        "capacity_kwh", "soc_initial", "soc_min", "auxiliary_power_kw")})
    response = run_tspeed(REQUEST, SEGMENTS, candidates(), current)
    assert response.status == Status.NEED_CLARIFICATION
    assert "vessel.sources.max_power_kw" in response.missing_fields
    no_peak = [replace(item, peak_power_kw=None) for item in candidates()]
    response = run_tspeed(REQUEST, SEGMENTS, no_peak, vessel())
    assert response.status == Status.NEED_CLARIFICATION
    assert response.missing_fields[0].endswith("peak_power_kw")


def test_power_time_and_soc_failures_are_classified():
    power = run_tspeed(REQUEST, SEGMENTS, candidates(), vessel(max_power_kw=1))
    assert power.status == Status.INFEASIBLE and power.infeasible_type == InfeasibleType.POWER
    time = run_tspeed(replace(REQUEST, max_duration_h=1), SEGMENTS, candidates(), vessel())
    assert time.status == Status.INFEASIBLE and time.infeasible_type == InfeasibleType.TIME
    soc = run_tspeed(REQUEST, SEGMENTS, candidates(), vessel(soc_initial=.3),)
    assert soc.status == Status.INVALID_INPUT  # request/vessel SOC mismatch is never silently reconciled
    low_request = replace(REQUEST, soc_initial=.3)
    soc = run_tspeed(low_request, SEGMENTS, candidates(), vessel(soc_initial=.3))
    assert soc.status == Status.INFEASIBLE and soc.infeasible_type == InfeasibleType.SOC


def test_candidate_duration_and_grid_size_are_guarded():
    bad = candidates()
    bad[0] = replace(bad[0], duration_h=99, power_kw=bad[0].energy_kwh / 99)
    response = run_tspeed(REQUEST, SEGMENTS, bad, vessel())
    assert response.status == Status.INVALID_INPUT and "耗时" in response.reason
    response = run_tspeed(REQUEST, SEGMENTS, candidates(), vessel(), max_combinations=3)
    assert response.status == Status.INVALID_INPUT and "超过上限" in response.reason
    duplicate = candidates() + [candidates()[0]]
    response = run_tspeed(REQUEST, SEGMENTS, duplicate, vessel())
    assert response.status == Status.INVALID_INPUT and "不能重复" in response.reason


def test_propulsion_scope_adds_auxiliary_once_in_soc_plan():
    optimization = selected("propulsion")
    management = run_tmanagement(REQUEST, DataContext(vessel(), "test-route"), optimization)
    assert management.status == Status.OK
    plan = management.payload
    expected_aux = 2 * optimization.total_duration_h
    assert plan["auxiliary_energy_kwh"] == pytest.approx(expected_aux)
    assert plan["required_energy_kwh"] == pytest.approx(
        optimization.total_energy_kwh + expected_aux)
    assert "已加到推进能耗口径" in plan["assumptions"][-1]


def test_propulsion_search_minimizes_total_requirement_including_auxiliary():
    segment = Segment("s", "甲", "乙", 1, max_speed_kmh=3, waiting_h=0, source=DOC)
    options = [
        EnergyResult("s", 1, 1, 1, 1, "propulsion", "test", 5),
        EnergyResult("s", 2, 6, .5, 3, "propulsion", "test", 7),
    ]
    request = VoyageRequest("甲", "乙", max_duration_h=2, soc_initial=.8,
                            load_state="test-only")
    current = vessel(auxiliary_power_kw=10)
    response = run_tspeed(request, [segment], options, current)
    assert response.status == Status.OK
    # 1 km/h uses 1 + 10*1 = 11 kWh; 2 km/h uses 3 + 10*.5 = 8 kWh.
    assert response.payload["energy_results"][0]["speed_kmh"] == 2


def test_management_reports_recharge_without_marking_safe():
    # Build a selected result under a larger planning budget, then evaluate the
    # same immutable energy plan against a deliberately smaller test budget.
    optimization = selected()
    low = vessel(capacity_kwh=50, soc_initial=.5, soc_min=.4)
    request = replace(REQUEST, soc_initial=.5)
    response = run_tmanagement(request, DataContext(low, "test-route"), optimization)
    assert response.status == Status.INFEASIBLE
    assert response.infeasible_type == InfeasibleType.SOC
    assert response.payload["safe"] is False
    assert validate(ManagementPlan.from_dict(response.payload)).valid
    assert response.payload["charge_required_kwh"] > 0
    assert "未执行补能" in response.reason


def test_management_missing_parameters_and_upstream_failure_stop():
    optimization = selected()
    missing = vessel(auxiliary_power_kw=None)
    response = run_tmanagement(REQUEST, DataContext(missing, "test-route"), optimization)
    assert response.status == Status.NEED_CLARIFICATION
    assert "data.vessel.auxiliary_power_kw" in response.missing_fields
    failed = OptimizationResult(False, "total", infeasible_type=InfeasibleType.TIME,
                                reason="测试时间不可行")
    response = run_tmanagement(REQUEST, DataContext(vessel(), "test-route"), failed)
    assert response.status == Status.INFEASIBLE and response.infeasible_type == InfeasibleType.TIME


def test_management_stops_on_tdata_missing_or_unresolved_conflict():
    optimization = selected()
    conflict = DataConflict("capacity_kwh", [ParameterValue(100, "kWh", DOC)])
    data = DataContext(vessel(), "test-route", conflicts=[conflict],
                       missing_fields=["route.constraints"])
    response = run_tmanagement(REQUEST, data, optimization)
    assert response.status == Status.NEED_CLARIFICATION
    assert "data.route.constraints" in response.missing_fields
    assert "data.conflicts.capacity_kwh" in response.missing_fields


@pytest.mark.parametrize("voyage_request,current_vessel", [
    (replace(REQUEST, max_duration_h=0), vessel()),
    (REQUEST, vessel(max_power_kw=0)),
])
def test_zero_hard_limits_are_invalid(voyage_request, current_vessel):
    response = run_tspeed(voyage_request, SEGMENTS, candidates(), current_vessel)
    assert response.status == Status.INVALID_INPUT
