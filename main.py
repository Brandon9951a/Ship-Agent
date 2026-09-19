import argparse
import json

from core.orchestrator import run_workflow


def main() -> None:
    parser = argparse.ArgumentParser(description="绿航智算 D2 命令行入口")
    parser.add_argument("task", nargs="?", help="中文航行任务")
    args = parser.parse_args()
    if not args.task:
        parser.print_help()
        return
    state = run_workflow(args.task)
    print(json.dumps(state, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
