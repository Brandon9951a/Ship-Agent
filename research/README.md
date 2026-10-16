# Tspeed（DE 航速优化）v2 实现说明

> 负责人：徐（Tspeed / DE）。对应任务书：[Tspeed_DE航速优化工具_任务书.md](../../docs/团队资料/02_当前设计/工具任务书/Tspeed_DE航速优化工具_任务书.md)；
> 字段与失败语义：[五工具联调接口_v2.md](../../docs/接口/五工具联调接口_v2.md)。
> 状态：`research-tools/2.0-draft` 目标契约的第一版实现；演示链路（`schemas/` + `tools/`，v1）保持不变。

## 1. 定位与边界

Tspeed 在真实约束下搜索各航段目标航速，向主智能体返回**候选与诊断**，不生成自然语言方案。求解算法为差分进化（DE/rand/1/bin）；每个候选的推进功率与电耗由 Tenergy 批量预测提供；最终选定方案的功率时序交给 Tmanagement。

已实现与未实现的边界：

| 能力 | 状态 |
|---|---|
| 轨迹展开（加减速上限、限速、等待、距离积分） | 已实现 |
| DE/rand/1/bin 搜索 + 可行优先排序 + 精英保留 | 已实现 |
| 独立约束检查器与核验精度复算 | 已实现 |
| 电机侧 → 母线侧**唯一**转换（共同需求构建器） | 已实现 |
| 与 Tenergy 的批量预测契约 + 解析后端 + LSTM 适配器 | 契约与适配器已实现；正式 LSTM 权重待罗冻结 |
| 滚动重规划（请求裁剪 + 真实当前速度） | 已实现 |
| `ManagementFeedback` 消费与有界重搜 | 反馈消费已实现（可证明上限 → 硬约束；SOC 路径失败 → 诊断） |
| 双电池预算（两组独立可供功率与 SOC 演化） | **未实现**（属 Tmanagement/FDP）；Tspeed 只做单一预算筛选 |
| 目标类型 | `min_energy` 已实现；`min_time` 按最短期望实现并如实标注；其他目标返回 `not_supported` |

## 2. 代码结构

```text
research/
├── contracts/                 # v2 机器可读契约（仅标准库）
│   ├── envelope.py            # SourceRef / ArtifactRef / 质量标记 / 稳定哈希
│   ├── diagnostics.py         # Diagnostic（stage/code/区间/字段/实测/限值/来源/可否重试）
│   ├── interfaces.py          # ToolRun 信封 + ExecutionStatus / OutcomeCode / SearchStatus
│   ├── energy_batch.py        # TrajectoryPoint / CandidateTrajectory / EnergyBatchRequest|Result
│   ├── power_demand.py        # PowerDemandSeries（唯一母线需求工件）
│   └── speed_optimization.py  # SpeedOptimizationRequest|Result / ManagementFeedback
├── demand/builder.py          # 共同需求构建器（Tspeed 负责人交付）
├── tspeed/
│   ├── trajectory.py          # 决策向量 → 连续轨迹（部分斜坡/巡航/减速 + 采样）
│   ├── profile.py             # 搜索期轻量剖面（避免逐点上万对象）
│   ├── predictor.py           # EnergyPredictor 协议 + 验收用解析后端
│   ├── lstm_adapter.py        # 正式 LSTM 的薄适配层 + 结构校验 + 一致性自检
│   ├── objective.py           # J = E + λ·Σ(v_i−v_{i+1})²（含量纲处理）
│   ├── eval.py                # 候选评估：展开 → 批量预测 → 需求 → 硬约束 → 目标值
│   ├── search.py              # DE/rand/1/bin、边界修复、迭代记录、预算
│   ├── verification.py        # 独立约束检查器（核验精度复算 + 重采样误差）
│   └── tool.py                # 公开入口：请求 → 搜索 → 核验 → v2 结果
├── migration/v1_to_v2.py      # v1 → v2 显式迁移层（不补齐缺失字段）
├── tests/                     # 98 项验收测试（可用 pytest 或自带运行器）
└── scripts/run_tspeed_experiments.py   # 验收实验与报告生成
```

## 3. 三个关键设计决定

### 3.1 决策向量与轨迹的关系

决策变量是**每个可优化航段的目标巡航航速** `v_i`（内部 km/h）。固定航速航段（港口、船闸）不进入决策向量。展开规则：

1. 每段依次生成 `加速斜坡 → 巡航 → 减速斜坡`；斜坡时长由加减速上限决定；
2. 段间衔接速度 = 下一段目标航速（因此本段出口速度就是下一段入口速度）；
3. 若航段距离不足以完成完整斜坡，求**恰好覆盖该距离的峰值航速**（解析求根），而不是让速度瞬时跳变；
4. 峰值仍达不到限速下限时，如实报告 `distance_too_short_for_speed_limit`，不造假速度；
5. 等待时间由 Tseg 的规则显式给出，单列计入总时长，等待阶段主推进功率按模型规则记 0。

