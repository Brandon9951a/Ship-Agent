"""不依赖 pytest 的最小测试运行器。

仓库的正式测试使用 pytest；本运行器让 ``research/`` 的验收脚本在
pytest 未安装的环境（例如仅带标准库的离线环境）里也能跑完。

用法::

    python -m research.tests.run_tests            # 全部
    python -m research.tests.run_tests contracts  # 文件名包含该串的模块
"""

from __future__ import annotations

import importlib
import sys
import traceback

MODULES = (
    "research.tests.test_contracts",
    "research.tests.test_demand_builder",
    "research.tests.test_trajectory",
    "research.tests.test_prediction",
    "research.tests.test_de_search",
    "research.tests.test_tool_entry",
    "research.tests.test_lstm_adapter",
    "research.tests.test_samples_and_schemas",
    "research.tests.test_migration_v1",
)


def _cases(module):
    for name in dir(module):
        if name.startswith("test_"):
            candidate = getattr(module, name)
            if callable(candidate):
                yield name, candidate
        elif name.startswith("Case") and isinstance(getattr(module, name), type):
            for method in dir(getattr(module, name)):
                if method.startswith("test_"):
                    yield f"{name}.{method}", getattr(module, name)(method)


def main(argv: list[str]) -> int:
    filters = argv[1:]
    passed = failed = 0
    failures: list[tuple[str, str]] = []
    for module_name in MODULES:
        if filters and not any(item in module_name for item in filters):
            continue
        try:
            module = importlib.import_module(module_name)
        except Exception:
            failed += 1
            failures.append((module_name, traceback.format_exc()))
            continue
        for case_name, case in _cases(module):
            label = f"{module_name}::{case_name}"
            try:
                case()
            except Exception:
                failed += 1
                failures.append((label, traceback.format_exc()))
            else:
                passed += 1
    for label, detail in failures:
        print(f"FAIL {label}\n{detail}", file=sys.stderr)
    print(f"passed={passed} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
