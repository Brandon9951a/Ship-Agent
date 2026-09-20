"""Run the D3 normal synthetic-demo task through parser and all five tools."""

import json

from core.orchestrator import run_workflow


TASK = (
    "从平顶山港到军李船闸，2026-09-21 08:00出发，"
    "2026-09-21 11:00到达，SOC85%，半载"
)


def main() -> int:
    state = run_workflow(TASK)
    assert state["task_understanding"]["authoritative_source"] == (
        "deterministic_parser_and_schema_validation"
    )
    output = {
        "status": state["status"],
        "real_ship_validation": False,
        "task_understanding": state["task_understanding"],
        "final_message": state["final_message"],
        "trace": state["trace"],
        "report": state.get("report"),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if state["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
