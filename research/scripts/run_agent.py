"""Run the native-model research agent from the command line."""
import argparse
import json
from pathlib import Path

from core.llm_layer import LLMClient, LLMConfig
from research.agent.runtime import AgentRuntime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--storage", type=Path, default=Path("var/research-agent"))
    parser.add_argument("--scenario", default="route_success")
    parser.add_argument("--task-type", default="auto", choices=["auto", "planning", "prediction", "question"])
    parser.add_argument("--text", default="从漯河船闸到漯河港，按当前仿真情景规划节能航行。")
    parser.add_argument("--resume", help="Waiting task ID; --text supplies the reply.")
    parser.add_argument("--response-id", default="cli-response")
    args = parser.parse_args()
    runtime = AgentRuntime(args.storage, LLMClient(LLMConfig.from_env(env_file=args.env_file)))
    try:
        if args.resume:
            pending = runtime.store.get(args.resume, "local")["pending"]
            result = runtime.resume(args.resume, "local", pending_id=pending["pending_id"],
                revision=pending["revision"], response_id=args.response_id, reply_text=args.text, background=False)
        else:
            result = runtime.start(args.text, scenario_id=args.scenario, task_type=args.task_type, background=False)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result["status"] == "failed" or result.get("answer_status") in ("rejected", "unavailable"):
            raise SystemExit(1)
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
