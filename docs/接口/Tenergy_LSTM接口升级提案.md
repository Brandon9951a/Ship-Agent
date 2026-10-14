# Tenergy LSTM 接入联调接口 v2 的核查与迁移

本文件是 [五工具联调接口 v2](五工具联调接口_v2.md) 的 Tenergy 实施清单。`research-tools/2.0-draft` 是目标契约；现有 `tools/lstm_energy.py` 是独立实验 API，`tools/tenergy.py` 与 `schemas/types.py` 属于 v1 演示链。此处不把实验 API 命名为已经实现的 v2 接口。

## 已有实现与阻断原因

`evaluate(speed_plan, environment_scenario, history, model_version, *, bundle)` 支持 12 点有效历史、1–5 个约 5 秒移动时步，使用未来计划航速/加速度而不读取未来真实功率；返回右端积分的电机侧功率和能量。`batch_evaluate` 逐项调用，同输入可复现。Excel 六输入扩展需要有来源的逐时环境；共同字段模型训练过 CSV，CSV 单位仍只按任务书暂定。等待与辅助能耗、电池侧能耗均为 `null`。

模型清单 `models/tenergy-lstm-v5/manifest.json` 标记 `planner_approved=false`。按来源留出 MAE 为 1.029 kW，持续功率基线为 0.858 kW；第 6–10 步滚动误差升高。详见[构建与验收报告](../数据/Tenergy_LSTM构建与验收.md)。因此研究调用可返回 `status=experimental`；实验 API 显式传入 `usage="planning"` 时给 `model_unapproved`，正式 v2 入口还必须执行相同门槛。这与某个候选的航速、环境或预测时域越界（`model_out_of_domain`）不同。不得把 `experimental`、v1 `ToolResponse.ok` 或模型预测的非空数字翻译成 v2 可选候选。

## v1 实验输入到 v2 的显式映射

| 当前实验字段 | v2 字段或处理 | 未满足时 |
|---|---|---|
| `speed_plan[]` 的 `segment_id`, `speed_kmh`, `dt_seconds` | `EnergyBatchRequest.candidates[].trajectory[]` 的 `segment_id`, `planned_speed_kmh`, `duration_s`；由规划基准和累计时长生成 `start_offset_s`，指定 `phase=moving` | 时间边界不明、区间重叠或航段断裂为 `invalid_input` |
| 当前可选 `time` | 仅作实验追踪；Excel 为相对秒，CSV 日历时区未核实，不可直接填 v2 `planning_origin_at` | 绝对时刻未知时保持 `null`，不输出日历 ETA |
| `waiting_seconds` | 当前为每个移动时步附带的等待秒数，不是 v2 独立 `phase=waiting` 功率区间 | 必须由轨迹展开器另列等待区间；LSTM 对等待功率不提供预测 |
| `history`、`environment_scenario` | 有来源的 `history_ref`、`environment_scenario_ref`，锁定于 `snapshot_id`、`scenario_id`；历史结束不得晚于规划起点 | 缺字段/来源为 `needs_input` |
| `steps[]` 与 `segments[]` | 每个候选写独立 `PowerPoint` 工件与分段汇总，回填 `candidate_id`、工件 `sha256`、覆盖率、质量标记 | 缺时序/覆盖不足不得生成可供选择的候选 |
| `model_version` | `ToolRun.model_or_algorithm_version` 同时关联权重、归一化器、预处理与数据源哈希 | 版本不一致为 `invalid_input` 或 `tool_error`，不能复用旧缓存 |

适配器须提供 `evaluate_batch(EnergyBatchRequest)`，校验批内 `candidate_id` 唯一、输出按 ID 配对、逐条和批量一致，并把研究实验输出与正式可选择输出分开。对于本模型，正式模式在模型级门槛处停止；研究模式允许保存预测工件，但 `usage=research_only`。`batch_evaluate` 目前没有这些能力，不能直接作为 v2 `evaluate_batch` 使用。

## 交接门槛

1. Tenergy：补 v2 请求/信封、相对时间区间、候选 ID、逐候选诊断、不可变时序工件及哈希；将已有实验 API 的模型批准门槛接入 v2，并覆盖历史缺失、域外、5 步以上、等待和部分候选失败。用真实代表性 DE 种群测量候选数、时步数、批量耗时和内存；现有 100 个五步 CPU 调用仅是冒烟基准。
2. Tspeed：从 Tenergy 接收电机侧 `PowerPoint`，在独立的共同需求构建器中依据有来源参数做一次电机到母线转换和辅助合并；入选轨迹以核验网格重算。当前 LSTM 未获准时用解析或具名仿真夹具验证 DE，不把它当正式目标函数。
3. Tmanagement：只读取同一 `selected_demand_ref` 的母线需求；两组独立电池参数、FDP 和独立检查尚未实现，不能由现有合并 SOC 反推。
4. 三方与主图：冻结字段、枚举、时间/网格映射、状态优先级及合法/异常 JSON，再发布机器可读 `research-tools/2.0` schema。模型级基线复验、CSV 单位及电机测量边界核实、长时预测与母线转换依据未完成前，完整安全方案不可标为成功。
