from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import replace

import pytest

from schemas.research_management import BatteryManagementRequest
from scripts.fdp_research_cases import (
    build_full_voyage_request,
    build_request,
    invalid_missing_efficiency_request,
)
from tools.fdp_verifier import verify_trajectory
from tools.tmanagement_fdp import compare_policies, run_management_fdp


EPS = 1e-9
MODES = (1, 2, 3)


def _request_with_demands(demands: list[float]) -> BatteryManagementRequest:
    base = build_request("success")
    points = []
    offset = 0.0
    for index, demand in enumerate(demands):
        duration = 600.0
        phase = "waiting" if demand == 0 else "sailing"
        propulsion = demand - 5.0 if demand > 0 else 0.0
        point = replace(
            base.demand_series[0],
            segment_id=f"fixture-leg-{index // 2 + 1}",
            phase=phase,
            start_offset_s=offset,
            duration_s=duration,
            demand_bus_kw=demand,
            motor_power_kw=max(propulsion - 1.0, 0.0),
            propulsion_bus_kw=propulsion,
            auxiliary_bus_kw=demand - propulsion,
        )
        points.append(point)
        offset += duration
    return replace(base, demand_series=points)


def _floor_to_grid(soc: float, minimum: float, step: float) -> float:
    if soc < minimum - EPS:
        return minimum - step
    return minimum + math.floor((soc - minimum + EPS) / step) * step


def _exhaustive_oracle(request: BatteryManagementRequest) -> dict:
    """Enumerate every legal mode path using the documented transition equations."""
    batteries = (request.battery_1, request.battery_2)
    step = request.grid.soc_step
    initial = {
        "soc1": _floor_to_grid(batteries[0].initial_soc, batteries[0].soc_min, step),
        "soc2": _floor_to_grid(batteries[1].initial_soc, batteries[1].soc_min, step),
        "actual1": batteries[0].initial_soc,
        "actual2": batteries[1].initial_soc,
        "mode": request.previous_mode,
        "previous_mode": request.previous_mode,
        "costs": {"power": 0.0, "switch": 0.0,
                  "soc_balance": 0.0, "low_load_mode3": 0.0},
        "rows": [],
    }
    # Keep every legal path. Merging by (grid SOC, mode) here would repeat
    # the FDP algorithm and could conceal a bad dominance decision.
    states = [initial]

    for point in request.demand_series:
        next_states = []
        for state in states:
            choices = ((state["mode"],) if point.demand_bus_kw <= EPS else MODES)
            for mode in choices:
                demand = point.demand_bus_kw
                prior_mode = state["previous_mode"]
                if mode is None:
                    if demand > EPS or prior_mode is not None:
                        continue
                    p1 = p2 = 0.0
                else:
                    if demand <= EPS and prior_mode is not None and mode != prior_mode:
                        continue
                    if (prior_mode is not None and mode != prior_mode
                            and (prior_mode, mode) not in request.allowed_mode_transitions):
                        continue
                    if mode == 1:
                        p1, p2 = demand, 0.0
                    elif mode == 2:
                        p1, p2 = 0.0, demand
                    else:
                        total_soc = state["actual1"] + state["actual2"]
                        if total_soc <= EPS:
                            continue
                        p1 = demand * state["actual1"] / total_soc
                        p2 = demand * state["actual2"] / total_soc

                powers = (p1, p2)
                feasible = True
                next_grid = []
                actual_after = []
                for battery, power, actual_soc in zip(
                        batteries, powers,
                        (state["actual1"], state["actual2"])):
                    if power > battery.max_power_kw + EPS:
                        feasible = False
                    if power > EPS and power < battery.min_power_kw - EPS:
                        feasible = False
                    next_soc = actual_soc - (
                        power * point.duration_s / 3600.0
                        / (battery.discharge_efficiency * battery.capacity_kwh)
                    )
                    snapped = _floor_to_grid(next_soc, battery.soc_min, step)
                    if next_soc < battery.soc_min - EPS or snapped < battery.soc_min - EPS:
                        feasible = False
                    next_grid.append(snapped)
                    actual_after.append(next_soc)
                if not feasible:
                    continue

                weights = request.weights
                switch = 0.0
                if (mode in MODES and prior_mode in MODES and mode != prior_mode
                        and not (mode in (1, 2) and prior_mode in (1, 2))):
                    switch = weights.k_s
                difference = abs(state["soc1"] - state["soc2"])
                components = {
                    "power": weights.k_p * (p1 * p1 + p2 * p2),
                    "switch": switch,
                    "soc_balance": (
                        weights.k_soc * difference if mode in (1, 2)
                        else 2.0 * weights.k_soc * difference if mode == 3 else 0.0
                    ),
                    "low_load_mode3": (
                        weights.k_low if mode == 3 and demand > EPS
                        and demand / request.load_rate_reference_kw
                        <= weights.low_load_threshold else 0.0
                    ),
                }
                costs = dict(state["costs"])
                for key, value in components.items():
                    costs[key] += value
                row = {
                    "mode": mode,
                    "battery_1_power_kw": p1,
                    "battery_2_power_kw": p2,
                    "soc_1_before": state["actual1"],
                    "soc_2_before": state["actual2"],
                    "soc_1_after": actual_after[0],
                    "soc_2_after": actual_after[1],
                    "planning_soc_1_before": state["soc1"],
                    "planning_soc_2_before": state["soc2"],
                    "planning_soc_1_after": next_grid[0],
                    "planning_soc_2_after": next_grid[1],
                    "cost_terms": components,
                }
                following_mode = (
                    mode if demand > EPS or prior_mode is not None else None
                )
                candidate = {
                    "soc1": next_grid[0], "soc2": next_grid[1],
                    "actual1": actual_after[0], "actual2": actual_after[1],
                    "mode": mode, "previous_mode": following_mode,
                    "costs": costs, "rows": [*state["rows"], row],
                }
                next_states.append(candidate)
        assert next_states, "fixture must have at least one legal complete prefix"
        states = next_states

    return min(states, key=lambda state: sum(state["costs"].values()))


