# D2 C 环境记录

日期：2026-09-19
分支：`d2-lgz`

## 检查结果

| 项目 | 实际结果 |
|---|---|
| 系统 `python` | Windows 商店占位符，无法执行，返回 9009 |
| Bundled Python | 3.12.14，可执行 |
| pytest | 初始未安装，已安装 pytest 9.1.1 到 Bundled Python 环境 |
| 工程依赖 | `pyproject.toml` 仍为标准库运行依赖，未修改 |
| 默认示例 CLI | 可运行，返回 `need_clarification` |

## 测试命令

```text
C:\Users\Lucien\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pytest tests/test_d2_tools.py -q
```

结果：4 passed。

```text
C:\Users\Lucien\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pytest tests/test_schemas.py -q
```

结果：83 passed。

```text
C:\Users\Lucien\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pytest tests/test_c_route_evidence.py -q
```

结果：24 passed。

```text
C:\Users\Lucien\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m ui.app --input configs/examples/voyage_request.json
```

结果：命令成功运行，Tdata 返回 `need_clarification`，Tseg 明确列出未知限速和等待字段。

## 全量测试说明

C 提交时未形成全量测试通过结果。A 集成到最新 `main`、补齐项目环境并修正规则后，全量测试结果为 353 passed。
