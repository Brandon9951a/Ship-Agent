# 任务 3：服务存活、进行中任务恢复与结果一致性

日期：2026-10-17
工作基线：`codex/three-tools-review-20261015`
负责：徐（Tspeed／联调）
PR base：`codex/three-tools-review-20261015`（不并入 `main`）

## 一、本轮修复的问题

| # | 已确认缺陷 | 后果 | 处理 |
|---|---|---|---|
| 1 | `AgentRuntime.__init__` 启动时把 `running`／`queued` 任务一律改成 `failed` + `process_interrupted` | 服务重启后，已保存的检查点与工件无法继续，用户看到“进程中断” | 改为启动恢复：从 LangGraph 检查点续跑，或从原始输入快照安全重试 |
| 2 | 队列只在进程内（`ThreadPoolExecutor` + 字典），无持久排队语义 | 重启即丢队列；无并发上限 | 任务状态持久化；启动时重新入队；活跃任务上限（429 可重试） |
| 3 | `_finish` 无幂等保护 | 恢复重放可能重复发布答复／重复 `plan_verified` | 已完成任务短路复用；同一任务只产生一次 `answer_completed` |
| 4 | 无任务级超时、无取消／重试语义 | 长任务无限占用；用户无法中止或恢复 | 看门狗超时（协作式取消）、`cancel`／`retry` 接口与状态 |
| 5 | 阶段状态不可见 | 刷新页面只能看到“计算中/失败”，无法定位卡在哪 | 任务记录写入 `phase`、`queue_entered_at`、`run_started_at`、`heartbeat_at`、`recovery_attempts` |
| 6 | 页面等待窗口 360 秒后直接报错；断线即判失败 | 长任务被误报为失败，刷新后恢复提示误导 | 等待窗口延长、断线重试；仍无终态时保留任务标识并提示刷新继续 |

## 二、实现要点

### 1. 启动恢复（`research/agent/runtime.py`）

`AgentRuntime.__init__` → `_recover()`：

- 扫描 `queued`／`running` 任务（`waiting_human` 本来就可恢复，跳过）；
- 用 `graph.get_state()` 判断检查点**是否还有待执行节点**：
  - 有 → `resume_from_checkpoint`：`graph.invoke(None)` 从最后一个提交的快照继续；
  - 没有（任务已到 END、或从未提交检查点）→ `restart_from_input_snapshot`：由任务记录重建初始状态重跑；
- 每次恢复递增 `recovery_attempts`，写入 `recovery_scheduled` 事件（含 `previous_status`、`path`、`has_checkpoint`）；
- 超过 `SHIP_AGENT_MAX_RECOVERY_ATTEMPTS` 才落 `failed` + `recovery_exhausted`，并给出“可重试”的说明，**不再**是 `process_interrupted`。

**为什么区分“续跑”与“重跑”**：检查点已到 END 时 `invoke(None)` 会空转，任务会停在 `running`。本实现据此回退到输入快照重试（该缺陷已由 `test_retry_after_cancel_restarts_from_input` 锁定）。

### 2. 单次执行与单次发布

- `Store.transition()` 用 `BEGIN IMMEDIATE` 做状态比较并交换：两个进程共用同一 SQLite 时只有一个能领取任务；
- `_run` 领取失败（已完成／已取消／被别的 worker 领走）直接跳过并记录 `run_skipped`；
- `_finish` 与 `_execute(prepare_final)` 在任务已 `completed` 且已有 `answer_ref` 时短路，记录 `answer_reused`／`finish_reused`，不再写第二个答复工件；
- `Store.put` 是内容寻址（同内容同哈希），重复写不会产生第二个文件。

### 3. 超时、取消与重试

- 看门狗线程按 `SHIP_AGENT_WATCHDOG_INTERVAL_S` 扫描：
  - `running` 超过 `SHIP_AGENT_TASK_TIMEOUT_S` → 置 `cancel_requested`，记 `task_timeout`；
  - `queued` 超过 `SHIP_AGENT_QUEUE_TIMEOUT_S` → `failed` + `queue_timeout`（未开始计算，可重试）；
- 取消是**协作式**的：在图的下一个 `reconcile` 节点终止，不进入工具执行，也不发布方案；
- `cancelled` 与 `task_timeout` 是独立终态，**不写发布工件**，`answer_ref` 为空；
- `retry()` 仅在 `failed`／`cancelled` 且结果为可重试码时允许；重试清空终态字段，保留历史事件与工件，增量记录 `manual_retries`。

### 4. 网站与界面

- `POST /api/runs/{id}/cancel`、`POST /api/runs/{id}/retry`（沿用同源 + CSRF 中间件）；
- 队列满返回 **429** `research_queue_full`（`limit`／`active`／`retry_after_s`），不是伪造失败终态；
- `GET /api/admin/research-runtime`：队列深度、超时／恢复边界、上次恢复统计（仅管理员）；
- `GET /healthz` 保持最小字段不变，计算任务运行期间仍即时响应；
- 前端：显示“排队中／计算中 · 阶段／已恢复 N 次”，提供**取消任务**与**重试任务**按钮；页面刷新或断线继续轮询同一 `thread_id`。

## 三、改动文件

