"""Reproducible CPU/API timings plus separate Python and process memory samples."""

import argparse
import copy
import ctypes
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import time
import tracemalloc

import torch

from scripts.example_tenergy_v2 import load_fixture
from tools.tenergy_v2 import JsonArtifactStore, TenergyV2


def process_memory():
    if os.name != "nt":
        import resource
        return {"peak_working_set_bytes":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024}
    size = ctypes.c_size_t
    class Counters(ctypes.Structure):
        _fields_ = [("cb",ctypes.c_ulong),("PageFaultCount",ctypes.c_ulong)]+[
            (name,size) for name in ("PeakWorkingSetSize","WorkingSetSize","QuotaPeakPagedPoolUsage",
                                    "QuotaPagedPoolUsage","QuotaPeakNonPagedPoolUsage","QuotaNonPagedPoolUsage",
                                    "PagefileUsage","PeakPagefileUsage")]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.windll.kernel32
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    query = ctypes.windll.psapi.GetProcessMemoryInfo
    query.argtypes = [ctypes.c_void_p,ctypes.POINTER(Counters),ctypes.c_ulong]
    if not query(kernel.GetCurrentProcess(),ctypes.byref(counters),counters.cb):
        raise OSError("GetProcessMemoryInfo failed")
    return {"working_set_bytes":counters.WorkingSetSize,"peak_working_set_bytes":counters.PeakWorkingSetSize}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=Path("models/tenergy-lstm-v5/improvement/performance.json"))
    parser.add_argument("--profile-dir",type=Path,default=Path("profile_output/tenergy_v2"))
    parser.add_argument("--counts",type=int,nargs="+",default=[50,500,5000])
    parser.add_argument("--repeats",type=int,default=3)
    args = parser.parse_args()
    if any(n<1 for n in args.counts) or args.repeats<2:
        parser.error("Positive counts and at least two repeats required")
    torch.set_num_threads(4)
    store = JsonArtifactStore(args.profile_dir/"artifacts")
    fixture = load_fixture(store)
    predictor = TenergyV2("models/tenergy-lstm-v5",store)
    before = process_memory()
    start = time.perf_counter()
    predictor._load()
    load_seconds = time.perf_counter()-start
    cases = []
    for count in args.counts:
        req = copy.deepcopy(fixture)
        req["candidates"] = []
        for i in range(count):
            length = (1,3,5)[i%3]
            speed = 9.5+(i%100)*.01
            rows = [{"segment_id":f"S{j//2+1}","phase":"moving","start_offset_s":j*5,
                     "duration_s":5,"planned_speed_kmh":speed,"environment_ref":None} for j in range(length)]
            req["candidates"].append({"candidate_id":f"candidate-{i}","trajectory":rows})
        predictor.evaluate_batch(req)  # warm filesystem/model paths before timing
        timings,hashes = [],[]
        for _ in range(args.repeats):
            start = time.perf_counter()
            run = predictor.evaluate_batch(req)
            timings.append(time.perf_counter()-start)
            hashes.append(run["payload_ref"]["sha256"])
        batch = store.get(run["payload_ref"])
        if any(c["outcome_code"]!="ok" for c in batch["candidates"]) or len(set(hashes))!=1:
            raise ValueError("Benchmark forecast rejected or nondeterministic")
        # Memory measurement is separate so tracing overhead does not inflate timings.
        tracemalloc.start()
        predictor.evaluate_batch(req)
        _,python_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        cases.append({"candidate_count":count,"interval_count":sum(len(c["trajectory"]) for c in req["candidates"]),
                      "candidate_duration_s":[5,15,25],"prediction_backend":"CPU sequential candidate inference",
                      "end_to_end_seconds":timings,"median_seconds":statistics.median(timings),
                      "python_traced_peak_bytes":python_peak,"process_memory":process_memory(),
                      "identical_result_hash_across_repeats":len(set(hashes))==1})
        print(f"benchmarked {count} candidates: {statistics.median(timings):.3f}s",flush=True)
    reload_predictor = TenergyV2("models/tenergy-lstm-v5",store)
    first = predictor.evaluate_batch(fixture)
    again = reload_predictor.evaluate_batch(fixture)
    long_req = copy.deepcopy(fixture)
    template = long_req["candidates"][0]["trajectory"][0]
    long_req["candidates"] = [{"candidate_id":f"long-{i}","trajectory":[dict(template,start_offset_s=j*5) for j in range(360)]}
                              for i in range(50)]
    start = time.perf_counter()
    long_run = predictor.evaluate_batch(long_req)
    rejected_seconds = time.perf_counter()-start
    long_batch = store.get(long_run["payload_ref"])
    if not all(c["outcome_code"]=="model_out_of_domain" for c in long_batch["candidates"]):
        raise ValueError("Long horizon was not rejected")
    try:
        gpu = subprocess.run(["nvidia-smi","--query-gpu=name,memory.total","--format=csv,noheader"],
                             capture_output=True,text=True,timeout=10).stdout.strip() or None
    except (OSError,subprocess.TimeoutExpired):
        gpu = None
    report = {"torch":torch.__version__,"python":platform.python_version(),"platform":platform.platform(),
              "cpu":platform.processor(),"cpu_logical_cores":os.cpu_count(),"torch_threads":torch.get_num_threads(),
              "gpu_inventory":gpu,"cuda_available_in_torch":torch.cuda.is_available(),
              "gpu_benchmark":None,"gpu_not_measured_reason":"Installed torch build is CPU-only" if not torch.cuda.is_available() else "CPU API benchmark only",
              "model_bundle_load_seconds_excluding_torch_import":load_seconds,"baseline_process_memory":before,
              "cases":cases,"reload_identical_result_hash":first["payload_ref"]["sha256"]==again["payload_ref"]["sha256"],
              "unsupported_horizon_case":{"candidate_count":50,"duration_s":1800,"intervals_per_candidate":360,
                 "seconds":rejected_seconds,"outcome":"model_out_of_domain","predicted_intervals":0},
              "timing_scope":"Validation, inference, integration, serialization, hash verification and local artifact I/O; warm repeats, no tracemalloc",
              "memory_scope":"tracemalloc: separate-run Python allocations only; process peak working set includes native torch and earlier cases, not per-call delta",
              "limitations":["Synthetic candidate workload, not complete-route DE or approved planner timing",
                             "Long-horizon rejection cost is not prediction throughput; no rolling bypass",
                             "No cross-tool integration or global parameter decisions"]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")


if __name__ == "__main__":
    main()
