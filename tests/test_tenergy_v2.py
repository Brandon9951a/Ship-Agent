import copy
import json
from pathlib import Path
import shutil

import pytest
import torch

from schemas.tenergy_v2 import EnergyBatchRequest
from scripts.example_tenergy_v2 import load_fixture
from tools.tenergy_v2 import JsonArtifactStore, TenergyV2


@pytest.fixture
def setup(tmp_path):
    torch.set_num_threads(2)
    store = JsonArtifactStore(tmp_path/"artifacts")
    request = load_fixture(store)
    predictor = TenergyV2("models/tenergy-lstm-v5",store)
    return predictor,store,request


def result(predictor,store,request):
    run = predictor.evaluate_batch(request)
    return run,store.get(run["payload_ref"]) if run["payload_ref"] else None


def test_different_lengths_partial_failures_and_integrals(setup):
    predictor,store,request = setup
    run,batch = result(predictor,store,EnergyBatchRequest.from_dict(request))
    expected = json.loads(Path("configs/examples/tenergy_v2/expected.json").read_text(encoding="utf-8"))
    assert run["execution_status"] == "completed"
    assert run["input_hash"] and run["config_hash"]
    assert run["parent_call_id"] == request["parent_call_id"]
    assert {c["candidate_id"]:c["outcome_code"] for c in batch["candidates"]} == expected["research"]
    for c in batch["candidates"]:
        assert c["selectable_for_planning"] is False
        assert c["battery_energy_kwh"] is None
        if c["power_series_ref"]:
            series = store.get(c["power_series_ref"],kind="power_series")["points"]
            energy = sum(p["predicted_motor_power_kw"]*p["duration_s"]/3600
                         for p in series if p["predicted_motor_power_kw"] is not None)
            assert c["covered_motor_energy_kwh"] == pytest.approx(energy,abs=1e-6)
            assert sum(s["covered_motor_energy_kwh"] for s in c["segment_summary"]) == pytest.approx(energy,abs=1e-6)
    waiting = next(c for c in batch["candidates"] if c["candidate_id"]=="with-wait")
    assert 0 < waiting["coverage_ratio"] < 1
    assert waiting["motor_energy_kwh"] is None
    assert waiting["waiting_energy_kwh"] is None


def test_order_single_batch_reload_and_no_target_leakage(setup):
    predictor,store,request = setup
    _,first = result(predictor,store,request)
    original = {c["candidate_id"]:c for c in first["candidates"]}
    request["candidates"].reverse()
    _,reversed_batch = result(predictor,store,request)
    assert {c["candidate_id"]:c for c in reversed_batch["candidates"]} == original
    for candidate in [c for c in request["candidates"] if c["candidate_id"] in ("short","longer")]:
        single = dict(request,candidates=[candidate])
        _,again = result(TenergyV2(predictor.model_directory,store),store,single)
        assert again["candidates"][0] == original[candidate["candidate_id"]]


def test_formal_mode_rejects_unapproved_model(setup):
    predictor,store,request = setup
    request["usage"] = "planning"
    request["candidates"] = request["candidates"][:2]
    run,batch = result(predictor,store,request)
    assert run["outcome_code"] == "model_unapproved"
    assert all(c["outcome_code"]=="model_unapproved" and not c["selectable_for_planning"]
               for c in batch["candidates"])
    assert predictor.bundle.manifest["planner_approved"] is False


def test_duplicate_ids_and_bad_time_are_not_silent(setup):
    predictor,store,request = setup
    duplicate = copy.deepcopy(request)
    duplicate["candidates"].append(duplicate["candidates"][0])
    assert predictor.evaluate_batch(duplicate)["outcome_code"] == "invalid_input"
    request["candidates"][0]["trajectory"][0]["start_offset_s"] = 5
    _,batch = result(predictor,store,request)
    assert batch["candidates"][0]["outcome_code"] == "invalid_input"
    assert batch["candidates"][1]["outcome_code"] == "ok"


def test_missing_history_snapshot_and_tampering(setup):
    predictor,store,request = setup
    absent = dict(request,history_ref=None)
    run,batch = result(predictor,store,absent)
    assert run["outcome_code"]=="needs_input" and len(batch["candidates"])==len(request["candidates"])
    mismatched = dict(request,snapshot_id="other")
    assert predictor.evaluate_batch(mismatched)["outcome_code"] == "invalid_input"
    path = store.directory/f"{request['history_ref']['sha256']}.json"
    path.write_text("{}",encoding="utf-8")
    run,_ = result(predictor,store,request)
    assert run["outcome_code"]=="tool_error" and run["execution_status"]=="failed"


