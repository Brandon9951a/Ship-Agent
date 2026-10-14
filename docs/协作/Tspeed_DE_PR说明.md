# Tspeed（DE 航速优化）v2 实现 — Pull Request

## 这个 PR 做什么

按《Tspeed_DE航速优化工具_任务书》与《五工具联调接口_v2.md》，为徐（Tspeed/DE 负责人）交付差分进化航速优化工具的**第一版 v2 实现**：契约、DE 内核、独立约束检查器、以及任务书指定由 Tspeed 负责人负责的**共同需求构建器**（电机侧→母线侧的唯一转换位置）。

演示链路（`schemas/` + `tools/`，v1）**未改动**；v1 结果通过显式迁移层进入 v2，不反向推断逐时功率或两组电池状态。

## 改动范围

```
research/                       新增（独立研究系统模块，仅依赖标准库）
├── contracts/                  v2 机器可读契约
├── demand/builder.py           共同需求构建器（Tspeed 负责人交付物）
├── tspeed/                     轨迹展开 / DE / 预测器契约 / 独立核验 / 工具入口
├── migration/v1_to_v2.py       v1 → v2 显式迁移层
├── schemas/                    JSON Schema + 合法/非法样例
├── tests/                      验收测试（106 项）
└── scripts/                    验收实验与样例生成
docs/协作/Tspeed_DE_实现与验收记录.md   新增（含实测数字与证据边界）
.gitignore                      忽略本机沙箱权限诊断输出
```

未修改任何 v1 文件，因此不影响 orchestrator、report、value_lock 与 D3–D6 既有测试。

## 关键设计

1. **决策向量 vs 轨迹**：决策变量是每段目标巡航航速；v2 工件同时记录 `planned_speed_kmh`（区间平均速度）与 `target_speed_kmh`（决策变量），核验阶段据此精确复原向量（斜坡平均速度不能反推目标航速）。
2. **判分路径与核验路径一致**：搜索期走轻量剖面（避免每代构造上万对象），核验期走完整 v2 工件，两条路径共用同一后端实现并有数值一致性断言。
3. **目标函数与约束检查器分离**：`J = E + λ·Σ(Δv)²` 做量纲处理后单独计算；所有 `feasible` 结论来自独立检查器，选定方案必须全部检查通过。
4. **单一转换位置**：电机侧→母线侧换算与辅助负载合并只在 `demand/builder.py` 执行一次，输出带配置哈希与能量守恒检查的 `PowerDemandSeries`，供 Tmanagement 读取同一工件。
5. **缺依据如实降级**：未取得转换依据时 `demand_bus_kw = null`，只输出电机侧对照实验并标注"不构成 SOC 安全结论"；未实现的目标类型返回 `not_supported`，不悄悄改用另一目标。

## 验证

```powershell
python -m research.tests.run_tests          # 106 passed / 0 failed
python -m research.scripts.run_tspeed_experiments --output-dir artifacts/tspeed-v2
python -m research.scripts.make_samples
```

* 与**精确最优**对照：目标函数在本项目轨迹模型下可精确分解，DP 求精确最优并每次用穷举自检；DE 与其偏差见验收记录（λ=0 时 0.03% 量级）。
* 完整 17 航段：选定方案通过全部 7 项独立检查（速度/加速度/连续性/距离/时间/能量积分/母线能量守恒）。
* 多随机种子 5/5 找到可行解，目标值相对离散度约 1.8%。
* 约束场景：时间过紧与母线功率上限过紧均返回 `search_unresolved` 且不返回选定候选。

## 实现过程中修掉的缺陷（均由独立复核暴露）

等待阶段漏算辅助能耗、决策向量无法复原、链条入口速度取错、相邻限速衔接未进搜索域、加减速上限误判、巡航下限与固定航速混用判据。详见验收记录第 5 节。

## 待联调（不阻塞本 PR）

* **罗（Tenergy）**：冻结 `evaluate_batch` 后替换解析后端；`LSTMEnergyAdapter` + `consistency_report` 已就绪。
* **陆（Tmanagement）**：用同一 `selected_demand_ref` 复算两组功率/SOC；确认 `ManagementFeedback.failure_scope` 取值。
* **共同冻结参数**：推理/规划步长、长时滚动策略、DE 种群与预算、`λ` 与惩罚尺度、效率与辅助负载采用值、数值容差、模型适用域。

## 已知限制

* 验收使用解析功率函数与具名研究假设（效率 0.94、辅助 30 kW），**不是实船标定**，未预先填入论文效果数值。
* 双电池预算与补能试算未实现（属 FDP 扩展），当前明确返回不支持。
* 预测器缓存完整逐时工件，17 航段 × 40 种群 × 60 代实测约 8 GB 内存，作为在线规划器需改为按需重建；本轮不宣称满足 1800s 实时周期。
