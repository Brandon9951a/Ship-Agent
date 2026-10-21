"""Ten risk-focused voyages through the website's runtime, with a real API model.

This is backend acceptance, not browser/visual acceptance. Keep initial and
retest runs separately; unresolved search is a failed acceptance check.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import json
import math
from pathlib import Path
import time

from core.llm_layer import LLMClient, LLMConfig
from research.agent.runtime import AgentRuntime
from research.scripts.audit_web_voyages import verification_summary


CASES = [
    ("01_reported_full_load", "马湾船闸", "周口港", 80, 90, "满载", 12, "min_energy", "energy"),
    ("02_entire_route_empty", "平顶山港", "周口港", 100, 100, "空载", 30, "min_energy", "plan_or_energy"),
    ("03_entire_reverse_empty", "周口港", "平顶山港", 100, 100, "空载", 30, "min_energy", "plan_or_energy"),
    ("04_reverse_many_locks", "葫芦湾船闸", "漯河船闸", 95, 85, "空载", 9, "min_energy", "plan"),
    ("05_unbalanced_energy", "军李船闸", "马湾船闸", 31, 95, "半载", 6, "min_energy", "plan"),
    ("06_one_at_floor", "周口港", "周口船闸", 25, 95, "半载", 10, "min_energy", "plan"),
    ("07_one_below_floor", "漯河港", "漯河船闸", 20, 85, "半载", 2, "min_energy", "input"),
    ("08_full_load_energy_edge", "马湾船闸", "周口港", 100, 100, "满载", 12, "min_energy", "plan"),
    ("09_impossible_deadline", "马湾船闸", "周口港", 100, 100, "满载", 6, "min_time", "time"),
    ("10_decimal_deadline", "军李船闸", "葫芦湾船闸", 80, 90, "半载", 29.006, "min_energy", "plan"),
]


def run_case(case, env_file, directory):
    name, origin, destination, soc1, soc2, load, hours, objective, expected = case
    text = f"从{origin}到{destination}，2026-10-22 09:00出发，甲组电池SOC{soc1}%，乙组电池SOC{soc2}%，{load}，{hours}小时内到达"
    if objective == "min_time":
        text += "，优先最短航时"
    runtime = AgentRuntime(directory / name, LLMClient(LLMConfig.from_env(env_file=env_file)))
    started = time.monotonic()
    try:
        task = runtime.start(text, task_type="auto", background=False)
        events, after = [], 0
        while True:
            batch = runtime.store.events(task["task_id"], after)
            events.extend(batch)
            if len(batch) < 200:
                break
            after = batch[-1]["seq"]
        refs = task.get("result_refs", {})
        artifacts = {key: runtime.store.read(task["task_id"], ref)["payload"] for key, ref in refs.items()}
        verifications = [e for e in events if e["type"] == "plan_verified"]
        verification = runtime.store.read(task["task_id"], verifications[-1]["artifact_ref"])["payload"] if verifications else None
        request = task.get("request", {})
        models = sorted({e["model"] for e in events if e["type"] == "model_completed"})
        diagnostics = [d for body in artifacts.values() for d in body.get("diagnostics", [])]
        codes = {d.get("code") for d in diagnostics}
        outcome = task.get("outcome_code")
        metrics = {k: v["value"] for k, v in task.get("metrics", {}).items()}
        guidance = task.get("operator_guidance") or {}
        checks = {
            "terminal": task["status"] == "completed",
            "route_preserved": (request.get("origin"), request.get("destination")) == (origin, destination),
            "soc_preserved": all(math.isclose(request.get(f"initial_soc_{i}", -1), soc/100, abs_tol=1e-9) for i, soc in ((1,soc1),(2,soc2))),
            "deadline_preserved": math.isclose(request.get("remaining_time_s", -1), hours*3600, abs_tol=1e-6),
            "load_preserved": request.get("load_state") == load,
            "objective_preserved": (request.get("objective") or "min_energy") == objective,
            "real_model": bool(models) and all(m != "scripted-test" for m in models),
            "no_crash": not any(e["type"] in ("worker_failed", "verification_failed") or
                (e["type"] == "tool_failed" and e.get("outcome_code") == "tool_error") for e in events),
            "not_search_unresolved": outcome not in ("solver_unresolved", "agent_budget_exhausted"),
            "answer_present": bool(task.get("final_answer")),
        }
        energy_failure = outcome == "infeasible_under_assumptions" and "selected_demand_energy_exceeds_available" in codes
        checks["expected_outcome"] = (
            outcome == "research_feasible" if expected == "plan" else
            energy_failure if expected == "energy" else
            outcome == "research_feasible" or energy_failure if expected == "plan_or_energy" else
            outcome == "infeasible_under_assumptions" and "deadline_below_physical_minimum" in codes if expected == "time" else
            outcome == "invalid_input" and "initial_soc_below_planning_floor" in codes)
        if outcome == "research_feasible":
            checks["verified"] = bool(verification and verification.get("passed") and verification.get("environment_replay",{}).get("passed"))
            checks["duration_within_limit"] = metrics.get("duration_s", float("inf")) <= hours*3600+1e-6
            checks["soc_within_bounds"] = all(.25-1e-8 <= metrics.get(f"final_soc_{i}",-1) <= soc/100+1e-8 for i, soc in ((1,soc1),(2,soc2)))
        else:
            checks["no_success_published"] = not verifications
            checks["separate_actionable_guidance"] = bool(guidance.get("reason") and guidance.get("suggestion") and guidance["reason"] != guidance["suggestion"])
            if energy_failure:
                budget = guidance.get("energy_budget") or {}
                checks["quantified_shortfall"] = bool(budget.get("shortfall_bus_kwh",0)>0 and "候选" in guidance.get("suggestion", ""))
            if expected == "input":
                checks["specific_input_guidance"] = "20%" in guidance.get("reason", "") and "25%" in guidance.get("reason", "")
        speed = artifacts.get("Tspeed",{})
        management = artifacts.get("Tmanagement",{})
        segment = artifacts.get("Tseg",{})
        return {"case":name,"text":text,"expected":expected,"task_id":task["task_id"],
            "runtime_s":round(time.monotonic()-started,3),"outcome_code":outcome,"status":task["status"],
            "request":request,"metrics":metrics,"models":models,
            "actions":[e["tool"] for e in events if e["type"]=="action_requested"],
            "diagnostics":diagnostics,"guidance":guidance,"final_answer":task.get("final_answer"),
            "speed_summary":speed.get("summary"),"segment_count":len(segment.get("segments",[])),
            "waiting_s":sum(s["waiting_duration_s"] for s in segment.get("segments",[])),
            "fdp_attempts":(management.get("result") or {}).get("search_attempts")
                or ((management.get("result") or {}).get("payload") or {}).get("search_attempts"),
            "dispatch_method":management.get("summary", {}).get("dispatch_method",
                "fdp" if (management.get("result") or {}).get("outcome_code") == "ok" else "not_completed"),
            "verification":verification_summary(verification,verifications[-1]["artifact_ref"] if verifications else None),
            "refs":refs,"checks":checks,"passed":all(checks.values())}
    finally:
        runtime.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file",type=Path,required=True)
    parser.add_argument("--storage",type=Path,default=Path("var/voyage-matrix-20261021"))
    parser.add_argument("--label",default="initial")
    parser.add_argument("--cases",nargs="*",type=int)
    parser.add_argument("--workers",type=int,choices=(1,2),default=1)
    args = parser.parse_args()
    directory = args.storage/args.label
    directory.mkdir(parents=True,exist_ok=True)
    report = {"scope":"real API backend runtime shared by website; not browser acceptance",
        "started_at":datetime.now().astimezone().isoformat(),"label":args.label,"cases":[]}
    selected = [c for i,c in enumerate(CASES,1) if not args.cases or i in args.cases]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_case,c,args.env_file,directory):c for c in selected}
        for future in as_completed(futures):
            case = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {"case":case[0],"passed":False,"exception_type":type(exc).__name__,"error":str(exc)}
            report["cases"].append(result)
            report["cases"].sort(key=lambda x:x["case"])
            report["passed"] = len(report["cases"])==len(selected) and all(c["passed"] for c in report["cases"])
            (directory/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
            print(json.dumps({"case":result["case"],"outcome":result.get("outcome_code"),
                "passed":result["passed"],"failed_checks":[k for k,v in result.get("checks",{}).items() if not v],
                "error":result.get("error")}),flush=True)
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
