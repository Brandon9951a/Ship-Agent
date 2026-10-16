# Tmanagement FDP 验收与实验记录

本记录覆盖研究仿真工具的正确性、失败语义、同口径策略对照和计算开销。
参数均为具名夹具假设；结果不代表实船运行、安全认证或运营批准。

## 样例结果

| 样例 | outcome_code | safe | 诊断 |
| --- | --- | --- | --- |
| success | ok | True | — |
| instantaneous_overload | infeasible_under_assumptions | False | instantaneous_power_exceeds_known_limit |
| soc_depletion | grid_unresolved | False | no_path_on_requested_grid |
| grid_budget | grid_unresolved | False | grid_unresolved |
| charging_unsupported | unsupported_charging | None | charging_not_supported |
| missing_efficiency | needs_input | None | missing_input, missing_input |

## 相同输入下的 FDP 与状态机对照

| 指标 | FDP | 状态机 | FDP − 状态机 |
| --- | ---: | ---: | ---: |
| 末端 SOC 1 | 0.7446 | 0.7502 | -0.0056 |
| 末端 SOC 2 | 0.7554 | 0.7498 | 0.0056 |
| 末端 SOC 差 | 0.0108 | 0.0003 | 0.0105 |
| 最大 SOC 差 | 0.0108 | 0.0056 | 0.0052 |
| 电池 1 功率变化总量 (kW) | 79.7213 | 49.7213 | 30.0000 |
| 电池 2 功率变化总量 (kW) | 80.2787 | 110.2787 | -30.0000 |
| 模式切换次数 | 2 | 2 | 0 |

| 成本分项 | FDP | 状态机 | FDP − 状态机 |
| --- | ---: | ---: | ---: |
| power | 0.5000 | 0.5000 | 0.0000 |
| switch | 2000.0000 | 2000.0000 | 0.0000 |
| soc_balance | 0.0081 | 0.0081 | 0.0000 |
| low_load_mode3 | 0.0000 | 0.0000 | 0.0000 |
| total | 2000.5081 | 2000.5081 | 0.0000 |

独立检查器约束违例数：FDP 0，状态机 0。
两列成本均按实际 SOC 回放复算；FDP 搜索用的保守网格成本另存于机器可读结果。离散网格与状态压缩可能使 FDP 成本高于状态机，此对照不构成全局最优证明。

## 自动测试

- python -B -m pytest -q -p no:cacheprovider tests/test_fdp_research.py
- 结果：15 passed in 0.14s，退出码 0。

## SOC 网格与权重敏感性

| SOC 步长 | 权重方案 | outcome | safe | 轨迹 | FDP 成本 | 状态峰值 | 转移数 | 耗时 ms | Python 峰值字节 |
| ---: | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| 0.1000 | paper_reference | ok | True | [1, 3, 3, 1] | 2000.5600 | 3 | 16 | 12.887 | 229396 |
| 0.1000 | half_soc_balance | ok | True | [1, 3, 3, 1] | 2000.5300 | 3 | 16 | 11.956 | 228519 |
| 0.1000 | double_soc_balance | ok | True | [1, 3, 3, 1] | 2000.6200 | 3 | 16 | 14.213 | 198497 |
| 0.0500 | paper_reference | ok | True | [1, 3, 3, 1] | 2000.5300 | 3 | 16 | 12.310 | 198554 |
| 0.0500 | half_soc_balance | ok | True | [1, 3, 3, 1] | 2000.5150 | 3 | 16 | 10.987 | 198554 |
| 0.0500 | double_soc_balance | ok | True | [1, 3, 3, 1] | 2000.5600 | 3 | 16 | 10.907 | 223191 |
| 0.0250 | paper_reference | ok | True | [1, 3, 3, 1] | 2000.5150 | 3 | 16 | 11.180 | 218930 |
| 0.0250 | half_soc_balance | ok | True | [1, 3, 3, 1] | 2000.5075 | 3 | 16 | 11.961 | 212282 |
| 0.0250 | double_soc_balance | ok | True | [1, 3, 3, 1] | 2000.5300 | 3 | 16 | 11.798 | 192143 |

## 24 小时代表性全程计算

- 区间数：48；规划时长：24.0 小时；SOC 网格步长：0.005。
- 结果：ok，safe=True；末端 SOC=[0.4687430966629175, 0.4719976440778232]。
- 峰值状态层：3；转移数：237；耗时：118.634 ms。
- Python 峰值分配：2297889 bytes；tracemalloc 不含本机原生分配。
- 独立检查：481/481 项通过。
- 该情景采用具名的仿真电池与需求参数，非实测全航程。

## 复现

从仓库根目录运行：

    python -m pytest -q tests/test_fdp_research.py
    python -m scripts.fdp_research_report --output-dir docs/验收

测量环境：Python 3.11.1；Windows-10-10.0.22631-SP0；CPU：not reported。

## 已知限制

- 接口仍标记为 research-tools/2.0-draft；现有三工具研究联调不等于正式五工具契约冻结。
- 网格状态取实际 SOC 的向下量化值；同格状态压缩只给出研究近似解，网格无路径不证明连续问题或实船全局无解。
- 首版不支持充电；负母线需求返回明确的不支持结果。
- 24 小时输入使用本工具内的具名仿真假设；不能用于声称实船 SOC、节能或安全。
- Python 峰值内存使用 tracemalloc 测得，不包含本机原生库分配。
