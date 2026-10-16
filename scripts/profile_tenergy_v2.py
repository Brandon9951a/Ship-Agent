"""Warm 500-candidate cProfile run, separate from uninstrumented timings."""

import copy
import cProfile
import json
from pathlib import Path
import pstats

import torch

from scripts.example_tenergy_v2 import load_fixture
from tools.tenergy_v2 import JsonArtifactStore, TenergyV2


def main():
    torch.set_num_threads(4)
    output = Path("profile_output/tenergy_v2_hotspots")
    store = JsonArtifactStore(output/"artifacts")
    request = load_fixture(store)
    valid = request["candidates"][:2]
    request["candidates"] = [dict(copy.deepcopy(valid[i%2]),candidate_id=f"profile-{i}") for i in range(500)]
    predictor = TenergyV2("models/tenergy-lstm-v5",store)
    predictor.evaluate_batch(request)
    profiler = cProfile.Profile()
    profiler.enable()
    run = predictor.evaluate_batch(request)
    profiler.disable()
    profiler.dump_stats(output/"adapter.pstats")
    stats = pstats.Stats(profiler)
    top = []
    for (filename,line,function),(primitive,calls,self_time,cumulative,_) in sorted(
            stats.stats.items(),key=lambda item:item[1][3],reverse=True)[:25]:
        top.append({"file":Path(filename).name,"line":line,"function":function,"calls":calls,
                    "self_seconds":self_time,"cumulative_seconds":cumulative})
    report = {"candidate_count":500,"candidate_duration_s":[5,25],"outcome_code":run["outcome_code"],
              "method":"cProfile warmed repeat; overhead included, not used as benchmark timing",
              "total_profile_seconds":stats.total_tt,"top_cumulative_functions":top}
    path = Path("models/tenergy-lstm-v5/improvement/profile_hotspots.json")
    path.write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(top[:8]))


if __name__ == "__main__":
    main()
