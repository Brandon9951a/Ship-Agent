# 系统架构

## 1. 分层

| 层级 | 目录 | 主要职责 |
|---|---|---|
| 接口层 | `ui/`、`main.py` | Web 页面、HTTP API、语音桥接和命令行入口 |
| 编排层 | `core/orchestrator.py` | LangGraph 状态、节点、条件路由和人工中断 |
| 运行层 | `core/workflow_runtime.py` | checkpoint 生命周期、任务创建、恢复、查询和并发锁 |
| 业务层 | `tools/` | 数据准备、航段划分、能耗估算、航速优化和能量管理 |
| 数据层 | `schemas/`、`configs/` | 结构化消息、船舶事实、航线事实、约束和演示策略 |
| 验证层 | `tests/`、`scripts/` | 单元测试、场景测试、批量实验、审计和打包 |

## 2. 工作流

正常路径按固定顺序执行：

```text
START → parse → Tdata → Tseg → Tenergy → Tspeed → Tmanagement → finalize → END
```

每个工具返回统一状态。缺少必要参数或工具失败时，流程停止并生成对应状态，不把不完整结果包装成成功方案。

Tspeed 判定不可行后进入人工确认路径：

```text
Tspeed → prepare_adjustment → await_choice
                              │ interrupt + checkpoint
                              ▼
                       apply_adjustment
                              │
                              └→ Tdata → 完整五工具链
```

`prepare_adjustment` 生成并验证选项。`await_choice` 只处理中断与恢复值。`apply_adjustment` 从状态中读取服务端保存的修改内容，客户端只能提交 `option_id`。

## 3. 持久化与恢复

- 本地使用 `AsyncSqliteSaver`，默认数据库为 `var/checkpoints.sqlite3`。
- Render 使用 `AsyncPostgresSaver`，连接串由 Blueprint 注入。
- 应用 lifespan 创建 checkpointer、执行 `setup()` 并编译一次图。
- 所有 Web 调用使用同一个应用级工作流实例。
- 每个 `thread_id` 使用独立异步锁，避免重复恢复。
- 恢复前再次核对当前节点、`decision_id` 和 `option_id`。
- 达到两轮重算上限后结束，不产生第三次中断。

## 4. 数值边界

大语言模型不生成或修改距离、速度、功率、能耗、时间、SOC 和约束阈值。数值由确定性工具计算，报告层只能解释已有结果。`core/value_lock.py` 对模型说明执行数值锁定检查，异常时回退到本地模板。

## 5. 部署

FastAPI 同时提供静态驾驶舱和 API。Render Blueprint 创建一个 Web Service 和一个同区域 PostgreSQL。`/healthz` 报告 checkpoint 与语音服务状态，用于部署健康检查。

## 6. 适用范围

当前系统为航行辅助决策原型。演示模型、候选航速和 SOC 边界用于软件验证；实船部署前仍需完成传感器协议核对、能耗模型标定、航道约束确认、安全审查和运营审批。
