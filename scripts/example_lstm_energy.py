"""Run a five-step experimental motor-side forecast from a held-out window.

python -m scripts.example_lstm_energy --model-dir models/tenergy-lstm-v5
This prints only model outputs and statuses, never original data rows.
"""

import argparse
import json
from pathlib import Path

from tools.lstm_energy import STEP_SECONDS, build_samples, evaluate, load_model, load_sources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir",type=Path,default=Path("models/tenergy-lstm-v5"))
    args = parser.parse_args()
    bundle = load_model(args.model_dir)
    samples,_ = build_samples(load_sources()[0])
    sample = samples["test"][0]
    history = [{"speed_kmh":float(h[0]),"motor_power_kw":float(h[-1]),
                "dt_seconds":STEP_SECONDS} for h in sample["history"]]
    plan = [{"segment_id":"example","speed_kmh":float(f[0]),"dt_seconds":STEP_SECONDS,
             "waiting_seconds":0,"time":float(t)}
            for f,t in zip(sample["future"],sample["target_times"])]
    result = evaluate(plan,None,history,bundle.manifest["version"],bundle=bundle)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