def test_future_history_and_unknown_model(setup):
    predictor,store,request = setup
    payload = store.get(request["history_ref"])
    payload["points"][-1]["observed_offset_s"] = 5
    request["history_ref"] = store.put(payload,"history")
    assert predictor.evaluate_batch(request)["outcome_code"]=="invalid_input"
    missing = TenergyV2("models/model-not-present",store)
    assert missing.evaluate_batch(request)["outcome_code"]=="tool_error"


def test_corrupt_model_returns_repeatable_tool_error(setup,tmp_path):
    _,store,request = setup
    directory = tmp_path/"bad-model"
    directory.mkdir()
    for name in ("weights.pt","scaler.json","manifest.json","inference_domain.json"):
        shutil.copyfile(Path("models/tenergy-lstm-v5")/name,directory/name)
    (directory/"weights.pt").write_bytes(b"corrupted")
    predictor = TenergyV2(directory,store)
    for _ in range(2):
        run = predictor.evaluate_batch(request)
        assert run["outcome_code"]=="tool_error" and run["execution_status"]=="failed"
        assert predictor.bundle is None


def test_output_store_failure_is_a_tool_error(setup,monkeypatch):
    predictor,store,request = setup
    original = store.put
    def refuse_result(payload,kind):
        if kind=="energy_batch_result":
            raise OSError("simulated disk failure")
        return original(payload,kind)
    monkeypatch.setattr(store,"put",refuse_result)
    run = predictor.evaluate_batch(request)
    assert run["outcome_code"]=="tool_error" and run["payload_ref"] is None
    assert run["diagnostics"][0]["code"]=="output_artifact_failed"


def test_acceleration_and_unknown_waiting_remain_distinct(setup):
    predictor,store,request = setup
    request["candidates"] = request["candidates"][:1]
    request["candidates"][0]["trajectory"][0]["planned_speed_kmh"] = 1.0
    _,batch = result(predictor,store,request)
    assert batch["candidates"][0]["outcome_code"] == "model_out_of_domain"
    assert batch["candidates"][0]["diagnostics"][0]["field_ref"] == "acceleration_mps2"


def test_extension_missing_sourced_environment_and_wrong_scenario(setup):
    _,store,request = setup
    predictor = TenergyV2("models/tenergy-lstm-v5/excel_six",store)
    predictor._load()
    request["model_version"] = "excel_six"
    request["candidates"] = request["candidates"][:1]
    fields = {f:(b[0]+b[1])/2 for f,b in predictor.bundle.manifest["environment_range"].items()}
    hist = store.get(request["history_ref"])
    for point in hist["points"]:
        point.update(fields)
    request["history_ref"] = store.put(hist,"history")
    _,batch = result(predictor,store,request)
    assert batch["candidates"][0]["outcome_code"]=="needs_input"
    env = {"scenario_id":request["scenario_id"],"points":[dict(fields,start_offset_s=0,duration_s=5)]}
    request["environment_scenario_ref"] = store.put(env,"environment")
    _,batch = result(predictor,store,request)
    assert batch["candidates"][0]["diagnostics"][0]["code"]=="environment_source_missing"
    env["source_ref"] = {"source_id":"synthetic-environment","kind":"assumption"}
    request["environment_scenario_ref"] = store.put(env,"environment")
    _,batch = result(predictor,store,request)
    assert batch["candidates"][0]["outcome_code"]=="ok"
    env["scenario_id"] = "different-scenario"
    request["environment_scenario_ref"] = store.put(env,"environment")
    _,batch = result(predictor,store,request)
    assert batch["candidates"][0]["outcome_code"]=="invalid_input"
    env["scenario_id"] = request["scenario_id"]
    env["points"] = ["malformed"]
    request["environment_scenario_ref"] = store.put(env,"environment")
    _,batch = result(predictor,store,request)
    assert batch["candidates"][0]["diagnostics"][0]["code"]=="invalid_environment_points"


def test_future_power_field_and_nan_rejected(setup):
    predictor,store,request = setup
    request["candidates"][0]["trajectory"][0]["motor_power_kw"] = 100
    _,batch = result(predictor,store,request)
    assert batch["candidates"][0]["outcome_code"]=="invalid_input"
    request["candidates"][0]["trajectory"][0].pop("motor_power_kw")
    request["candidates"][0]["trajectory"][0]["planned_speed_kmh"] = float("nan")
    assert predictor.evaluate_batch(request)["outcome_code"]=="invalid_input"
