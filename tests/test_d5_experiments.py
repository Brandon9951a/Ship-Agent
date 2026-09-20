import csv
import json

import pytest

from scripts.validate_d5 import load_experiments, run_batch, write_evidence


@pytest.fixture(scope="module")
def report():
    return run_batch()


def test_batch_has_40_verified_cases_and_both_directions(report):
    assert report["status"] == "passed"
    assert report["aggregate"]["case_count"] == 40
    assert report["aggregate"]["verified_case_count"] == 40
    pairs = {(item["origin"], item["destination"]) for item in report["results"]}
    assert ("平顶山港", "马湾船闸") in pairs
    assert ("马湾船闸", "平顶山港") in pairs


def test_every_case_matches_independent_grid_reference(report):
    for item in report["results"]:
        assert item["verification"] == {
            "status_match": True,
            "optimum_match": True,
            "distance_match": True,
        }
        assert item["status"] == item["reference"]["expected_status"]
        assert item["infeasible_type"] == item["reference"]["expected_infeasible_type"]


def test_comparison_uses_same_constraints_and_never_claims_real_ship_saving(report):
    assert report["real_ship_validation"] is False
    assert report["aggregate"]["comparable_case_count"] >= 24
    assert report["aggregate"]["aggregate_energy_difference_percent"] > 0
    for item in report["results"]:
        assert item["real_ship_validation"] is False
        if item["comparison"] is None:
            assert item["status"] != "ok"
            continue
        comparison = item["comparison"]
        assert comparison["same_constraints"] is True
        assert comparison["optimized_required_energy_kwh"] <= (
            comparison["baseline_required_energy_kwh"] + 1e-9
        )
        assert comparison["energy_difference_percent"] >= -1e-9


def test_tight_deadline_reproduces_fastest_baseline(report):
    tight = [
        item for item in report["results"]
        if item["profile_id"] == "tight_high_soc"
    ]
    assert len(tight) == 8
    for item in tight:
        assert item["comparison"]["energy_difference_kwh"] == pytest.approx(0)


def test_reserve_only_cases_are_soc_infeasible_without_fake_comparison(report):
    reserve = [
        item for item in report["results"]
        if item["profile_id"] == "reserve_only"
    ]
    assert len(reserve) == 8
    assert all(item["status"] == "infeasible" for item in reserve)
    assert all(item["infeasible_type"] == "soc" for item in reserve)
    assert all(item["comparison"] is None for item in reserve)


def test_evidence_writer_emits_raw_csv_chart_and_hashes(tmp_path, report):
    output = tmp_path / "d5-evidence"
    write_evidence(report, output)
    expected = {
        "raw_results.json", "aggregate.json", "cases.csv",
        "energy_comparison.svg", "evidence_manifest.json",
    }
    assert {item.name for item in output.iterdir()} == expected
    restored = json.loads((output / "raw_results.json").read_text(encoding="utf-8"))
    assert restored["aggregate"]["case_count"] == 40
    with (output / "cases.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 40
    assert all(row["verified"] == "true" for row in rows)
    svg = (output / "energy_comparison.svg").read_text(encoding="utf-8")
    assert "非实船节能率" in svg and svg.startswith("<svg")
    manifest = json.loads(
        (output / "evidence_manifest.json").read_text(encoding="utf-8")
    )
    assert set(manifest["files"]) == expected - {"evidence_manifest.json"}


def test_evidence_writer_refuses_overwrite(tmp_path, report):
    output = tmp_path / "d5-evidence"
    write_evidence(report, output)
    with pytest.raises(FileExistsError):
        write_evidence(report, output)


def test_config_rejects_real_ship_claim_and_small_batch(tmp_path):
    source = load_experiments()
    source["real_ship_validation"] = True
    path = tmp_path / "bad-real.json"
    path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="不得标记为实船验证"):
        load_experiments(path)

    source["real_ship_validation"] = False
    source["routes"] = source["routes"][:1]
    path = tmp_path / "bad-small.json"
    path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="至少展开30项"):
        load_experiments(path)
