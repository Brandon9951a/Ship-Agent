# Tspeed（DE 航速优化）实现与验收记录

> 负责人：徐（Tspeed / DE）。分支：`feat/tspeed-de-v2`（基于 `main` b5a977b）。
> 依据：[Tspeed_DE航速优化工具_任务书.md](../团队资料/02_当前设计/工具任务书/Tspeed_DE航速优化工具_任务书.md)、[五工具联调接口_v2.md](../接口/五工具联调接口_v2.md)。
> 代码说明：[research/README.md](../../research/README.md)。

## 1. 交付内容

| 交付物 | 位置 |
|---|---|
| v2 机器可读契约（信封、能量批量、母线需求、航速优化结果） | `research/contracts/`、`research/schemas/*.schema.json` |
| DE 搜索引擎（DE/rand/1/bin、边界修复、精英保留、预算与迭代记录） | `research/tspeed/search.py` |
| 轨迹展开器（加减速上限、限速、等待、距离积分、部分斜坡求根） | `research/tspeed/trajectory.py` |
| 独立约束检查器（核验精度复算 + 重采样误差） | `research/tspeed/verification.py` |
| **共同需求构建器**（电机侧→母线侧唯一转换） | `research/demand/builder.py` |
| Tenergy 预测器契约 + 验收解析后端 + LSTM 适配层 | `research/tspeed/predictor.py`、`research/tspeed/lstm_adapter.py` |
| v1 → v2 显式迁移层 | `research/migration/v1_to_v2.py` |
| 合法/非法样例 JSON（含反例说明） | `research/schemas/examples/` |
| 验收测试 | `research/tests/`（见第 3 节） |
| 验收实验脚本与报告 | `research/scripts/run_tspeed_experiments.py` |

演示链路（`schemas/` + `tools/`，v1）未改动；任务书要求"缺逐时功率与双电池状态时返回不支持，不反向推断"，由迁移层承担。

## 2. 必做项对照

| 任务书要求 | 实现 | 证据 |
|---|---|---|
| 独立目标函数与约束检查器分离 | `objective.py` 只算 `J`；`verification.py` 独立复核 | 测试 `test_de_search`、`test_tool_entry` |
| DE 在合法边界初始化、修复越界、保存种子 | `search.effective_bounds` / `_repair` / `DEConfig.seeds` | `test_reproducible_with_same_seed`、`test_repair_rule_keeps_solutions_in_bounds` |
| 硬约束可行优先，不靠罚值判定安全 | `_better` 可行优先；选定方案必须全部检查通过 | `test_selected_result_rejects_failed_checks` |
| 批量提交种群、缓存预测、记录耗时 | 逐候选批量调用 + 评估缓存 + `runtime_ms` | `test_cache_is_used_across_generations`、第 4 节耗时 |
| 滚动重规划（当前位置/真实速度/剩余航段） | 请求裁剪 + `current_speed_kmh` 作为首段入口 | `test_rolling_replan_does_not_touch_past_segments`、第 4 节 |
| 诊断输出（迭代轨迹、可行比例、违反统计） | `SearchTrace` | `test_violation_statistics_are_recorded` |
| 与 Tmanagement 的"筛选—FDP 复核—必要时重搜" | `apply_management_feedback`：可证明上限转硬约束，SOC 路径失败只作诊断 | `test_provable_power_limit_becomes_hard_constraint`、`test_soc_path_feedback_is_not_a_hard_constraint` |
| 单一转换位置（Tspeed 负责人交付） | `research/demand/builder.py` | `test_energy_conservation_passes_for_constant_efficiency` |
| 缺转换依据时 `demand_bus_kw=null` | `motor_side_only_spec` + 结果如实标注 | `test_missing_conversion_keeps_bus_side_null`、`test_motor_only_scenario_is_labelled_and_not_claimed_safe` |
| v2 机器可读 schema 与四类样例 | `research/schemas/` + 成功/未解决/域外/反馈样例 | `test_samples_and_schemas` |
| 旧接口显式迁移层 | `research/migration/v1_to_v2.py` | `test_migration_v1`（分段平均不逆推逐时、合并 SOC 不拆两组） |

## 3. 测试结果

运行方式（两套均可）：

```powershell
python -m research.tests.run_tests        # 自带运行器，无需 pytest
python -m pytest research/tests -q        # 如已安装 pytest
```

结果：**117 项通过、0 项失败**（`research/` 全部模块）。覆盖：

