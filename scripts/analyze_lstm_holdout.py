"""Reevaluate frozen v5 weights on unchanged splits; never fits or tunes a model."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

import numpy as np
import torch

from scripts.train_lstm_energy import predict, summarize
from tools.lstm_energy import build_samples, load_model, load_sources, HORIZON, FEATURES, write_inference_domain


def distributions(items):
    future = np.concatenate([s["future"] for s in items])
    power = np.concatenate([s["target"] for s in items])
    return {"windows":len(items),"by_source":dict(Counter(s["source"] for s in items)),
            "future_feature_quantiles":{FEATURES[i]:np.quantile(future[:,i],[0,.1,.5,.9,1]).tolist()
                                        for i in range(future.shape[-1])},
            "speed_kmh_quantiles":np.quantile(future[:,0],[0,.1,.5,.9,1]).tolist(),
            "acceleration_mps2_quantiles":np.quantile(future[:,1],[0,.1,.5,.9,1]).tolist(),
            "power_kw_quantiles":np.quantile(power,[0,.1,.5,.9,1]).tolist()}


def support(train,test):
    x = np.concatenate([s["future"] for s in train])
    y = np.concatenate([s["future"] for s in test])
    # A descriptive grid fixed for this audit, not a new admission threshold.
    edges = ([0,2,4,6,8,10,12,30],[-1e6,-.05,-.01,0,.01,.05,1e6])
    cells = lambda z: list(zip(np.digitize(z[:,0],edges[0]),np.digitize(z[:,1],edges[1])))
    occupied = set(cells(x))
    return {"speed_acceleration_grid_edges":edges,
            "test_fraction_inside_train_range_by_feature":{FEATURES[i]:float(np.mean(
                (y[:,i]>=x[:,i].min())&(y[:,i]<=x[:,i].max()))) for i in range(x.shape[-1])},
            "test_steps":len(y),"train_occupied_cells":len(occupied),
            "test_fraction_in_train_occupied_cell":sum(c in occupied for c in cells(y))/len(y),
            "test_fraction_inside_train_speed_range":float(np.mean((y[:,0]>=x[:,0].min())&(y[:,0]<=x[:,0].max()))),
            "test_fraction_inside_train_acceleration_range":float(np.mean((y[:,1]>=x[:,1].min())&(y[:,1]<=x[:,1].max()))),
            "meaning":"Marginal/grid coverage of observed windows; not proof of joint causal or counterfactual validity"}


def rolling_comparison(bundle,items,cubic):
    lookup = {(s["source"],s["start_index"]):s for s in items}
    pairs = [(a,lookup[(a["source"],a["start_index"]+HORIZON)]) for a in items
             if a["start_index"]%10==0 and (a["source"],a["start_index"]+HORIZON) in lookup]
    if not pairs:
        return {"windows":0}
    first = [a for a,_ in pairs]
    p1 = predict(bundle.model,first,bundle.scaler)
    second = [dict(b,history=np.concatenate((a["history"][HORIZON:],
              np.concatenate((a["future"],p[:,None]),axis=1)))) for (a,b),p in zip(pairs,p1)]
    pred = {"lstm":np.concatenate((p1,predict(bundle.model,second,bundle.scaler)),axis=1),
            "persistence":np.stack([np.repeat(a["history"][-1,-1],10) for a,b in pairs]),
            "cubic":np.stack([np.maximum(0,cubic[0]+cubic[1]*np.concatenate((a["future"][:,0],b["future"][:,0]))**3)
                               for a,b in pairs])}
    truth = np.stack([np.concatenate((a["target"],b["target"])) for a,b in pairs])
    dts = np.stack([np.concatenate((a["target_dts"],b["target_dts"])) for a,b in pairs])
    metrics = {}
    for name,p in pred.items():
        error = p-truth
        metrics[name] = {"mae_kw":float(np.abs(error).mean()),"rmse_kw":float(np.sqrt((error**2).mean())),
                         "first_five_mae_kw":float(np.abs(error[:,:5]).mean()),
                         "second_five_mae_kw":float(np.abs(error[:,5:]).mean()),
                         "per_horizon_mae_kw":np.abs(error).mean(axis=0).tolist(),
                         "energy_mae_kwh":float(np.abs((error*dts).sum(axis=1)/3600).mean())}
    return {"windows":len(pairs),"by_source":dict(Counter(a["source"] for a,b in pairs)),"metrics":metrics,
            "method":"Same 10-step windows for every model; second LSTM block uses predicted history power"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir",type=Path,default=Path("models/tenergy-lstm-v5"))
    parser.add_argument("--output",type=Path,default=Path("models/tenergy-lstm-v5/improvement/holdout_analysis.json"))
    args = parser.parse_args()
    torch.set_num_threads(4)
    sources,_ = load_sources()
    original = json.loads((args.model_dir/"acceptance.json").read_text(encoding="utf-8"))
    experiments = {}
    for key,subdir in (("common_time",""),("common_source","common_source"),("excel_six","excel_six")):
        directory = args.model_dir/subdir
        bundle = load_model(directory)
        manifest = bundle.manifest
        samples,split = build_samples(sources,extension=manifest["extension"],source_holdout=manifest["source_holdout"])
        if split != manifest["split"]:
            raise ValueError("Frozen split changed; analysis refused")
        write_inference_domain(directory,samples["train"])
        cubic = manifest["cubic_coefficients"]
        metrics = {}
        for part in ("val","test"):
            items = samples[part]
            predictions = {"lstm":predict(bundle.model,items,bundle.scaler),
                "persistence":np.stack([np.repeat(s["history"][-1,-1],5) for s in items]),
                "cubic":np.stack([np.maximum(0,cubic[0]+cubic[1]*s["future"][:,0]**3) for s in items])}
            metrics[part] = {name:summarize(items,p) for name,p in predictions.items()}
            for name,summary in metrics[part].items():
                if abs(summary["mae_kw"]-original["experiments"][key]["metrics"][part][name]["mae_kw"]) > 1e-6:
                    raise ValueError("Original MAE did not reproduce")
        test = metrics["test"]
        delta = {base:{"mae_kw_lstm_minus_baseline":test["lstm"]["mae_kw"]-test[base]["mae_kw"],
                       "relative_mae_improvement_percent":100*(1-test["lstm"]["mae_kw"]/test[base]["mae_kw"])}
                 for base in ("persistence","cubic")}
        experiments[key] = {"file_sha256":{name:hashlib.sha256((directory/name).read_bytes()).hexdigest()
                                             for name in ("manifest.json","scaler.json","weights.pt")},
                            "metrics":metrics,"delta_vs_baseline":delta,
                            "split_distributions":{part:distributions(items) for part,items in samples.items()},
                            "test_support":support(samples["train"],samples["test"]),
                            "rolling_ten_step":rolling_comparison(bundle,samples["test"],cubic)}
    report = {"method":"Frozen v5 weights/scalers/splits; no retraining or threshold tuning",
              "seed_count":1,"independent_voyage_confidence_intervals":None,
              "limitations":["Overlapping windows are correlated, not independent voyages",
                             "CSV engineering units provisional; no motor-to-bus conversion verified",
                             "Recorded future speed is observed-condition evaluation, not counterfactual validation"],
              "planner_approved":False,"experiments":experiments}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print(json.dumps({k:v["delta_vs_baseline"] for k,v in experiments.items()}))


if __name__ == "__main__":
    main()