@pytest.mark.parametrize(
    "demands",
    [
        [70.0, 0.0],
        [20.0, 80.0, 30.0],
        [30.0, 80.0, 0.0, 30.0],
    ],
)
def test_fdp_matches_exhaustive_mode_paths_for_two_to_four_steps(demands):
    request = _request_with_demands(demands)
    result = run_management_fdp(request)
    assert result["outcome_code"] == "ok"
    payload = result["payload"]
    assert payload["safe"] is True
    assert all(check["passed"] for check in payload["constraint_checks"])

    expected = _exhaustive_oracle(request)
    rows = payload["rows"]
    assert [row["mode"] for row in rows] == [
        row["mode"] for row in expected["rows"]
    ]
    for key in ("power", "switch", "soc_balance", "low_load_mode3"):
        assert payload["cost_terms"][key] == pytest.approx(expected["costs"][key])
    for actual, wanted in zip(rows, expected["rows"]):
        for key in (
            "battery_1_power_kw", "battery_2_power_kw",
            "soc_1_before", "soc_2_before", "soc_1_after", "soc_2_after",
            "planning_soc_1_before", "planning_soc_2_before",
            "planning_soc_1_after", "planning_soc_2_after",
        ):
            assert actual[key] == pytest.approx(wanted[key])
        for key in ("power", "switch", "soc_balance", "low_load_mode3"):
            assert actual["cost_terms"][key] == pytest.approx(wanted["cost_terms"][key])


def test_zero_load_retains_previous_mode_without_idle_low_load_penalty():
    request = _request_with_demands([70.0, 0.0])
    result = run_management_fdp(request)
    rows = result["payload"]["rows"]
    assert rows[0]["mode"] == 3
    assert rows[1]["mode"] == 3
    assert rows[1]["battery_1_power_kw"] == 0
    assert rows[1]["battery_2_power_kw"] == 0
    assert rows[1]["cost_terms"]["switch"] == 0
    assert rows[1]["cost_terms"]["low_load_mode3"] == 0


def test_short_intervals_do_not_consume_one_soc_grid_step_each():
    base = build_request("success")
    points = [replace(base.demand_series[0], start_offset_s=i * 5.0,
                      duration_s=5.0) for i in range(20)]
    result = run_management_fdp(replace(base, demand_series=points))
    assert result["outcome_code"] == "ok"
    rows = result["payload"]["rows"]
    assert rows[-1]["soc_1_after"] > 0.79
    assert rows[-1]["planning_soc_1_after"] >= 0.75
    assert all(check["passed"] for check in result["payload"]["constraint_checks"])


def test_persisted_time_rounding_does_not_break_contiguous_demand():
    from tools.tmanagement_fdp import validate_request

    base = build_request("success")
    first = replace(base.demand_series[0], start_offset_s=0.0, duration_s=4.352051836)
    rounded = replace(first, start_offset_s=4.352051835, duration_s=5.0)
    request = replace(base, demand_series=[first, rounded])
    assert not any(item["field_ref"].endswith("start_offset_s") for item in validate_request(request))
    gap = replace(rounded, start_offset_s=4.353051836)
    request = replace(base, demand_series=[first, gap])
    assert any(item["field_ref"].endswith("start_offset_s") for item in validate_request(request))