| 模块 | 项数 | 重点 |
|---|---|---|
| `test_contracts` | 21 | 字段边界、失败语义映射、序列化、时间基准缺失不得给 ETA |
| `test_trajectory` | 18 | 距离/时间一致、加减速上限、固定航段、等待、过短航段如实报告 |
| `test_de_search` | 18 | 可复现性、与精确最优对照、约束场景、预算、缓存、精英保留 |
| `test_demand_builder` | 13 | 单一转换、null 传播、能量守恒、来源强制 |
| `test_samples_and_schemas` | 11 | 合法样例自洽、非法样例确被实现拒绝 |
| `test_prediction` | 10 | 剖面/工件两条路径数值一致、批量与逐条一致、缓存键 |
| `test_tool_entry` | 12 | 结果工件、失败语义、FDP 反馈、重采样误差上报 |
| `test_lstm_adapter` | 8 | 结构校验、候选配对、能量口径、部分候选失败隔离 |
| `test_migration_v1` | 6 | 不补齐缺失字段、不升级为证明性分类 |

### 关键正确性证据：与精确最优对照

在本项目的轨迹模型下，目标函数可精确分解（单航段能量只取决于"入口速度、目标航速、出口速度"三元组），因此可用 **DP 精确求解**并**穷举校验 DP**：

* DP 与穷举在相同网格上结果完全一致（`exact_grid_optimum(verify_against_exhaustive=True)` 每次自检）；
* DP 的目标值与按展开器复算的同一向量在机器精度上一致（`|Δ| < 1e-6` 断言）；
* DE 与精确最优的相对偏差见第 4 节实测数字。

## 4. 自测实测（非正式验收）

> 说明：本轮**未做正式验收**（由负责人决定取消）。以下数字是本机自测结果，用于说明
> 实现是否按预期工作、以及还差什么；**不构成验收结论**，也不能作为对外效果数据。
> 原始 JSON（本机生成，不入库）：`artifacts/tspeed-v2/tspeed_acceptance.json`。

运行方式：

```powershell
python -m research.scripts.run_tspeed_experiments --output-dir artifacts/tspeed-v2 `
    --population 40 --generations 60
