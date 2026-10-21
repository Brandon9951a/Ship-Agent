"""Join browser observations with hash-checked task evidence; never submit tasks."""
import argparse
import hashlib
import json
import math
import re
import sqlite3
from datetime import datetime
from pathlib import Path


def verification_summary(evidence, reference=None):
    """Keep the full verification in its hashed artifact, avoid copying every row."""
    if evidence is None:
        return None
    fdp = evidence.get("fdp", {})
    checks = fdp.get("checks", [])
    return {**evidence, "artifact_ref": reference, "fdp": {
        **{k: v for k, v in fdp.items() if k != "checks"},
        "check_count": len(checks), "failed_checks": [c for c in checks if not c.get("passed")],
    }}


def audit(storage, browser_results):
    db = sqlite3.connect("file:" + (storage / "research.sqlite3").as_posix() + "?mode=ro", uri=True)
    tasks = [json.loads(row[0]) for row in db.execute("SELECT body FROM tasks ORDER BY created")]
    results = []
    def artifact(task, reference):
        data = (storage / "artifacts" / (reference + ".json")).read_bytes()
        if hashlib.sha256(data).hexdigest() != reference:
            raise ValueError("artifact_hash_mismatch")
        body = json.loads(data)
        if body["task_id"] != task:
            raise ValueError("artifact_task_mismatch")
        return body["payload"]
    try:
        for browser in browser_results:
            candidates = [t for t in tasks if t["task_text"] == browser["text"]]
            if not candidates:
                raise ValueError("browser_task_not_found:" + browser["case"])
            task = candidates[-1]
            events = [json.loads(row[0]) for row in db.execute("SELECT body FROM events WHERE task=? ORDER BY seq", (task["task_id"],))]
            expected_failure = browser["case"].startswith(("07_", "08_"))
            expected = "infeasible_under_assumptions" if expected_failure else "research_feasible"
            checks = {"expected_outcome": task.get("outcome_code") == expected,
                      "completed": task["status"] == "completed", "answer_present": bool(task.get("final_answer"))}
            request = task["request"]
            text = browser["text"]
            route = re.search(r"从(.+?)到(.+?)[，,]", text)
            socs = re.findall(r"[甲乙]组电池SOC(\d+(?:\.\d+)?)%", text)
            hours = re.search(r"(\d+(?:\.\d+)?)小时内到达", text)
            load = next(x for x in ("空载", "半载", "满载") if x in text)
            checks["route_preserved"] = (request.get("origin"), request.get("destination")) == route.groups()
            checks["deadline_preserved"] = math.isclose(request.get("remaining_time_s", -1), float(hours[1]) * 3600, abs_tol=1e-6)
            checks["soc_preserved"] = all(math.isclose(request.get(f"initial_soc_{i}", -1), float(socs[i-1])/100, abs_tol=1e-8) for i in (1, 2))
            checks["load_preserved"] = request.get("load_state") == load
            checks["objective_preserved"] = (request.get("objective") or "min_energy") == ("min_time" if "最短航时" in text else "min_energy")
            models = sorted({e["model"] for e in events if e["type"] == "model_completed"})
            checks["real_model"] = bool(models) and all(m != "scripted-test" for m in models)
            diagnostics = [e for e in events if e["type"] == "tool_failed"]
            errors = [e for e in events if e["type"] in ("tool_failed", "verification_failed", "worker_failed")
                      and not (expected_failure and e["type"] == "tool_failed"
                               and e.get("outcome_code") == "infeasible_under_assumptions")]
            checks["no_execution_error"] = not errors
            verifications = [e for e in events if e["type"] == "plan_verified"]
            evidence = artifact(task["task_id"], verifications[-1]["artifact_ref"]) if verifications else None
            metrics = {k: v["value"] for k, v in task.get("metrics", {}).items()}
            view = browser["view"]
            if expected_failure:
                checks["no_success_published"] = not verifications and view["decision-status"] != "仿真方案已生成"
                reason, suggestion = view["infeasible-reason"], view["infeasible-suggestion"]
                checks["actionable_failure"] = bool(reason and suggestion and reason != suggestion)
            else:
                checks["independent_verification"] = bool(evidence and evidence.get("passed") and evidence.get("environment_replay", {}).get("passed"))
                checks["ui_success"] = view["decision-status"] == "仿真方案已生成"
                for key, field, scale, tolerance in (("metric-energy", "bus_energy_kwh", 1, .051),
                        ("metric-time", "duration_s", 1/3600, .00051)):
                    shown = re.search(r"[\d.]+", view[key])
                    checks[key + "_matches"] = bool(shown and abs(float(shown[0]) - metrics.get(field, -1) * scale) <= tolerance)
                shown_socs = re.findall(r"([\d.]+)%", view["metric-soc"])
                checks["ui_soc_matches"] = len(shown_socs) == 2 and all(abs(float(shown_socs[i-1]) - metrics.get(f"final_soc_{i}", -1) * 100) <= .051 for i in (1, 2))
                checks["soc_no_unexplained_charge"] = all(.25 - 1e-8 <= metrics.get(f"final_soc_{i}", -1) <= request[f"initial_soc_{i}"] + 1e-8 for i in (1, 2))
            refs = task.get("result_refs", {})
            speed = artifact(task["task_id"], refs["Tspeed"]) if refs.get("Tspeed") else {}
            seg = artifact(task["task_id"], refs["Tseg"]) if refs.get("Tseg") else {}
            duration = (datetime.fromisoformat(task["updated_at"]) - datetime.fromisoformat(task["run_started_at"])).total_seconds()
            results.append({"case": browser["case"], "task_id": task["task_id"], "task_text": text,
                "status": task["status"], "outcome_code": task.get("outcome_code"), "answer_status": task.get("answer_status"),
                "runtime_s": duration, "models": models, "model_responses": sum(e["type"] == "model_completed" for e in events),
                "actions": [e["tool"] for e in events if e["type"] == "action_requested"],
                "tool_completions": sum(e["type"] == "tool_completed" for e in events), "errors": errors,
                "tool_diagnostics": diagnostics,
                "request": request, "metrics": metrics, "segment_count": len(seg.get("segments", [])),
                "ground_speeds_kmh": speed.get("summary", {}).get("speed_vector_kmh", []),
                "water_speeds_kmh": speed.get("summary", {}).get("water_speed_vector_kmh", []),
                "waiting_s": sum(s["waiting_duration_s"] for s in seg.get("segments", [])),
                "environment_hash": speed.get("environment_hash"), "refs": refs,
                "verification": verification_summary(evidence, verifications[-1]["artifact_ref"] if verifications else None),
                "final_answer": task.get("final_answer"), "browser_view": view, "checks": checks,
                "passed": all(checks.values())})
    finally:
        db.close()
    return {"scope": "local website; real API model; registered simulation environment", "count": len(results),
            "passed": len(results) == 10 and all(r["passed"] for r in results), "cases": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage", type=Path, default=Path("var/environment-web-acceptance/research"))
    parser.add_argument("--browser-results", type=Path, default=Path("var/environment-web-acceptance/ten-voyages/browser-results.json"))
    parser.add_argument("--output", type=Path, default=Path("var/environment-web-acceptance/ten-voyages/audit.json"))
    args = parser.parse_args()
    report = audit(args.storage, json.loads(args.browser_results.read_text(encoding="utf-8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"count": report["count"], "passed": report["passed"], "failures": [
        {"case": r["case"], "checks": [k for k, v in r["checks"].items() if not v]} for r in report["cases"] if not r["passed"]]}))


if __name__ == "__main__":
    main()
