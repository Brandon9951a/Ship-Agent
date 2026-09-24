import argparse
import json

from core.orchestrator import run_workflow
from core.llm_layer import LLMClient, LLMConfig, LLMConfigError


def main() -> None:
    parser = argparse.ArgumentParser(description="智行合一 D3 五工具命令行入口")
    parser.add_argument("task", nargs="?", help="中文航行任务")
    parser.add_argument("--llm", action="store_true", help="启用已配置的云端模型做定性解释")
    args = parser.parse_args()
    if not args.task:
        parser.print_help()
        return
    client = None
    if args.llm:
        try:
            client = LLMClient(LLMConfig.from_env())
        except LLMConfigError:
            client = None
    state = run_workflow(args.task, llm_client=client)
    print(json.dumps(state, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
