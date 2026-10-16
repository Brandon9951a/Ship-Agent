"""Run the named synthetic v2 fixture; no observed vessel rows are exported."""

import argparse
import json
from pathlib import Path

from tools.tenergy_v2 import JsonArtifactStore, TenergyV2

FIXTURES = Path(__file__).resolve().parents[1]/"configs/examples/tenergy_v2"


def load_fixture(store):
    history = json.loads((FIXTURES/"history.json").read_text(encoding="utf-8"))
    ref = store.put(history,"history")
    request = json.loads((FIXTURES/"request.json").read_text(encoding="utf-8"))
    if request["history_ref"] != ref:
        raise ValueError("Fixture history hash mismatch")
    return request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir",type=Path,default=Path("models/tenergy-lstm-v5"))
    parser.add_argument("--artifact-dir",type=Path,default=Path("artifacts/tenergy-v2-example"))
    parser.add_argument("--output",type=Path)
    parser.add_argument("--planning",action="store_true")
    args = parser.parse_args()
    store = JsonArtifactStore(args.artifact_dir)
    request = load_fixture(store)
    if args.planning:
        request["usage"] = "planning"
    run = TenergyV2(args.model_dir,store).evaluate_batch(request)
    result = store.get(run["payload_ref"],kind="energy_batch_result") if run["payload_ref"] else None
    document = {"tool_run":run,"result":result}
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(document,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print(json.dumps({"execution_status":run["execution_status"],"outcome_code":run["outcome_code"],
                      "candidates":{c["candidate_id"]:c["outcome_code"] for c in result["candidates"]} if result else None}))


if __name__ == "__main__":
    main()
