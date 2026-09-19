import json
from pathlib import Path

import pytest

from schemas.types import Segment, SourceRef, Status
from schemas.validate import validate
from tools.tenergy import EnergyModel, run_tenergy


SOURCE = SourceRef("route-test", "assumption", confirmed=False)
SEGMENTS = [Segment("s1", "甲", "乙", 8, max_speed_kmh=10, waiting_h=0.5, source=SOURCE)]
DEMO = EnergyModel("cubic-demo-v1", 0.125, 2, "total", "synthetic_demo")


def test_demo_tenergy_returns_valid_contract_and_is_labeled():
    response = run_tenergy(SEGMENTS, [4, 5], DEMO)
    assert response.status == Status.OK and validate(response).valid
    first = response.payload["candidate_results"][0]
    assert first["duration_h"] == 2.5 and first["energy_kwh"] == 21
    assert "synthetic_demo_only" in first["assumptions"][0]


def test_tenergy_preserves_segment_then_speed_order():
    segments = SEGMENTS + [Segment("s2", "乙", "丙", 1, max_speed_kmh=10,
                                  waiting_h=0, source=SOURCE)]
    response = run_tenergy(segments, [4, 5], DEMO)
    assert [(item["segment_id"], item["speed_kmh"])
            for item in response.payload["candidate_results"]] == [
                ("s1", 4), ("s1", 5), ("s2", 4), ("s2", 5)]


@pytest.mark.parametrize("segments,speeds,model,missing", [
    ([], [4], DEMO, "segments"),
    (SEGMENTS, [], DEMO, "candidate_speeds_kmh"),
    (SEGMENTS, [4], None, "model"),
    (SEGMENTS, [4], EnergyModel(), "model.model_id"),
    (SEGMENTS, [4], EnergyModel("m", .1, 2, "total", "approved"), "model.approval_ref"),
])
def test_missing_inputs_request_clarification(segments, speeds, model, missing):
    response = run_tenergy(segments, speeds, model)
    assert response.status == Status.NEED_CLARIFICATION
    assert missing in response.missing_fields and response.questions
    assert validate(response).valid


def test_unknown_waiting_is_not_silently_zero():
    segment = Segment("s1", "甲", "乙", 8, max_speed_kmh=10, source=SOURCE)
    response = run_tenergy([segment], [4], DEMO)
    assert response.status == Status.NEED_CLARIFICATION
    assert response.missing_fields == ["segments[0].waiting_h"]


def test_demo_waiting_assumption_is_explicitly_labeled():
    segment = Segment("s1", "甲", "乙", 8, max_speed_kmh=10, source=SOURCE)
    model = EnergyModel("demo", .125, 2, "total", "synthetic_demo", default_waiting_h=0)
    result = run_tenergy([segment], [4], model)
    assert result.status == Status.OK
    assert any("等待时间使用演示假设" in item for item in
               result.payload["candidate_results"][0]["assumptions"])


def test_approved_model_requires_per_segment_waiting_and_reference():
    model = EnergyModel("approved-v1", .125, 2, "total", "approved", "A记录", 0)
    response = run_tenergy(SEGMENTS, [4], model)
    assert response.status == Status.INVALID_INPUT
    assert "不得用默认等待时间" in response.reason


@pytest.mark.parametrize("speeds", [[0], [4, 4], [True], [float("inf")]])
def test_invalid_candidate_speeds_are_rejected(speeds):
    response = run_tenergy(SEGMENTS, speeds, DEMO)
    assert response.status == Status.INVALID_INPUT and response.reason


def test_segment_speed_limit_is_enforced():
    segment = Segment("s1", "甲", "乙", 8, max_speed_kmh=3, waiting_h=0, source=SOURCE)
    response = run_tenergy([segment], [4], DEMO)
    assert response.status == Status.INVALID_INPUT and "above limit" in response.reason


def test_unknown_model_usage_cannot_produce_unlabeled_result():
    model = EnergyModel("bad", .125, 2, "total", "production")
    response = run_tenergy(SEGMENTS, [4], model)
    assert response.status == Status.INVALID_INPUT and "usage" in response.reason


def test_current_c_route_fact_surfaces_unconfirmed_limit_and_waiting():
    route = json.loads((Path(__file__).parents[1] / "configs/route_facts.yaml").read_text(encoding="utf-8"))
    raw = route["segments"][0]
    segment = Segment(raw["id"], raw["origin"], raw["destination"], raw["distance_km"],
                      raw["max_speed_kmh"], raw["min_speed_kmh"], raw["waiting_h"],
                      SourceRef.from_dict(raw["source"]), raw["assumptions"])
    response = run_tenergy([segment], [6], DEMO)
    assert response.status == Status.NEED_CLARIFICATION
    assert response.missing_fields == ["segments[0].max_speed_kmh", "segments[0].waiting_h"]
