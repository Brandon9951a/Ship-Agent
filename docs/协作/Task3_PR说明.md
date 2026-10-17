# 任务 3：服务存活、进行中任务恢复与结果一致性

**PR 标题**

```
fix(research): 服务重启后进行中任务恢复、取消重试与单次发布
```

**base 必须是 `codex/three-tools-review-20261015`，不是 `main`。**

---

## 修改内容

按《三项稳定性改进任务与验收标准_2026-10-17》任务 3，修复“服务重启后进行中任务
直接变成 `process_interrupted`”的问题，并补齐队列、超时、取消／重试与结果一致性。

1. **启动恢复**（`research/agent/runtime.py`）
   - 启动时扫描 `queued`／`running` 任务：检查点仍有待执行节点 → 从 LangGraph
     检查点续跑；否则从原始输入快照安全重试；
   - 每次恢复记录 `recovery_scheduled` 事件（`previous_status`／`path`／`has_checkpoint`）；
   - 超过 `SHIP_AGENT_MAX_RECOVERY_ATTEMPTS` 才落 `recovery_exhausted`，不再一律
     `process_interrupted`。
2. **单次执行与单次发布**
   - `Store.transition()` 用 `BEGIN IMMEDIATE` 原子领取任务，两个进程共用同一 SQLite
     时只有一个能执行；
   - `_finish`／`_execute(prepare_final)` 对已完成任务短路，记录 `answer_reused`；
   - 同一任务只产生一次 `answer_completed`。
3. **超时／取消／重试**
   - 看门狗按任务级超时置 `cancel_requested`，在图的下一个决策节点协作式终止；
   - 新增 `POST /api/runs/{id}/cancel`、`POST /api/runs/{id}/retry`；
   - 取消 (`task_cancelled`) 与超时 (`task_timeout`) 是独立终态，不写发布工件。
4. **队列与可观察性**
   - 活跃任务上限；满时返回 **429** `research_queue_full`（含 `retry_after_s`）；
   - 任务写入 `phase`／`queue_entered_at`／`run_started_at`／`heartbeat_at`／`recovery_attempts`；
   - 新增 `GET /api/admin/research-runtime`（队列深度、超时边界、上次恢复统计）。
5. **前端**
   - 显示“排队中／计算中 · 阶段／已恢复 N 次”，提供取消与重试按钮；
   - 断线自动重试；长任务不再误报失败，刷新后继续同一 `thread_id`。

**改动范围**（13 个文件，+1430/−32）：`research/agent/runtime.py`、`research/agent/store.py`、
`ui/web_server.py`、`ui/frontend/{app.js,index.html,research-bridge.js,research-bridge.css}`、
`.env.example`、`.gitignore`、新增两个测试文件、证据脚本、交付说明。

## 验证结果

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest tests/test_runtime_recovery.py tests/test_runtime_recovery_api.py -q
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m scripts.run_task3_recovery_acceptance --output-dir artifacts/task3-evidence
```

- **本任务专项：20 项通过**（恢复 13 + 网站接口 7）。
- **全量：523 通过 / 3 失败**；3 个失败与基线 `codex/three-tools-review-20261015`
  完全相同（`Tenergy interval energy differs from P * duration / 3600` 及 LSTM 短时
  用例），属任务 1（Tenergy）范围，本 PR 未改动该路径、未计入修复成果。
- 中断阶段覆盖：排队、模型调用中、**Tspeed 搜索中**、**FDP 求解中**；
  重启后均到达明确终态，事件与工件无重复冲突。

**可复现证据**（`artifacts/task3-evidence/task3_recovery_evidence.json`）：

| 场景 | 任务 ID | 结果 |
|---|---|---|
| 计算中被杀 → 重启 | `482f7915-6443-40d5-b6bb-f3f120882682` | `running` → `completed`；`resume_from_checkpoint`；`answer_completed=1` |
| 运行中取消 | `0963c159-a4e4-4462-9853-72d1b008c08a` | `cancelled`／`task_cancelled`；`plan_verified=0`；无 `answer_ref` |
| 取消后重试 | 同上 | `completed`／`research_feasible`；`answer_completed=1` |
| finish 重放 | `03591b91-edc3-4797-b184-5a53089b3c20` | 首次发布，重放返回空并记 `answer_reused` |

重启场景工具调用记录（节选）：`Tdata ok → Tseg ok → Tenergy.candidate ×N（父调用 ID
关联 Tspeed）→ Tspeed ok → Tmanagement ok → plan_verified → answer_completed`；
工件含 `PowerDemandSeries`、`Tspeed`、`Tmanagement`、`plan_verification`、`answer`，
均带 SHA-256，可由 `store.read()` 复算。

**模型标识**：`task3-scripted`（确定性脚本模型，**不访问网络**）。工具链为真实实现。
真实 DeepSeek 调用需配置 `SHIP_LLM_*` 后执行：

```powershell
Copy-Item .env.example .env   # 填 SHIP_LLM_API_KEY，设 SHIP_LLM_ENABLED=true
.\.venv\Scripts\python.exe -m ui.web_server --host 127.0.0.1 --port 8765 --llm
```

## 接口与配置

- 新增 `POST /api/runs/{id}/cancel`：取消排队／运行中／等待补充任务；返回任务状态。
- 新增 `POST /api/runs/{id}/retry`：仅对可重试结果（`model_unavailable`、`tool_error`、
  `task_timeout`、`queue_timeout`、`recovery_exhausted` 等）开放；已完成不重发。
- 新增 `GET /api/admin/research-runtime`（管理员）：队列深度、限制、上次恢复统计。
- `POST /api/run` 队列满返回 429 `research_queue_full`（`limit`／`active`／`retry_after_s`）。
- 任务字段新增：`phase`、`queue_entered_at`、`run_started_at`、`heartbeat_at`、
  `recovery_attempts`、`recovery_path`、`cancel_requested`、`cancel_reason`、
  `manual_retries`；新终态码 `task_cancelled`、`task_timeout`、`queue_timeout`、
  `recovery_exhausted`。
- 环境变量：`SHIP_AGENT_MAX_ACTIVE_TASKS`（默认 8）、`SHIP_AGENT_TASK_TIMEOUT_S`（900）、
  `SHIP_AGENT_QUEUE_TIMEOUT_S`（900）、`SHIP_AGENT_MAX_RECOVERY_ATTEMPTS`（3）、
  `SHIP_AGENT_WATCHDOG_INTERVAL_S`（2）。
- 未改变：`/healthz` 响应字段、既有鉴权/CSRF、v1 演示工作流、Tspeed—FDP 交接逻辑。

## 已知限制

1. **自动验收用脚本模型**：本机无 DeepSeek 凭据；联合验收要求的真实模型调用需由项目
   负责人执行，命令见上。
2. **超时为协作式**：工具内部长计算不会被硬打断，最坏情况是当前工具返回后才终止。
3. **队列为单进程语义**：多 worker 部署需共享队列／分布式领取；本轮只保证同一 SQLite
   文件上的领取互斥。
4. **恢复仅覆盖 LangGraph 研究任务**：v1 `workflow_runtime` 的中断语义未改动。
5. **取消后重试从输入快照重跑**：复用已生成工件，但会重新走模型与工具。
6. **72 组起终点覆盖、Tenergy 后端选择**属任务 1／任务 2，本 PR 不含。
7. 研究仿真边界不变：结果仍标 `synthetic_demo`，不代表实船安全或航行批准。