由于"一个航段展开为多个区间"，v2 工件里同时记录 `planned_speed_kmh`（区间平均速度，斜坡上不等于目标航速）与 `target_speed_kmh`（决策变量）。核验阶段据此精确复原决策向量——这是本项目新增的字段，已写入契约文档说明。

### 3.2 判分路径与核验路径的一致性

DE 每代要评估数十个候选，若每次都把上万个区间实例化为 v2 对象，构造与哈希开销会远超数值计算。因此：

* 搜索期使用 `TrajectoryProfile`（标量列）调用 `predict_profile`；
* 核验期使用完整 v2 工件调用 `predict`；
* 两条路径共用同一个后端实现（`AnalyticPowerPredictor.predict_profile`），并有测试断言两者数值完全一致；
* 轨迹 → 区间的转换只有一个来源（`ExpandedTrajectory.ordered_intervals`），等待阶段同时出现在两条路径上（曾因此修掉一个漏算辅助能耗的缺陷）。

### 3.3 目标函数与量纲

`J = E_pred + λ·Σ(v_i−v_{i+1})²`。`Σ(Δv)²` 的量纲是 (km/h)²，不能直接与 kWh 相加，因此先除以边界数（取平均速度差平方）再乘以尺度系数 `scale`。`λ` 与 `scale` 都是研究参数，须由敏感性实验冻结；导师论文的 `λ=10` 只用于复现实验，代码不预设为默认值。`E_pred` 的口径由 `objective_terms.energy_scope` 如实标注为 `motor` 或 `bus`。

## 4. 失败语义

| 情形 | `outcome_code` | `search_status` | 说明 |
|---|---|---|---|
| 找到可行候选且独立核验通过 | `ok` | `candidate_found` | 仍不等于完整方案安全（FDP 未过时尤其如此） |
| DE 在预算内未找到可行候选 | `search_unresolved` | `search_unresolved` | 附预算、违反约束统计与诊断候选；**不表述为数学无解** |
| 关键约束/参数缺失 | `needs_input` | `input_invalid` | 不用默认值制造可行方案 |
| 字段类型、范围、时序非法 | `invalid_input` | `input_invalid` | |
| 候选明显超出模型适用域 | `model_out_of_domain` | `model_out_of_domain` | 由 Tenergy 侧标记驱动 |
| 未实现的目标类型或补能试算 | `not_supported` | `input_invalid` | 不悄悄改用另一目标 |
| 运行异常、预测器故障 | `tool_error` | `tool_error` | 有界重试 |

`execution_status` 只区分 `completed / failed / cancelled`；`needs_input`、`invalid_input`、`search_unresolved`、`not_supported` 都是**工具按约定正常返回的工程结果**，因此为 `completed`。

## 5. 复现命令

```powershell
# 全量验收测试（不需要 pytest）
python -m research.tests.run_tests

# 也可用 pytest
python -m pytest research/tests -q

# 验收实验（生成 artifacts/tspeed-v2/tspeed_acceptance.json）
python -m research.scripts.run_tspeed_experiments --output-dir artifacts/tspeed-v2 `
    --population 40 --generations 60
```

实验输出包含：小规模与精确 DP 的对照、完整 17 航段求解、多随机种子稳定性、约束场景、滚动重规划、论文配置耗时外推、规则航速基线对照。实测结果见 [Tspeed_DE_实现与验收记录.md](../../docs/协作/Tspeed_DE_实现与验收记录.md)。

## 6. 协作接口

| 对象 | 我的输入 | 我交给对方 |
|---|---|---|
| Tenergy（罗） | `EnergyBatchRequest`（候选轨迹含目标航速、历史引用、环境情景、模型版本、积分规则） | `EnergyBatchResult` 消费契约、`LSTMEnergyAdapter` 结构校验、`consistency_report` 一致性自检 |
| Tmanagement（陆） | `ManagementFeedback`（失败区间、需求/可供功率、两组 SOC、上限来源、建议动作） | `PowerDemandSeries`（唯一母线需求工件 + 配置哈希 + 输入工件 ID + 能量守恒检查） |
| Tdata / Tseg | 两组独立电池参数、当前位置/速度、逐段限速与等待规则的来源 | 缺来源时的缺参报告（不使用演示默认值） |
| 主图集成者 | 请求、快照、情景与方案版本关联 | `ToolRun` 信封、`SpeedOptimizationResult`、搜索轨迹与调用父子 ID |

**待冻结参数**（须由实测/实验确定，代码不填默认值）：推理与规划步长、长时滚动预测策略、DE 种群与预算、`λ` 与惩罚尺度、效率/辅助负载采用值、数值容差、模型适用域。

## 7. 证据边界

* 验收实验使用**解析功率函数** `P=c·v³`，只用于验证算法正确性、约束一致性与耗时量级；它不代表实船标定，也不能当作节能率证据。
* 母线侧效率（0.94）与辅助负载（30 kW）在验收情景中是**具名研究假设**，沿演示配置量级；有来源的采用值须由 Tdata 提供后才能替换。
* 未取得转换依据时 `demand_bus_kw = null`，只输出电机侧对照实验，结果中如实标注"不构成 SOC 安全结论"。
* 全部测试与实验均**不预先填入**导师论文的精度或效果数值。
