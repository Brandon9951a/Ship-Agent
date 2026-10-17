"""One-command reproducible experimental Tenergy training and acceptance.

Run: python -m scripts.train_lstm_energy --output-dir models/tenergy-lstm-v1
The output directory must be new. Raw source rows are never copied to it.
"""

import argparse
import copy
import json
import random
import time
from pathlib import Path

import numpy as np
import torch
from torch import nn

from tools.lstm_energy import (DATA, EXPECTED_SHA256, HORIZON, STEP_SECONDS, FEATURES,
                               ConditionalLSTM, ModelBundle, arrays, batch_evaluate,
                               build_samples, fit_scalers, load_sources, write_inference_domain)


def predict(model, items, scaler, *, batch_size=1024):
    h, f, _ = arrays(items, scaler)
    out = []
    model.eval()
    with torch.inference_mode():
        for i in range(0, len(items), batch_size):
            out.append(model(torch.from_numpy(h[i:i+batch_size]),
                             torch.from_numpy(f[i:i+batch_size])).numpy())
    raw = np.concatenate(out)
    return np.maximum(0, raw*scaler["target"]["std"]+scaler["target"]["mean"])


def fit_model(samples, scaler, *, seed=20261014, epochs=30, patience=5, hidden=32):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.set_num_threads(min(torch.get_num_threads(), 4))
    h, f, y = arrays(samples["train"], scaler)
    vh, vf, vy = arrays(samples["val"], scaler)
    model = ConditionalLSTM(f.shape[-1], hidden)
    optimizer = torch.optim.Adam(model.parameters(), lr=.002)
    loss_fn = nn.L1Loss()
    best, wait, best_state, history = float("inf"), 0, None, []
    for epoch in range(epochs):
        model.train()
        order = np.random.permutation(len(y))
        for indices in np.array_split(order, max(1,(len(order)+255)//256)):
            optimizer.zero_grad()
            loss = loss_fn(model(torch.from_numpy(h[indices]),
                                 torch.from_numpy(f[indices])),torch.from_numpy(y[indices]))
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
        model.eval()
        with torch.inference_mode():
            val = np.mean([loss_fn(model(torch.from_numpy(vh[i:i+1024]),
                                        torch.from_numpy(vf[i:i+1024])),
                                   torch.from_numpy(vy[i:i+1024])).item()
                           for i in range(0,len(vy),1024)])
        history.append(float(val))
        print(f"epoch={epoch+1} val_scaled_mae={val:.5f}", flush=True)
        if val < best-1e-4:
            best, wait, best_state = val, 0, copy.deepcopy(model.state_dict())
        else:
            wait += 1
            if wait >= patience:
                break
    model.load_state_dict(best_state)
    return model, {"epochs_run":len(history),"best_epoch":int(np.argmin(history))+1,
                   "validation_scaled_mae":history,"early_stop_patience":patience}


def fit_cubic(train):
    speed = np.concatenate([s["future"][:,0] for s in train])
    target = np.concatenate([s["target"] for s in train])
    coef = np.linalg.lstsq(np.stack((np.ones_like(speed),speed**3),axis=1),target,rcond=None)[0]
    return coef.tolist()


def summarize(items, predictions):
    truth = np.stack([s["target"] for s in items])
    dts = np.stack([s["target_dts"] for s in items])
    speed = np.stack([s["future"][:,0] for s in items])
    error = predictions-truth
    measure = lambda a: {"mae_kw":float(np.mean(np.abs(a))),"rmse_kw":float(np.sqrt(np.mean(a*a)))}
    bins = {}
    for lo,hi in ((0,4),(4,8),(8,12),(12,20),(20,100)):
        mask = (speed>=lo)&(speed<hi)
        if np.any(mask):
            bins[f"{lo}-{hi} km/h"] = {"steps":int(mask.sum()), **measure(error[mask])}
    by_source = {}
    for source in sorted({s["source"] for s in items}):
        mask = np.array([s["source"]==source for s in items])
        by_source[source] = {"windows":int(mask.sum()),**measure(error[mask])}
    # Each forecast window is five measured 5-second steps. Windows overlap, so
    # these are window-level errors rather than independent voyage energy errors.
    energy_error = np.sum(error*dts,axis=1)/3600
    return {"windows":len(items),"steps":int(error.size),**measure(error),
            "per_horizon":[measure(error[:,i]) for i in range(HORIZON)],
            "speed_bins":bins,"by_source":by_source,
            "peak_mae_kw":float(np.mean(np.abs(np.max(predictions,axis=1)-np.max(truth,axis=1)))),
            "window_energy_mae_kwh":float(np.mean(np.abs(energy_error))),
            "window_energy_bias_kwh":float(np.mean(energy_error))}


def rolling_ten_step(model, items, scaler):
    """Open-loop second block uses predicted power, never observed future power."""
    lookup = {(s["source"],s["start_index"]):s for s in items}
    pairs = [(s,lookup[(s["source"],s["start_index"]+HORIZON)])
             for s in items if s["start_index"] % (2*HORIZON) == 0 and
             (s["source"],s["start_index"]+HORIZON) in lookup]
    if not pairs:
        return {"windows":0}
    first = [a for a,_ in pairs]
    p1 = predict(model,first,scaler)
    second = []
    for (a,b),pred in zip(pairs,p1):
        hist = np.concatenate((a["history"][HORIZON:],
                               np.concatenate((a["future"],pred[:,None]),axis=1)))
        second.append(dict(b,history=hist))
    p2 = predict(model,second,scaler)
    truth = np.stack([np.concatenate((a["target"],b["target"])) for a,b in pairs])
    dts = np.stack([np.concatenate((a["target_dts"],b["target_dts"])) for a,b in pairs])
    prediction = np.concatenate((p1,p2),axis=1)
    error = prediction-truth
    return {"windows":len(pairs),"mae_kw":float(np.mean(np.abs(error))),
            "first_five_mae_kw":float(np.mean(np.abs(error[:,:HORIZON]))),
            "second_five_mae_kw":float(np.mean(np.abs(error[:,HORIZON:]))),
            "ten_step_energy_mae_kwh":float(np.mean(np.abs(np.sum(error*dts,axis=1)/3600)))}


def experiment(sources, *, extension, source_holdout, seed, epochs):
    samples, split = build_samples(sources, extension=extension, source_holdout=source_holdout)
    scaler = fit_scalers(samples)
    model, train_log = fit_model(samples,scaler,seed=seed,epochs=epochs)
    cubic = fit_cubic(samples["train"])
    outcomes = {}
    for part in ("val","test"):
        items = samples[part]
        predictions = {
            "lstm": predict(model,items,scaler),
            "persistence":np.stack([np.repeat(s["history"][-1,-1],HORIZON) for s in items]),
            "cubic":np.stack([np.maximum(0,cubic[0]+cubic[1]*s["future"][:,0]**3) for s in items]),
        }
        outcomes[part] = {name:summarize(items,pred) for name,pred in predictions.items()}
        outcomes[part]["lstm"]["rolling_ten_step"] = rolling_ten_step(model,items,scaler)
    speeds = np.concatenate([s["future"][:,0] for s in samples["train"]])
    manifest = {"split":split,"speed_range_kmh":[float(np.min(speeds)),float(np.max(speeds))],
                "future_features":samples["train"][0]["future"].shape[-1],
                "extension":extension,"source_holdout":source_holdout,
                "hidden":32,"seed":seed,"training":train_log,"cubic_coefficients":cubic}
    if extension:
        future = np.concatenate([s["future"] for s in samples["train"]])
        manifest["environment_range"] = {
            name:[float(np.min(future[:,i])),float(np.max(future[:,i]))]
            for i,name in enumerate(FEATURES[2:],2)}
    return model,scaler,manifest,outcomes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir",type=Path,default=Path("models/tenergy-lstm-v1"))
    parser.add_argument("--data-dir",type=Path,default=DATA)
    parser.add_argument("--epochs",type=int,default=30)
    parser.add_argument("--seed",type=int,default=20261014)
    args = parser.parse_args(argv)
    if args.output_dir.exists():
        parser.error("Output directory already exists; choose a new version directory")
    if not 1 <= args.epochs <= 400:
        parser.error("epochs must be between 1 and 400")
    sources,audit = load_sources(args.data_dir)
    all_results = {}
    trained = {}
    for key,extension,source_holdout in (("common_time",False,False),
                                         ("common_source",False,True),
                                         ("excel_six",True,False)):
        print(f"training {key}",flush=True)
        model,scaler,manifest,metrics = experiment(sources,extension=extension,
                                                    source_holdout=source_holdout,
                                                    seed=args.seed,epochs=args.epochs)
        all_results[key] = {"manifest":manifest,"metrics":metrics}
        trained[key] = (model,scaler,manifest)
    model,scaler,manifest = trained["common_time"]
    version = args.output_dir.name
    manifest.update({"version":version,"source_sha256":EXPECTED_SHA256,
                     "dependency_versions":{"python":__import__("sys").version.split()[0],
                                            "numpy":np.__version__,"torch":torch.__version__},
                     "label":"propulsion_motor_observed_kw",
                     "status":"experimental_unapproved",
                     "step_seconds":STEP_SECONDS,"horizon":HORIZON,
                     "csv_units":"provisional per LSTM task book; field dictionary unconfirmed",
                     "integration":"right_endpoint_kW_seconds_over_3600"})
    time_test = all_results["common_time"]["metrics"]["test"]
    source_test = all_results["common_source"]["metrics"]["test"]
    accepted = all(time_test["lstm"]["mae_kw"] < time_test[b]["mae_kw"] and
                   source_test["lstm"]["mae_kw"] < source_test[b]["mae_kw"]
                   for b in ("persistence","cubic"))
    manifest["planner_approved"] = accepted and False  # requires measurement and interface approval
    benchmark_sample = build_samples(sources)[0]["train"][0]
    history = [{"speed_kmh":float(h[0]),"motor_power_kw":float(h[-1]),
                "dt_seconds":STEP_SECONDS} for h in benchmark_sample["history"]]
    plan = [{"segment_id":"benchmark","speed_kmh":float(f[0]),"dt_seconds":STEP_SECONDS,
             "waiting_seconds":0} for f in benchmark_sample["future"]]
    bundle = ModelBundle(model,scaler,manifest)
    t0 = time.perf_counter()
    benchmark_results = batch_evaluate([plan]*100,[None]*100,[history]*100,version,bundle=bundle)
    batch_seconds = time.perf_counter()-t0
    if any(result["status"] != "experimental" for result in benchmark_results):
        raise ValueError("Batch benchmark input rejected")
    report = {"data_audit":audit,"experiments":all_results,
              "batch_inference":{"plans":100,"seconds":batch_seconds,
                                 "hardware":"local CPU", "method":"sequential API batch"},
              "prediction_gate_passed":accepted,
              "planner_status":"requires_measurement_and_interface_approval" if accepted
                               else "rejected_by_holdout_baseline_comparison",
              "limitations":["CSV units provisional from task book; D1 dictionary says unconfirmed",
                             "Motor electrical measurement boundary and battery conversion unverified",
                             "Overlapping window energy errors are not independent voyage errors",
                             "Forecasts beyond five steps are refused until rolling accumulation is validated"]}
    args.output_dir.mkdir(parents=True)
    for key,(fitted,scale,info) in trained.items():
        directory = args.output_dir if key == "common_time" else args.output_dir/key
        directory.mkdir(exist_ok=True)
        info.update({"version":directory.name,"source_sha256":EXPECTED_SHA256,
                     "dependency_versions":manifest["dependency_versions"],
                     "label":"propulsion_motor_observed_kw",
                     "status":"experimental_unapproved", "step_seconds":STEP_SECONDS,
                     "horizon":HORIZON,"csv_units":manifest["csv_units"],
                     "integration":manifest["integration"],"planner_approved":False})
        torch.save(fitted.state_dict(),directory/"weights.pt")
        for name,obj in (("manifest.json",info),("scaler.json",scale)):
            (directory/name).write_text(json.dumps(obj,ensure_ascii=False,indent=2,allow_nan=False)+"\n",
                                        encoding="utf-8")
        training_samples = build_samples(sources,extension=info["extension"],source_holdout=info["source_holdout"])[0]["train"]
        write_inference_domain(directory,training_samples)
    (args.output_dir/"acceptance.json").write_text(
        json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print(json.dumps({"output":str(args.output_dir),"prediction_gate_passed":accepted,
                      "time_mae_kw":time_test["lstm"]["mae_kw"],
                      "source_mae_kw":source_test["lstm"]["mae_kw"]}),flush=True)


if __name__ == "__main__":
    main()
