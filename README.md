# 绿航智算：船舶能效管理智能体

绿航智算面向纯电动内河船舶的航行辅助决策场景。系统接收自然语言或结构化航次任务，依次完成数据准备、航段划分、能耗估算、航速优化和能量管理，并输出可追溯的航速、ETA、能耗与 SOC 结果。

当前版本用于软件演示和方法验证。工程数值来自确定性 Python 工具，大语言模型只参与任务理解确认和文字说明。所有结果均标记为 `synthetic_demo`，不代表实船安全认证、航道许可或运营批准。

## 核心流程

```text
任务输入
  ↓
parse → Tdata → Tseg → Tenergy → Tspeed → Tmanagement → finalize
                                      │
                                      └─ 不可行 → 生成已验证选项
                                                   ↓
                                             interrupt 等待选择
                                                   ↓
                                      checkpoint 恢复并完整重算
```

五个计算工具的职责如下：

| 工具 | 职责 |
|---|---|
| `Tdata` | 读取航线、船舶和任务数据，记录来源、缺失与冲突 |
| `Tseg` | 按起终点、港闸和航线顺序生成连续航段 |
| `Tenergy` | 估算候选航速对应的推进功率与航段能耗 |
| `Tspeed` | 在离散候选网格内检查时间、功率和 SOC 约束 |
| `Tmanagement` | 计算 SOC 轨迹、能量预算、告警和补能需求 |

当任务不可行时，后端只返回经过同一工具链验证的调整选项。船员选择后，LangGraph 使用 checkpoint 从中断点恢复。系统最多执行两轮调整重算；放弃任务不会触发新的计算轮次。

## 主要功能

- 中文自然语言与结构化航次输入。
- LangGraph 状态编排、条件路由和人工确认中断。
- SQLite 本地 checkpoint 与 PostgreSQL 云端 checkpoint。
- 页面刷新或服务重启后的待确认任务恢复。
- 服务端选项校验、决策版本校验和单线程恢复锁。
- 五工具完整轨迹、轮次记录和最终数值锁定。
- 三维船舶航段回放、航速、能耗、ETA 与 SOC 展示。
- 科大讯飞流式语音输入，凭据只保存在服务端。
- DeepSeek 定性理解与说明；服务不可用时自动使用本地模板。
- 正常、缺参、时间不可行、低 SOC、工具失败和恢复重算测试。

## 项目结构

```text
.
├── core/          # LangGraph 编排、运行时、解析、报告和数值锁定
├── tools/         # 五个确定性计算工具及共享计算函数
├── schemas/       # 请求、工具响应和航行方案数据结构
├── configs/       # 船舶、航线、约束和演示策略
├── ui/            # FastAPI 服务和驾驶舱前端
├── scripts/       # 数据检查、验证、审计和工程打包
├── tests/         # 自动化测试
├── docs/          # 架构、接口、算法、数据和验收记录
├── main.py        # 命令行入口
├── render.yaml    # Render Blueprint
└── requirements.txt
```

## 安装

要求 Python 3.10 或以上。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

真实密钥只填写在本机 `.env` 或部署平台的环境变量中，不提交到仓库。

## 本地运行

Windows 可直接双击 `start_local_demo.cmd`，也可以手动启动：

```powershell
.\.venv\Scripts\python.exe -m ui.web_server --host 127.0.0.1 --port 8765 --llm
```

访问 `http://127.0.0.1:8765`。模型配置缺失或调用失败时，五工具计算仍可运行。

命令行调用：

```powershell
.\.venv\Scripts\python.exe main.py "从平顶山港到军李船闸，2026-09-25 09:00出发，SOC85%，半载，6小时内到达"
```

## Web API

### 新建任务

```http
POST /api/run
```

```json
{
  "task_text": "从平顶山港到军李船闸，SOC85%，半载，6小时内到达"
}
```

不可行任务返回 `status=awaiting_choice`、`thread_id`、`decision_id` 和服务端已验证选项，不返回成功计划。

### 恢复任务

```http
POST /api/resume
```

```json
{
  "thread_id": "UUID",
  "decision_id": "UUID:0",
  "option_id": "accept_late"
}
```

客户端不能提交修改后的工程参数。后端从 checkpoint 中读取对应选项并恢复工作流。

### 查询任务

```http
GET /api/runs/{thread_id}
```

该接口用于刷新页面后恢复当前任务，不提供任务列表。

### 健康检查

```http
GET /healthz
```

健康结果包含模型模式、checkpoint 后端、checkpoint 就绪状态和语音服务状态。

## 测试与复现

```powershell
# 全量测试
.\.venv\Scripts\python.exe -m pytest -q

# 三场景验证
.\.venv\Scripts\python.exe -m scripts.validate_d4 --output-dir artifacts/d4-run

# 40 项批量实验
.\.venv\Scripts\python.exe -m scripts.validate_d5 --output-dir artifacts/d5-run

# 冻结实验与当前代码一致性审计
.\.venv\Scripts\python.exe -m scripts.audit_d6

# 生成比赛工程资料包
.\.venv\Scripts\python.exe -m scripts.package_release
```

打包程序只收录白名单中的源码、配置样例、测试、必要技术文档和冻结实验；自动排除 `.env`、数据库、缓存、Git 历史、原始调研材料和本机路径，并执行敏感信息扫描。

## Render 部署

仓库根目录的 `render.yaml` 同时定义 Web Service 和同区域 PostgreSQL。通过 Render Blueprint 部署后，需要在 Web Service 的 Environment 页面配置：

```text
SHIP_LLM_API_KEY
XFYUN_APPID
XFYUN_API_KEY
XFYUN_API_SECRET
```

`SHIP_CHECKPOINT_DATABASE_URL` 由 Blueprint 从 PostgreSQL 自动注入，不要手工填写。

线上健康检查应满足：

```json
{
  "status": "ok",
  "checkpoint_backend": "postgres",
  "checkpoint_ready": true,
  "speech_mode": "iflytek_iat"
}
```

## 验证结论与边界

- 40 项批量软件实验均通过独立有限网格复核，其中 32 项可行、8 项为 SOC 不可行。
- 同一约束下，最低能耗策略与最快可行基线的模型内汇总差异为 22.5220%。该数字只描述当前演示模型和离散速度网格，不是实船节能率。
- 当前演示有效容量为 1567.85 kWh，规划 SOC 下限为 20%，预警线为 25%，辅助功率为 30 kW。以上均为软件演示配置。
- 逐段真实限速、实船充电功率、传感器字段协议、BMS 硬件保护值和实船模型标定仍需现场确认。

详细说明见：

- [系统架构](docs/architecture.md)
- [五工具接口契约](docs/接口/五工具接口契约.md)
- [模型接入与回退约定](docs/接口/模型接入与回退约定.md)
- [字段字典](docs/数据/字段字典.md)
- [能耗基线与验证设计](docs/算法/能耗基线与验证设计.md)
- [最新开发与验收记录](docs/协作/最新开发与验收记录.md)
- [工程交付说明](docs/工程交付说明.md)

## 数据与安全

- 仓库和工程包不包含真实 API 密钥。
- 浏览器不会收到模型或语音服务凭据。
- 原始船舶资料、历史航行数据和展示素材不进入公开工程包。
- 所有外部数据、算法和第三方组件应按来源与许可使用。
- 软件输出仅提供辅助决策信息，不直接控制实船设备。