@pytest.mark.parametrize(
    ("case_id", "outcome"),
    [
        ("instantaneous_overload", "infeasible_under_assumptions"),
        ("soc_depletion", "grid_unresolved"),
        ("grid_budget", "grid_unresolved"),
        ("charging_unsupported", "unsupported_charging"),
    ],
)
def test_failure_outcomes_keep_scope_and_diagnostic_distinct(case_id, outcome):
    result = run_management_fdp(build_request(case_id))
    assert result["outcome_code"] == outcome
    if case_id == "charging_unsupported":
        assert result.get("payload") is None
        assert [item["code"] for item in result["diagnostics"]] == [
            "charging_not_supported"
        ]
        return
    assert result["payload"]["safe"] is False
    feedback = result["payload"]["feedback"]
    assert feedback["selected_candidate_id"]
    assert feedback["selected_demand_ref"]
    assert feedback["request_revision"] == 1
    assert feedback["snapshot_id"]
    assert feedback["scenario_id"]
    assert feedback["battery_config_hash"]
    assert feedback["first_failure_interval"]["start_offset_s"] == 0
    if case_id == "instantaneous_overload":
        assert feedback["failure_scope"] == "instantaneous_power"
        assert feedback["hard_constraint_applicable_to_candidate_family"] is True
        assert feedback["limit_source_ref"]
    elif case_id == "grid_budget":
        assert feedback["failure_scope"] == "grid_budget"
        assert feedback["diagnostic_certainty"] == "transition_budget_exhausted"
    elif case_id == "soc_depletion":
        assert feedback["failure_scope"] == "candidate_grid_path"
        assert feedback["deliverable_kw"] is None
        assert feedback["diagnostic_certainty"] == "no_path_on_requested_grid"
        assert result["diagnostics"][0]["code"] == "no_path_on_requested_grid"
    else:
        assert result["diagnostics"][0]["code"] == "charging_not_supported"


def test_missing_battery_efficiency_stays_missing_and_returns_needs_input():
    request = BatteryManagementRequest.from_dict(invalid_missing_efficiency_request())
    result = run_management_fdp(request)
    assert result["outcome_code"] == "needs_input"
    assert result.get("payload") is None
    assert any(
        item["field_ref"] == "battery_1.discharge_efficiency"
        for item in result["diagnostics"]
    )


def test_long_horizon_budget_retry_keeps_unresolved_distinct_from_infeasible():
    """A bounded adaptive retry can finish a long horizon without hiding failure."""
    base = build_request("success")
    points = []
    for index in range(160):
        points.append(replace(
            base.demand_series[0],
            segment_id=f"long-leg-{index // 20}",
            start_offset_s=index * 60.0,
            duration_s=60.0,
            demand_bus_kw=70.0,
            motor_power_kw=64.0,
            propulsion_bus_kw=65.0,
            auxiliary_bus_kw=5.0,
        ))
    request = replace(
        base,
        demand_series=points,
        grid=replace(base.grid, soc_step=0.01, max_states_per_layer=5000,
                     max_transitions=100),
        battery_1=replace(base.battery_1, capacity_kwh=100000.0),
        battery_2=replace(base.battery_2, capacity_kwh=100000.0),
    )
    result = run_management_fdp(request)
    assert result["outcome_code"] in ("ok", "grid_unresolved")
    payload = result.get("payload") or {}
    if result["outcome_code"] == "ok":
        assert payload["safe"] is True
        assert payload["search_strategy"] == "bounded_adaptive_grid"
        assert payload["search_attempts"][0]["outcome_code"] == "grid_unresolved"
        assert all(check["passed"] for check in payload["constraint_checks"])
    else:
        assert result["outcome_code"] != "infeasible_under_assumptions"
        assert payload["search_strategy"] == "bounded_adaptive_grid"
        assert payload["search_attempts"][-1]["outcome_code"] == "grid_unresolved"


def test_coarser_retry_cannot_turn_original_budget_miss_into_infeasibility(monkeypatch):
    import tools.tmanagement_fdp as fdp

    outcomes = iter([
        {"outcome_code": "grid_unresolved", "payload": {"budget": "transitions"}, "diagnostics": []},
        {"outcome_code": "infeasible_under_assumptions", "payload": {"grid": "coarser"}, "diagnostics": []},
    ])
    monkeypatch.setattr(fdp, "_run_management_fdp_once", lambda _request: next(outcomes))
    result = fdp.run_management_fdp(build_request("success"))
    assert result["outcome_code"] == "grid_unresolved"
    assert result["payload"]["budget"] == "transitions"
    assert result["payload"]["retry_result"] == {"grid": "coarser"}
    assert [attempt["outcome_code"] for attempt in result["search_attempts"]] == [
        "grid_unresolved", "infeasible_under_assumptions"]


