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

全量测试未形成可交付的全通过结果。既有 `test_inspect_data.py` 和 `test_llm_layer.py` 出现错误，涉及数据/模型测试环境；D2 专项测试、schema 测试和路由证据测试已分别通过。错误没有被隐瞒为通过，需后续由 A/B 按测试环境补齐。

