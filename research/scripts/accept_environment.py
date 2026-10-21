"""Real API acceptance for the river environment. No scripted model or hidden fallback."""
import argparse
import json
from pathlib import Path

from core.llm_layer import LLMClient, LLMConfig
from research.agent.runtime import AgentRuntime
from research.agent.store import now


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--storage", type=Path, default=Path("var/environment-acceptance-20261020"))
    args = parser.parse_args()
    runtime = AgentRuntime(args.storage, LLMClient(LLMConfig.from_env(env_file=args.env_file)))
    report = {"started_at": now(), "cases": []}
    cases = [
        ("forward", "route_success", "平顶山港", "军李船闸", 6, 85, "research_feasible"),
        ("reverse", "route_success", "军李船闸", "平顶山港", 6, 85, "research_feasible"),
        ("wind_channel", "adverse_environment_demo", "军李船闸", "马湾船闸", 6, 85, "research_feasible"),
        ("tight_deadline", "adverse_current_demo", "马湾船闸", "军李船闸", 1, 85, "infeasible_under_assumptions"),
        ("low_soc", "route_success", "漯河船闸", "漯河港", 2, 25, "infeasible_under_assumptions"),
    ]
    try:
        for case, scenario, origin, destination, hours, soc, expected in cases:
            text = f"从{origin}到{destination}，2026-10-21 09:00出发，甲组电池SOC{soc}%，乙组电池SOC{soc}%，半载，{hours}小时内到达。"
            result = runtime.start(text, scenario_id=scenario, task_type="planning", background=False)
            events, after = [], 0
            while True:
                batch = runtime.store.events(result["task_id"], after)
                events.extend(batch)
                if len(batch) < 200:
                    break
                after = batch[-1]["seq"]
            verification = [e for e in events if e["type"] == "plan_verified"]
            evidence = runtime.store.read(result["task_id"], verification[-1]["artifact_ref"])["payload"] if verification else None
            models = sorted({e["model"] for e in events if e["type"] == "model_completed"})
            passed = (result.get("outcome_code") == expected and bool(models)
                and result.get("answer_status") in ("accepted", "operator_guidance")
                and bool(result.get("final_answer"))
                and (bool(evidence and evidence.get("environment_replay", {}).get("passed")) if expected == "research_feasible" else not verification))
            item = {"case": case, "scenario": scenario, "task_id": result["task_id"],
                "outcome_code": result.get("outcome_code"), "answer_status": result.get("answer_status"),
                "models": models, "actions": [e["tool"] for e in events if e["type"] == "action_requested"],
                "tool_completions": sum(e["type"] == "tool_completed" for e in events),
                "tool_errors": [e for e in events if e["type"] == "tool_failed"],
                "metrics": result.get("metrics"), "refs": result.get("result_refs"),
                "verification": evidence, "final_answer": result.get("final_answer"), "passed": passed}
            report["cases"].append(item)
            (args.storage / "acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"case": case, "outcome": item["outcome_code"], "models": models, "passed": passed}), flush=True)
        report["finished_at"] = now()
        report["passed"] = all(x["passed"] for x in report["cases"])
        (args.storage / "acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if not report["passed"]:
            raise SystemExit(1)
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
