"""Text UI for the real five-tool synthetic-demo workflow."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.orchestrator import run_structured_workflow


ROOT = Path(__file__).resolve().parents[1]


def run(payload: dict) -> dict:
    state = run_structured_workflow(payload)
    result = {
        "status": state["status"],
        "missing_fields": state.get("missing_fields", []),
        "questions": state.get("questions", []),
        "task_understanding": state.get("task_understanding", {}),
        "final_message": state["final_message"],
        "trace": state.get("trace", []),
    }
    for response in state.get("tool_results", []):
        result[response["tool"].lower()] = response
    if "plan" in state:
        result["plan"] = state["plan"]
        result["report"] = state["report"]
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="绿航智算 D3 文本演示界面")
    parser.add_argument("--input", default=ROOT / "configs/examples/voyage_request.json")
    args = parser.parse_args()
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    print(json.dumps(run(payload), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
