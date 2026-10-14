"""Experimental motor-side conditional LSTM; never implies battery/SOC approval."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import torch
from torch import nn

from scripts.inspect_data import csv_rows, xlsx_rows, xlsx_sheets

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "docs/团队资料/05_航行数据/历史航行数据"
EXPECTED_SHA256 = {
    "data.xlsx": "a62c5fde4796aa8e18322661ed19fe1fa3be473e1cc130d0a8c5b90d2206f5b7",
    "log4p-10-29(1).csv": "d1123094ee22ebb8098d3920fa0d45f803dd8b374f257d2032ff837d0e23b1f7",
    "log4p-10-31(1).csv": "2cf012d1ddb2c73acc957598cf40a126ffc60e8cb5f87197d1e99dcdca813fb5",
}
HISTORY, HORIZON, STEP_SECONDS = 12, 5, 5
WINDOW = HISTORY + HORIZON
FEATURES = ("speed_kmh", "acceleration_mps2", "depth_m", "current_mps", "wind_mps", "wind_angle_deg")


def _number(row, index):
    try:
        value = float(row[index])
        return value if math.isfinite(value) else None
    except (ValueError, IndexError, TypeError):
        return None


def _series(rows, *, csv=False):
    """Collapse duplicate timestamps before splitting; reject conflicting groups."""
    groups = defaultdict(list)
    audit = defaultdict(int)
    for record, row in enumerate(rows, 2):
        try:
            # Naive CSV clocks have unknown timezone; UTC here is only a stable
            # arithmetic origin, not a claim about the recorded local timezone.
            stamp = datetime.fromisoformat(row[0]).replace(tzinfo=timezone.utc).timestamp() if csv else float(row[0])
        except (ValueError, IndexError, OverflowError):
            audit["invalid_time"] += 1
            continue
        speed = _number(row, 65 if csv else 54)
        power = _number(row, 5 if csv else 2)
        if speed is None or power is None:
            audit["missing_speed_or_power"] += 1
            continue
        env = [] if csv else [_number(row, i) for i in (56, 59, 58, 57)]
        groups[stamp].append((speed * 1.852, power, env, record))
    points = []
    for stamp, group in sorted(groups.items()):
        speeds = [x[0] for x in group]
        powers = [x[1] for x in group]
        if len(group) > 1:
            audit["duplicate_groups"] += 1
        speed_spread = max(speeds) - min(speeds)
        power_spread = max(powers) - min(powers)
        if speed_spread > 0.1852 or power_spread > max(5.0, .05 * max(abs(p) for p in powers)):
            audit["conflicting_groups"] += 1
            continue
        # Median is stable under row ordering; provenance includes all original record numbers.
        point = {"time": stamp, "speed_kmh": float(np.median(speeds)),
                 "power_kw": float(np.median(powers)), "rows": [x[3] for x in group],
                 "duplicate_spread": {"speed_kmh": speed_spread, "power_kw": power_spread}}
        if not csv:
            point.update(dict(zip(FEATURES[2:], group[0][2])))
        points.append(point)
    audit["unique_times_after_policy"] = len(points)
    audit["zero_speed_points"] = sum(p["speed_kmh"] == 0 for p in points)
    audit["negative_power_points"] = sum(p["power_kw"] < 0 for p in points)
    audit["zero_power_points"] = sum(p["power_kw"] == 0 for p in points)
    audit["non_five_second_intervals"] = sum(not 4 <= b["time"]-a["time"] <= 6
                                           for a,b in zip(points,points[1:]))
    return points, dict(audit)


def load_sources(data_dir=DATA):
    sources, audit = {}, {}
    for name, digest in EXPECTED_SHA256.items():
        path = Path(data_dir) / name
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f"Source hash changed: {name}")
        if name.endswith(".csv"):
            _, rows, stream = csv_rows(path)
            try:
                next(rows)
                sources[name], audit[name] = _series(rows, csv=True)
            finally:
                stream.close()
        else:
            with ZipFile(path) as archive:
                sheets, strings, _ = xlsx_sheets(archive)
                if len(sheets) != 1:
                    raise ValueError("Expected exactly one Excel sheet")
                rows = xlsx_rows(archive, sheets[0][1], strings)
                next(rows)
                sources[name], audit[name] = _series(rows)
    return sources, audit


def build_samples(sources, *, extension=False, source_holdout=False):
    """Split source/time blocks first, then create windows wholly inside each block."""
    samples = {part: [] for part in ("train", "val", "test")}
    manifest = {"history": HISTORY, "horizon": HORIZON, "purge_points": WINDOW,
                "extension": extension, "source_holdout": source_holdout, "blocks": []}
    rejected = defaultdict(int)
    for name, points in sources.items():
        if extension and not name.endswith(".xlsx"):
            continue
        n = len(points)
        if source_holdout and name.startswith("log4p-10-31"):
            spans = [("test", 0, n)]
        elif source_holdout:
            spans = [("train", 0, int(.8*n)), ("val", int(.8*n)+WINDOW, n)]
        else:
            spans = [("train", 0, int(.6*n)), ("val", int(.6*n)+WINDOW, int(.8*n)),
                     ("test", int(.8*n)+WINDOW, n)]
        for part, start, end in spans:
            manifest["blocks"].append({"source": name, "split": part, "start": start, "end": end,
                                       "first_time": points[start]["time"] if start < n else None,
                                       "last_time": points[end-1]["time"] if end > start else None})
            for i in range(start, end-WINDOW+1):
                window = points[i:i+WINDOW]
                times = [p["time"] for p in window]
                if any(not 4.0 <= b-a <= 6.0 for a,b in zip(times,times[1:])):
                    rejected["timing_discontinuity"] += 1
                    continue
                if any(p["speed_kmh"] <= 0 or p["power_kw"] <= 0 or
                       p["speed_kmh"] > 30 or p["power_kw"] > 300 for p in window):
                    rejected["nonmoving_negative_or_outlier"] += 1
                    continue
                if extension and any(any(p.get(f) is None for f in FEATURES[2:]) for p in window):
                    rejected["missing_environment"] += 1
                    continue
                speeds = np.array([p["speed_kmh"] for p in window], dtype=np.float32)
                acc = np.diff(speeds / 3.6, prepend=speeds[0]/3.6) / np.array(
                    [STEP_SECONDS] + [b-a for a,b in zip(times,times[1:])], dtype=np.float32)
                x = np.stack((speeds, acc), axis=1)
                if extension:
                    x = np.concatenate((x, np.array([[p[f] for f in FEATURES[2:]] for p in window],
                                                     dtype=np.float32)), axis=1)
                power = np.array([p["power_kw"] for p in window], dtype=np.float32)
                samples[part].append({"history": np.concatenate((x[:HISTORY], power[:HISTORY,None]), axis=1),
                                      "future": x[HISTORY:], "target": power[HISTORY:],
                                      "source": name, "start_index": i,
                                      "first_row": window[0]["rows"][0],
                                      "last_row": window[-1]["rows"][-1],
                                      "first_time": times[0], "target_times": times[HISTORY:],
                                      "target_dts":np.diff(np.asarray(times[HISTORY-1:],dtype=np.float64))})
    manifest["sample_counts"] = {part: len(items) for part,items in samples.items()}
    manifest["sample_counts_by_source"] = {
        part:{name:sum(s["source"] == name for s in items) for name in sources}
        for part,items in samples.items()}
    manifest["rejected_windows"] = dict(rejected)
    if not samples["train"] or not samples["val"] or not samples["test"]:
        raise ValueError("Insufficient valid windows for all splits")
    return samples, manifest


def fit_scalers(samples):
    history = np.concatenate([s["history"] for s in samples["train"]])
    future = np.concatenate([s["future"] for s in samples["train"]])
    targets = np.concatenate([s["target"] for s in samples["train"]])
    def stats(x):
        return {"mean": np.mean(x, axis=0).tolist(), "std": np.maximum(np.std(x, axis=0), 1e-6).tolist()}
    return {"history": stats(history), "future": stats(future), "target": stats(targets)}


def arrays(items, scaler):
    def norm(x, key):
        s = scaler[key]
        return (x-np.asarray(s["mean"], dtype=np.float32))/np.asarray(s["std"], dtype=np.float32)
    return (np.stack([norm(s["history"], "history") for s in items]).astype("float32"),
            np.stack([norm(s["future"], "future") for s in items]).astype("float32"),
            np.stack([norm(s["target"], "target") for s in items]).astype("float32"))


class ConditionalLSTM(nn.Module):
    def __init__(self, future_features=2, hidden=32):
        super().__init__()
        self.encoder = nn.LSTM(future_features+1, hidden, batch_first=True)
        self.decoder = nn.LSTM(future_features, hidden, batch_first=True)
        self.head = nn.Linear(hidden, 1)

    def forward(self, history, future):
        _, state = self.encoder(history)
        out, _ = self.decoder(future, state)
        return self.head(out).squeeze(-1)


@dataclass
class ModelBundle:
    model: ConditionalLSTM
    scaler: dict
    manifest: dict


def load_model(directory, *, version=None):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if version is not None and version != manifest["version"]:
        raise ValueError("Unknown model version")
    scaler = json.loads((directory / "scaler.json").read_text(encoding="utf-8"))
    model = ConditionalLSTM(manifest["future_features"], manifest["hidden"])
    model.load_state_dict(torch.load(directory / "weights.pt", map_location="cpu", weights_only=True))
    model.eval()
    return ModelBundle(model, scaler, manifest)


def write_inference_domain(directory, train_samples):
    """Save descriptive training ranges tied to the exact frozen model files."""
    directory = Path(directory)
    future = np.concatenate([s["future"] for s in train_samples])
    history = np.concatenate([s["history"] for s in train_samples])
    domain = {"version":"v5-training-domain/1","scope":"Observed training ranges only; not a planner approval",
              "model_files_sha256":{name:hashlib.sha256((directory/name).read_bytes()).hexdigest()
                                    for name in ("weights.pt","scaler.json","manifest.json")},
              "future_feature_ranges":{FEATURES[i]:[float(future[:,i].min()),float(future[:,i].max())]
                                       for i in range(future.shape[-1])},
              "history_power_range_kw":[float(history[:,-1].min()),float(history[:,-1].max())]}
    (directory/"inference_domain.json").write_text(json.dumps(domain,indent=2,allow_nan=False)+"\n",encoding="utf-8")


def evaluate(speed_plan, environment_scenario, history, model_version, *, bundle, usage="research"):
    """Experimental five-step forecast. Planning use requires an approved manifest."""
    if usage not in ("research", "planning"):
        return {"status": "invalid_usage"}
    if model_version != bundle.manifest["version"]:
        return {"status": "invalid_model_version"}
    if not history or len(history) < HISTORY or any("motor_power_kw" not in p for p in history[-HISTORY:]):
        return {"status": "missing_history"}
    if not speed_plan:
        return {"status": "invalid_plan"}
    if any(p.get("waiting_seconds") is None for p in speed_plan):
        return {"status": "waiting_time_unknown"}
    if any(not isinstance(p.get("segment_id"),str) or not p["segment_id"] or
           not isinstance(p.get("waiting_seconds"),(int,float)) or
           not math.isfinite(p["waiting_seconds"]) or p["waiting_seconds"] < 0
           for p in speed_plan):
        return {"status":"invalid_plan"}
    extension = bundle.manifest["extension"]
    if extension and (not environment_scenario or len(environment_scenario) != len(speed_plan) or
                      any(any(f not in p for f in FEATURES[2:]) or not p.get("source")
                          for p in environment_scenario)):
        return {"status": "missing_environment"}
    if usage == "planning" and not (bundle.manifest.get("planner_approved") is True and
                                     bundle.manifest.get("approval_ref")):
        return {"status": "model_unapproved", "model_version": model_version}
    if len(speed_plan) > HORIZON:
        return {"status": "long_horizon_unvalidated"}
    bounds = bundle.manifest["speed_range_kmh"]
    if any(not isinstance(p.get("speed_kmh"),(int,float)) or
           not isinstance(p.get("dt_seconds"),(int,float)) or
           not math.isfinite(p["speed_kmh"]) or not math.isfinite(p["dt_seconds"]) or
           not bounds[0] <= p["speed_kmh"] <= bounds[1] or
           not 4 <= p["dt_seconds"] <= 6 for p in speed_plan):
        return {"status": "outside_training_domain"}
    if extension:
        for env in environment_scenario:
            for field,bound in bundle.manifest["environment_range"].items():
                value = env[field]
                if not isinstance(value,(int,float)) or not math.isfinite(value) or not bound[0] <= value <= bound[1]:
                    return {"status":"outside_training_domain"}
    hist = history[-HISTORY:]
    if any(not isinstance(p.get("motor_power_kw"),(int,float)) or
           not isinstance(p.get("speed_kmh"),(int,float)) or
           not isinstance(p.get("dt_seconds",STEP_SECONDS),(int,float)) or
           not math.isfinite(p["motor_power_kw"]) or
           not math.isfinite(p["speed_kmh"]) or
           not 4 <= p.get("dt_seconds",STEP_SECONDS) <= 6 or
           p["motor_power_kw"] <= 0 or p["speed_kmh"] <= 0 for p in hist):
        return {"status": "invalid_history"}
    def feature(speed, acc, env=None):
        values = [speed, acc]
        if extension:
            values.extend(env[f] for f in FEATURES[2:])
        return values
    hfeatures = []
    for i,p in enumerate(hist):
        previous = hist[max(0,i-1)]
        dt = p.get("dt_seconds", STEP_SECONDS)
        acc = (p["speed_kmh"]-previous["speed_kmh"])/3.6/dt
        if extension and any(f not in p for f in FEATURES[2:]):
            return {"status": "missing_environment"}
        hfeatures.append(feature(p["speed_kmh"], acc, p)+[p["motor_power_kw"]])
    future, previous_speed = [], hist[-1]["speed_kmh"]
    for i,p in enumerate(speed_plan):
        acc = (p["speed_kmh"]-previous_speed)/3.6/p["dt_seconds"]
        future.append(feature(p["speed_kmh"], acc, environment_scenario[i] if extension else None))
        previous_speed = p["speed_kmh"]
    # Decoder supports shorter plans; no future observed power enters this path.
    hs, fs = bundle.scaler["history"], bundle.scaler["future"]
    h = ((np.asarray(hfeatures,dtype=np.float32)-hs["mean"])/hs["std"]).astype(np.float32)
    f = ((np.asarray(future,dtype=np.float32)-fs["mean"])/fs["std"]).astype(np.float32)
    with torch.inference_mode():
        scaled = bundle.model(torch.from_numpy(h[None]),torch.from_numpy(f[None]))[0].numpy()
    predicted = np.maximum(0, scaled*bundle.scaler["target"]["std"]+bundle.scaler["target"]["mean"])
    steps, segments = [], {}
    for p,power in zip(speed_plan,predicted):
        segment = p["segment_id"]
        energy = float(power*p["dt_seconds"]/3600)
        steps.append({"time": p.get("time"), "segment_id": segment, "speed_kmh": p["speed_kmh"],
                      "motor_power_kw": float(power), "motor_energy_kwh": energy,
                      "quality": "provisional_csv_units" if not extension else "excel_only",
                      "environment_source": environment_scenario[len(steps)].get("source") if extension else None,
                      "model_version": model_version})
        entry = segments.setdefault(segment,{"segment_id":segment,"moving_seconds":0.0,
                                            "waiting_seconds":0.0,"motor_energy_kwh":0.0,
                                            "peak_motor_power_kw":0.0,"integration_coverage":1.0})
        entry["moving_seconds"] += p["dt_seconds"]
        entry["waiting_seconds"] += p["waiting_seconds"]
        entry["motor_energy_kwh"] += energy
        entry["peak_motor_power_kw"] = max(entry["peak_motor_power_kw"],float(power))
    return {"status":"experimental" if usage == "research" else "planning_prediction",
            "measurement_boundary":"propulsion_motor_observation",
            "battery_energy_kwh":None,"waiting_energy_kwh":None,"auxiliary_energy_kwh":None,
            "integration_rule":"right_endpoint", "model_version":model_version,
            "steps":steps,"segments":list(segments.values())}


def batch_evaluate(plans, environment_scenarios, histories, model_version, *, bundle, usage="research"):
    if not (len(plans) == len(environment_scenarios) == len(histories)):
        raise ValueError("Batch lengths differ")
    return [evaluate(p,e,h,model_version,bundle=bundle,usage=usage)
            for p,e,h in zip(plans,environment_scenarios,histories)]