```

场景：豫交投001 研究情景，17 航段（12 段可优化、5 段固定航速），81.1 km。

| 项目 | 结果 |
|---|---|
| 与精确最优对照（λ=0） | 精确 39.4255 / DE 39.4378，偏差 **+0.031%** |
| 与精确最优对照（λ=10） | 精确 42.0429 / DE 42.0138，偏差 **−0.069%** |
| 完整 17 航段求解 | `candidate_found`，能量 144.72 kWh，历时 14.75 h，2440 次评估，**92.7 s**（38.0 ms/次） |
| 独立检查 | 速度、加速度、连续性、距离、时间、能量积分、母线能量守恒 **7/7 通过** |
| 多随机种子 | 5/5 找到可行解，目标值 153.752–156.538，相对离散度 **1.81%**，单次 17–18 s |
| 时间过紧 | `search_unresolved`（诊断 `arrival_deadline_missed`），不返回选定候选 |
| 母线功率上限过紧 | `search_unresolved`，不返回选定候选 |
| 缺转换依据 | `ok` / `candidate_found`，`bus_side_available=false`，仅电机侧口径 |
| 滚动重规划（剩 9 段、37.4 km、真实当前速度 7.5 km/h） | `search_unresolved`（见第 5 节第 7 条，待查） |
| 论文配置耗时外推（50×500＝25050 次评估） | 按实测 34.9 ms/次 ≈ **0.24 h** |

### 4.1 必须写明的负面结果：DE 未跑赢规则基线

同一能量口径、同一硬约束下，λ=0 的 DE 与"全取各段限速下限"的规则基线比较：

| 方案 | 目标值 | 电机侧能量 |
|---|---|---|
| 规则基线 `all_min_speed` | 149.75 | 142.47 kWh |
| DE（λ=0，纯最小能耗） | 151.39 | 151.39 kWh |
| DE（λ=10） | 153.75 | 150.81 kWh |
| 规则基线 `all_mid_speed` | 333.64 | 319.90 kWh |
| 规则基线 `all_max_speed` | 593.62 | 570.44 kWh |

**DE 比规则基线差约 1.1%。** 这不能当成"DE 效果"来汇报，也不能用"模型内差异"糊过去。
两种可能，必须在下一轮定位：

1. **收敛缺陷**：规则解落在 DE 的有效边界内、目标值更优，但 DE 在 24×20 的预算下没找到它
   ——那说明预算/参数或边界修复策略不足；
2. **口径不一致**：基线向量经展开器时被限速夹住（其"下限"未必等于展开后的实际速度），
   使基线的实际能量比名义值更低——那说明基线对照本身不公平，需要统一评价口径。

在查清之前，本工具**不主张任何节能率**，第 4 节其余数字只说明"链路与约束检查按预期工作"。

### 4.2 性能与内存

修掉"缓存每个候选全部逐时工件"的缺陷后（第 5 节第 8 条）：

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 单次评估 | 251 ms | **38 ms** |
| 完整 17 航段（40×60） | 613 s | **92.7 s** |
| 峰值内存 | 约 10.4 GB | 约 0.6 GB |

## 5. 过程中发现并修掉的实现缺陷

实现过程中由"独立复核"暴露的问题（均已修复并有回归测试）：

1. **等待阶段漏算**：等待区间只进入核验路径、未进入 DE 判分路径，导致辅助能耗在搜索期被漏算。修复为单一来源 `ExpandedTrajectory.ordered_intervals`。
2. **决策向量无法复原**：斜坡区间的平均速度被当作目标航速，核验时复原出错误向量。修复为在 v2 工件中显式增加 `target_speed_kmh`（决策变量）与 `interval_kind`。
3. **链条入口速度取错**：展开器用"上一段目标航速"而非"上一段实际出口速度"作为下一段入口，导致重复减速、漏掉加速。
4. **相邻限速衔接**：DE 搜索域未考虑"边界速度须同时满足两侧限速"，大量候选注定不可行（实测 172/180 次违规）。修复为 `effective_bounds` 求交。
5. **加减速上限误判**：复核按"存储符号"选择上限，且采样把残余时间摊薄放大加速度，导致减速段被拿加速上限判定（0.2 > 0.15）。修复为按区间类型选择上限 + 残余并入最后一片。
6. **巡航下限**：固定航段的巡航速度必须等于其固定航速，但过渡斜坡允许在限速区间内取值——原先两者混在一个判据里。
7. **后段限速被前置**：一度把"下一段的限速下限"套到当前段出口，会把"前段低速通过、后段再加速"的合法方案（以及规则基线解）误判为不可行。已按物理正确口径改回：段下限只约束本段巡航，入段斜坡负责过渡。
8. **无界缓存**：预测器与评估上下文长期驻留每个候选的上万个逐时工件，17 航段实测峰值 10.4 GB。改为有界缓存 + 搜索期只保留汇总与诊断、逐时工件按需重建（性能同时提升 6.6 倍，见 4.2）。

仍有**未定位**的问题：4.1 的"DE 未跑赢规则基线"与"滚动重规划 `search_unresolved`"（剩余 9 段、37.4 km、期限按剩余航程折算为 22.4 h，仍未找到可行解，需检查剩余段的限速衔接与固定航段处理）。

## 6. 证据边界与待办

**证据边界**

* 验收使用解析功率函数 `P = c·v³`（c=0.055 kW/(km/h)³，10 km/h 约 55 kW）；母线侧效率 0.94、辅助负载 30 kW 是**具名研究假设**，沿演示配置量级。以上均**不是实船标定**，不能作为节能率或 SOC 安全证据。
* 未预先填入导师论文的任何精度或效果数值。
* 未取得转换依据时母线侧为 `null`，结果中如实标注"仅为电机侧对照实验，不构成 SOC 安全结论"。

**待办（需与两位负责人联调后冻结）**

| 待办 | 依赖 |
|---|---|
| **定位"DE 未跑赢规则基线"（第 4.1 节）**：先判断是收敛不足还是基线口径不公平 | 本工具内部，优先 |
| 定位滚动重规划在剩余 9 段上的 `search_unresolved` | 本工具内部 |
| 用罗的 LSTM 替换解析后端，实测批量推理耗时 | Tenergy 冻结 `evaluate_batch` |
| 冻结推理/规划步长、长时滚动预测策略、DE 种群与预算、`λ` 与惩罚尺度、数值容差、模型适用域 | 三人联调 |
| 双电池预算（两组独立可供功率与 SOC 演化） | Tmanagement 的 FDP 与两组电池参数 |
| 补能试算（充电状态转移） | 设施与充电模型来源，当前明确返回不支持 |

**性能与资源（已改善）**：修复无界缓存后，17 航段 40×60 实测 92.7 s、峰值内存约 0.6 GB（修复前 613 s / 10.4 GB）。是否满足 1800 s 滚动周期仍需在 LSTM 接入后按实测耗时判断，本轮不作结论。

## 7. 下一步（向两位负责人）

* **罗（Tenergy）**：请确认 `EnergyBatchRequest/Result` 字段与 `target_speed_kmh` 的加入；我用 `LSTMEnergyAdapter` 对接，先跑 `consistency_report` 核验批量/逐条一致与能量积分口径，再替换解析后端。
* **陆（Tmanagement）**：`PowerDemandSeries` 已按 v2 第 5 节交付（含配置哈希与能量守恒检查），请用同一 `selected_demand_ref` 复算两组功率与 SOC，并确认 `ManagementFeedback` 的 `failure_scope` 取值与"可证明上限"的判定条件。
* **主图集成者**：`ToolRun` 信封与 `SpeedOptimizationResult` 可直接消费；`arrival_at` 在无绝对基准时为 `null`（不生成日历 ETA）。
