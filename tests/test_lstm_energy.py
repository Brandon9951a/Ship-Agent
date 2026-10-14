import copy

import numpy as np

from tools.lstm_energy import (HISTORY, ConditionalLSTM, ModelBundle, _series,
                               batch_evaluate, build_samples, evaluate, fit_scalers)


def _point(i, power=10):
    return {"time":float(i*5),"speed_kmh":10.,"power_kw":float(power),"rows":[i+2]}


def test_duplicate_policy_is_order_independent_and_excludes_conflicts():
    def row(time,speed,power):
        result = [""]*66
        result[0],result[5],result[65] = time,str(power),str(speed)
        return result
    rows = [row("2025-01-01T00:00:00",5,10),row("2025-01-01T00:00:00",5.01,10.1),
            row("2025-01-01T00:00:05",5,10),row("2025-01-01T00:00:05",7,70)]
    points,audit = _series(rows,csv=True)
    assert len(points) == 1
    assert audit["duplicate_groups"] == 2
    assert audit["conflicting_groups"] == 1
    assert np.isclose(points[0]["speed_kmh"],5.005*1.852)
    assert len(points[0]["rows"]) == 2


def test_time_blocks_purge_and_train_only_scaler():
    points = [_point(i) for i in range(200)]
    points[197]["power_kw"] = 200
    samples,manifest = build_samples({"data.xlsx":points})
    assert manifest["purge_points"] == 17
    assert all(s["first_time"] <= 515 for s in samples["train"])
    assert all(s["first_time"] >= 885 for s in samples["test"])
    scaler = fit_scalers(samples)
    assert scaler["target"]["mean"] == 10
    assert any(np.max(s["target"]) == 200 for s in samples["test"])


def test_inference_contract_determinism_no_future_power_leakage():
    import torch
    torch.manual_seed(2)
    model = ConditionalLSTM()
    bundle = ModelBundle(model,{"history":{"mean":[0,0,0],"std":[1,1,1]},
                                "future":{"mean":[0,0],"std":[1,1]},
                                "target":{"mean":[0],"std":[1]}},
                         {"version":"test","extension":False,"speed_range_kmh":[1,20]})
    history = [{"speed_kmh":10.,"motor_power_kw":20.,"dt_seconds":5} for _ in range(HISTORY)]
    plan = [{"segment_id":"A","speed_kmh":10.,"dt_seconds":5,"waiting_seconds":0},
            {"segment_id":"A","speed_kmh":11.,"dt_seconds":5,"waiting_seconds":0}]
    a = evaluate(plan,None,history,"test",bundle=bundle)
    changed = copy.deepcopy(plan)
    changed[0]["motor_power_kw"] = 1e8
    b = evaluate(changed,None,history,"test",bundle=bundle)
    assert a == b
    assert a["status"] == "experimental"
    assert a["battery_energy_kwh"] is None
    assert sum(x["motor_energy_kwh"] for x in a["steps"]) == a["segments"][0]["motor_energy_kwh"]
    assert a["segments"][0]["moving_seconds"] == 10
    assert evaluate(plan,None,[],"test",bundle=bundle)["status"] == "missing_history"
    assert evaluate([dict(plan[0],waiting_seconds=None)],None,history,"test",bundle=bundle)["status"] == "waiting_time_unknown"
    assert evaluate([dict(plan[0],speed_kmh=21)],None,history,"test",bundle=bundle)["status"] == "outside_training_domain"
    assert evaluate(plan*3,None,history,"test",bundle=bundle)["status"] == "long_horizon_unvalidated"
    assert batch_evaluate([plan,plan],[None,None],[history,history],"test",bundle=bundle) == [a,a]
    assert evaluate(plan,None,history,"test",bundle=bundle,usage="planning")["status"] == "model_unapproved"
    assert all(result["status"] == "model_unapproved" for result in
               batch_evaluate([plan,plan],[None,None],[history,history],"test",
                              bundle=bundle,usage="planning"))


def test_excel_extension_requires_sourced_environment():
    model = ConditionalLSTM(future_features=6)
    fields = ("depth_m","current_mps","wind_mps","wind_angle_deg")
    bundle = ModelBundle(model,{"history":{"mean":[0]*7,"std":[1]*7},
                                "future":{"mean":[0]*6,"std":[1]*6},
                                "target":{"mean":0,"std":1}},
                         {"version":"six","extension":True,"speed_range_kmh":[1,20],
                          "environment_range":{f:[-5,50] for f in fields}})
    history = [dict({"speed_kmh":10,"motor_power_kw":20,"dt_seconds":5},
                    **{f:1 for f in fields}) for _ in range(HISTORY)]
    plan = [{"segment_id":"A","speed_kmh":10,"dt_seconds":5,"waiting_seconds":0}]
    assert evaluate(plan,None,history,"six",bundle=bundle)["status"] == "missing_environment"
    env = [dict({f:1 for f in fields},source="forecast-v1")]
    assert evaluate(plan,env,history,"six",bundle=bundle)["steps"][0]["environment_source"] == "forecast-v1"
    env[0]["depth_m"] = 100
    assert evaluate(plan,env,history,"six",bundle=bundle)["status"] == "outside_training_domain"
