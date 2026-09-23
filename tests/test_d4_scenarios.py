import csv
import json

import pytest

from scripts.validate_d4 import load_scenarios, run_suite, write_report


@pytest.fixture(scope="module")
def report():
    return run_suite()


def test_three_required_scenarios_pass_expected_boundaries(report):
    assert report["status"] == "passed"
    assert report["scenario_count"] == 3
    actual = {
        item["id"]: (item["status"], item["infeasible_type"])
        for item in report["scenarios"]
    }
    assert actual == {
        "normal": ("ok", None),
        "time_infeasible": ("infeasible", "time"),
        "low_soc": ("infeasible", "soc"),
    }


def test_all_scenarios_reuse_same_checked_route_and_demo_grid(report):
    distances = {item["route_distance_km"] for item in report["scenarios"]}
    segment_ids = {
        tuple(segment["segment_id"] for segment in item["segments"])
        for item in report["scenarios"]
    }
    model_ids = {item["model"]["model_id"] for item in report["scenarios"]}
    assert distances == {50.5}
    assert len(segment_ids) == 1
    assert len(model_ids) == 1


def test_demo_speed_ceiling_is_explicitly_not_a_route_limit(report):
    for item in report["scenarios"]:
        for segment in item["segments"]:
            assert segment["max_speed_kmh"] == pytest.approx(11.112)
            assert any("不是通航限速" in note for note in segment["assumptions"])
        assert item["upstream"]["demo_resolution"]["speed_ceiling_kmh"] == pytest.approx(11.112)
        assert item["upstream"]["demo_resolution"]["speed_ceiling_role"] == (
            "synthetic_demo_operating_cap_not_legal_waterway_limit"
        )


def test_effective_capacity_and_power_boundary_are_not_mixed(report):
    normal = report["scenarios"][0]
    management = normal["tmanagement"]["payload"]
    assert management["capacity_kwh"] == pytest.approx(1567.85)
    assert normal["model"]["energy_scope"] == "propulsion"
    assert management["required_energy_kwh"] == pytest.approx(
        normal["tspeed"]["payload"]["total_energy_kwh"]
        + management["auxiliary_energy_kwh"]
    )


def test_normal_scenario_has_traceable_soc_result(report):
    normal = report["scenarios"][0]
    plan = normal["tmanagement"]["payload"]
    assert plan["safe"] is True
    assert plan["soc_final"] >= plan["soc_min"]
    assert plan["charge_required_kwh"] == 0
    assert len(plan["soc_trajectory"]) == 2


def test_infeasible_scenarios_never_report_management_success(report):
    for item in report["scenarios"][1:]:
        assert item["tmanagement"] is None
        assert item["tspeed"]["status"] == "infeasible"
        assert item["tspeed"]["payload"]["feasible"] is False


def test_infeasible_boundaries_are_quantified_without_claiming_a_plan(report):
    time_case = report["scenarios"][1]["boundary_diagnostics"]
    low_soc = report["scenarios"][2]["boundary_diagnostics"]
    assert time_case["fastest_duration_h"] == pytest.approx(50.5 / 11.112)
    assert time_case["time_shortfall_h"] == pytest.approx(50.5 / 11.112 - 4)
    assert time_case["minimum_required_energy_within_time_kwh"] is None
    assert low_soc["minimum_charge_required_within_time_kwh"] > 0
    assert low_soc["diagnostic_only"] is True


def test_every_result_is_marked_non_vessel(report):
    assert report["real_ship_validation"] is False
    assert all(item["real_ship_validation"] is False for item in report["scenarios"])
    assert all(
        result["model"]["calibration"] == "derived_demo_point_not_real_voyage_fit"
        for result in report["scenarios"]
    )


def test_report_writer_emits_reproducible_json_and_csv(tmp_path, report):
    output = tmp_path / "d4-results"
    write_report(report, output)
    restored = json.loads((output / "results.json").read_text(encoding="utf-8"))
    with (output / "summary.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert restored == report
    assert [row["scenario"] for row in rows] == [
        "normal", "time_infeasible", "low_soc"
    ]
    assert all(row["real_ship_validation"] == "false" for row in rows)


def test_report_writer_refuses_overwrite(tmp_path, report):
    output = tmp_path / "d4-results"
    write_report(report, output)
    with pytest.raises(FileExistsError):
        write_report(report, output)


def test_configuration_rejects_a_false_real_ship_claim(tmp_path):
    source = load_scenarios()
    source["real_ship_validation"] = True
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="不得标记为实船验证"):
        load_scenarios(path)
