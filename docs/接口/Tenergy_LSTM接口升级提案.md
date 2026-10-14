# Tenergy LSTM 接入联调接口 v2 的核查与迁移

本文件记录按[个人完善要求](../团队资料/02_当前设计/工具任务书/工具构建完善要求.md)交付的 Tenergy v2 草案实现。入口在 `tools/tenergy_v2.py`，类型与输入校验在 `schemas/tenergy_v2.py`，对应[五工具联调接口 v2](五工具联调接口_v2.md)的 `research-tools/2.0-draft`。这是本工具的可运行接口建议；全项目采用版本及与其他 PR 的字段冲突由项目负责人裁决。`tools/lstm_energy.py` 继续作为底层实验预测 API，v1 演示主图尚未接入新入口。

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

适配器已提供 `TenergyV2.evaluate_batch(EnergyBatchRequest)` 和同名模块函数。`EnergyBatchRequest.from_dict` 拒绝未知信封字段、重复候选 ID、非法类型/单位时间表达；每个候选独立校验，输出按 ID 配对。整体 `ToolRun.outcome_code=ok` 在混合批中表示处理过程完整，每个候选仍须检查自身状态。研究结果始终为 `usage=research_only` 且 `selectable_for_planning=false`；正式模式在未批准清单处停止。底层列表式 `batch_evaluate` 不等同于此入口。

`JsonArtifactStore` 保存不可变的请求、历史、逐时功率和批量结果 JSON。`ArtifactRef` 包含 `artifact_id, sha256, row_count, schema_version`，只按内容摘要寻址；读取时复核哈希、类型、行数。`ToolRun` 的输入哈希关联完整请求与模型身份，模型身份包括权重、归一化器、清单、训练范围文件的哈希及预处理版本；输出还指向输入请求工件，能够重新解析调用依据。历史必须绑定同一 `snapshot_id`，环境绑定 `scenario_id`，历史最后一点在相对 0 秒且不含未来观测。

`inference_domain.json` 从冻结训练窗口导出航速/加速度及扩展特征的描述性范围，并与原模型文件绑定；它不表示模型准入通过。支持的移动前缀为 1–5 点、相邻 4–6 秒。等待/停靠及其后的区间不由本模型补功率；保留 null 和诊断，可返回等待前已预测前缀的覆盖电耗。覆盖率按已预测秒数/候选请求总秒数计算；只要覆盖不全，完整候选/航段电耗为 null，`covered_motor_energy_kwh` 仅表示已覆盖部分。电池侧、等待和辅助电耗一直为 null。

模型与工件不可用归 `tool_error`，输入非法归 `invalid_input`，缺必要历史/有来源环境及未知等待负载归 `needs_input`，超过训练特征/五步时域归 `model_out_of_domain`。正式模式对结构有效候选优先执行 `model_unapproved` 门槛；研究模式可单独检验候选域和缺参状态。布尔值不能冒充数值，未来真实功率字段不会进入预测张量。

机器可读夹具见 `configs/examples/tenergy_v2/`，实际输出见 `models/tenergy-lstm-v5/improvement/`。运行：

```powershell
.\.venv\Scripts\python.exe -m scripts.example_tenergy_v2
.\.venv\Scripts\python.exe -m scripts.example_tenergy_v2 --planning
```

## 项目负责人待裁决的问题

1. 本工具建议 `phase=moving/waiting/stopped`，工件来源为内嵌 `source_ref`，ToolRun 仅以 `payload_ref` 交付结果；请与其他 PR 的字段及序列化方式统一。当前结构是个人实现建议，没有宣布全项目冻结。
2. CSV 单位、主电机通道具体测量位置、电机到母线转换版本、等待及辅助负载来源仍待核实；本工具只预测电机侧，不选定共同转换参数。
3. 当前时域至多五步，50/500/5,000 候选性能只覆盖 5/15/25 秒的具名仿真输入；不能代表全航程 DE 负载。长航程模型、重采样算子及准入证据阈值请另行协调。
4. 其他工具 PR、主图接入、联调重搜与整体方案验收由项目负责人统筹。本轮只交付 Tenergy 输入输出、边界、复现证据与未决问题，详情见[个人完善验收报告](../数据/Tenergy_个人完善验收.md)。