def test_missing_adapter_components_do_not_turn_into_zero():
    request = build_request("success")
    points = list(request.demand_series)
    points[0] = replace(points[0], auxiliary_bus_kw=None)
    result = run_management_fdp(replace(request, demand_series=points))
    assert result["outcome_code"] == "needs_input"
    assert any(item["field_ref"] == "demand_series[0].auxiliary_bus_kw"
               for item in result["diagnostics"])


def test_non_finite_input_has_a_json_safe_invalid_result():
    request = build_request("success")
    points = list(request.demand_series)
    points[0] = replace(points[0], demand_bus_kw=float("nan"))
    result = run_management_fdp(replace(request, demand_series=points))
    assert result["outcome_code"] == "invalid_input"
    assert len(result["input_hash"]) == 64
    json.dumps(result, allow_nan=False)


def test_independent_verifier_rejects_interval_and_energy_tampering():
    request = build_request("success")
    result = run_management_fdp(request)
    payload = result["payload"]
    assert payload["safe"] is True
    for field, value in (
        ("segment_id", "wrong-segment"),
        ("phase", "waiting"),
        ("battery_1_energy_drawn_kwh", -1.0),
        ("planning_soc_1_before", 0.99),
        ("cost_terms", {"power": "not-a-number"}),
    ):
        rows = copy.deepcopy(payload["rows"])
        rows[0][field] = value
        check = verify_trajectory(request, rows, payload["cost_terms"])
        assert check["passed"] is False, field


def test_fdp_and_state_machine_comparison_reports_all_requested_metrics():
    comparison = compare_policies(build_request("success"))
    assert comparison["fdp_outcome"] == "ok"
    assert comparison["state_machine_outcome"] == "ok"
    assert comparison["fdp"]["feasible"] is True
    assert comparison["state_machine"]["feasible"] is True
    for policy in ("fdp", "state_machine"):
        metrics = comparison[policy]
        assert "final_soc_difference" in metrics
        assert "max_soc_difference" in metrics
        assert "power_variation_1_kw" in metrics
        assert "power_variation_2_kw" in metrics
        assert "switch_count" in metrics
        assert set(metrics["cost_terms"]) >= {
            "power", "switch", "soc_balance", "low_load_mode3", "total"
        }
    assert comparison["comparison"]["constraint_violations"] == {
        "fdp": 0, "state_machine": 0
    }


def test_full_voyage_energy_summaries_and_artifact_hashes_recompute():
    request = build_full_voyage_request()
    result = run_management_fdp(request)
    payload = result["payload"]
    assert result["outcome_code"] == "ok"
    assert payload["safe"] is True
    rows = payload["rows"]
    summary = payload["summary"]
    assert len(rows) == 48
    assert sum(row["duration_s"] for row in rows) == 86400
    assert summary["final_soc_1"] == pytest.approx(rows[-1]["soc_1_after"])
    assert summary["final_soc_2"] == pytest.approx(rows[-1]["soc_2_after"])
    assert summary["bus_energy_1_kwh"] == pytest.approx(
        sum(row["battery_1_bus_energy_kwh"] for row in rows)
    )
    assert summary["bus_energy_2_kwh"] == pytest.approx(
        sum(row["battery_2_bus_energy_kwh"] for row in rows)
    )
    assert summary["demand_bus_energy_kwh"] == pytest.approx(
        sum(row["demand_bus_energy_kwh"] for row in rows)
    )
    assert summary["mode_counts"] == {
        str(mode): sum(row["mode"] == mode for row in rows) for mode in MODES
    }
    artifact_rows = {
        "mode_trajectory_ref": rows,
        "battery_1_power_ref": [
            {key: row[key] for key in
             ("start_offset_s", "duration_s", "battery_1_power_kw")}
            for row in rows
        ],
        "battery_2_power_ref": [
            {key: row[key] for key in
             ("start_offset_s", "duration_s", "battery_2_power_kw")}
            for row in rows
        ],
        "battery_1_soc_ref": [
            {key: row[key] for key in
             ("start_offset_s", "soc_1_before", "soc_1_after")}
            for row in rows
        ],
        "battery_2_soc_ref": [
            {key: row[key] for key in
             ("start_offset_s", "soc_2_before", "soc_2_after")}
            for row in rows
        ],
    }
    for name, values in artifact_rows.items():
        artifact = payload[name]
        serialized = json.dumps(
            values, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        assert artifact["row_count"] == len(values)
        assert artifact["sha256"] == hashlib.sha256(serialized).hexdigest()
        assert artifact["schema_version"] == request.schema_version