```
research/agent/runtime.py        启动恢复、原子领取、幂等收尾、看门狗、取消/重试、队列上限
research/agent/store.py          原子状态迁移 transition()、按状态计数 count_status()
ui/web_server.py                 /cancel、/retry、429 队列满、/api/admin/research-runtime
ui/frontend/research-bridge.js   断线重试、阶段展示、cancel/retry、可重试码
ui/frontend/app.js              任务阶段、取消/重试按钮、长任务不再误报失败
ui/frontend/index.html           #task-controls（阶段 + 取消 + 重试）
ui/frontend/research-bridge.css  任务控制条样式
.env.example                     运行时恢复与队列环境变量
tests/test_runtime_recovery.py          13 项（恢复/工具阶段中断/取消/超时/排队超时/上限/重试/单次发布/跨实例领取）
tests/test_runtime_recovery_api.py      7 项（healthz/429/cancel/retry/重启取回/重新登录取回）
scripts/run_task3_recovery_acceptance.py 可复现证据脚本
```

## 四、运行命令

```powershell
# 环境（首次）
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

# 本任务测试
.\.venv\Scripts\python.exe -m pytest tests/test_runtime_recovery.py tests/test_runtime_recovery_api.py -q

# 全部测试
.\.venv\Scripts\python.exe -m pytest -q

# 可复现证据（任务 ID／事件／工件哈希）
.\.venv\Scripts\python.exe -m scripts.run_task3_recovery_acceptance `
    --output-dir artifacts/task3-evidence

# 本地网站（人工验收）
Copy-Item .env.example .env    # 填 SHIP_LLM_* 后设 SHIP_LLM_ENABLED=true
.\.venv\Scripts\python.exe -m ui.web_server --host 127.0.0.1 --port 8765 --llm
```

重启验证（人工）：提交任务后在计算中按 `Ctrl+C` 结束服务 → 重新启动 → 刷新页面；
同一 `thread_id` 应继续到终态，`/api/runs/{id}/events` 不出现第二条 `answer_completed`。

## 五、本轮证据（`artifacts/task3-evidence/task3_recovery_evidence.json`）

模型标识：`task3-scripted`（确定性脚本模型，自动化验收用；**不访问网络**）。
工具链为真实实现：Tdata → Tseg → Tspeed（内部 Tenergy 批量候选预测）→ Tmanagement。

| 场景 | 任务 ID | 结果 |
|---|---|---|
| 计算中（模型调用）被杀 → 重启 | `482f7915-6443-40d5-b6bb-f3f120882682` | `running` → `completed`，`recovery_path=resume_from_checkpoint`，`answer_completed=1` |
| 运行中取消 | `0963c159-a4e4-4462-9853-72d1b008c08a` | `cancelled`／`task_cancelled`，`plan_verified=0`，无 `answer_ref` |
| 取消后重试 | 同上 | `completed`／`research_feasible`（从输入快照重跑），`answer_completed=1` |
| finish 重放 | `03591b91-edc3-4797-b184-5a53089b3c20` | 首次发布，重放返回空并记 `answer_reused`；工件哈希稳定 |

重启场景的工具事件（节选）：`Tdata ok → Tseg ok → Tenergy.candidate ×N（父调用 ID 关联 Tspeed）→ Tspeed ok → Tmanagement ok → plan_verified → answer_completed`；
工件引用含 `PowerDemandSeries`、`Tspeed`、`Tmanagement`、`plan_verification`、`answer`，均带 SHA-256，可由 `store.read()` 复算。

## 六、测试结果

- 本任务专项：**15 项通过**（`test_runtime_recovery.py` 9 + `test_runtime_recovery_api.py` 6）。
- 相关回归：`test_research_agent.py`、`test_web_frontend.py`、`test_research_workflow.py`、`test_frontend_state.py` 均通过。
- 全量结果见 PR 描述（附 `pytest -q` 输出）。
- **基线既有的失败**：`test_actual_lstm_prediction_and_handoff[lstm_short-ok]` 在 `codex/three-tools-review-20261015` 原样失败（`Tenergy interval energy differs from P * duration / 3600`），属任务 1（Tenergy）范围，本轮未改动该路径，也未把它计为本 PR 的修复成果。

## 七、仍存在的限制

1. **自动验收用的是脚本模型**：本机没有 DeepSeek 凭据，`task3-scripted` 不产生真实模型调用。联合验收要求的“至少一轮真实配置 DeepSeek API”需由项目负责人在有凭据的环境执行；命令见第四节，接口与恢复逻辑与模型无关。
2. **超时是协作式的**：工具内部长时间计算不会被强制打断，最坏情况是一个工具调用返回后才终止。要做到硬中断需要把工具调用放进可取消的执行单元，本轮未做。
3. **队列与执行器是单进程**：多 worker 部署（如多个 uvicorn 进程）需要共享队列与分布式领取；本轮只保证同一 SQLite 文件上的领取互斥。
4. **恢复只覆盖 LangGraph 任务**：旧版演示工作流（`workflow_runtime`）的中断语义未改动。
5. **取消后的重试从输入快照重跑**：会复用已完成的工具工件，但会重新走模型与工具，耗时与首次相当。
6. **72 组起终点覆盖属任务 2**（FDP／长航线），本 PR 不改变 Tspeed—FDP 交接逻辑。
7. 研究仿真与实船标定的边界不变：结果仍标 `synthetic_demo`，不得表述为实船安全或正式航行批准。
