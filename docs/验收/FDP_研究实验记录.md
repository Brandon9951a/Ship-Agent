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
| 末端 SOC 1 | 0.7459 | 0.7502 | -0.0043 |
| 末端 SOC 2 | 0.7541 | 0.7498 | 0.0043 |
| 末端 SOC 差 | 0.0082 | 0.0003 | 0.0079 |
| 最大 SOC 差 | 0.0082 | 0.0056 | 0.0027 |
| 电池 1 功率变化总量 (kW) | 77.4194 | 49.7213 | 27.6981 |
| 电池 2 功率变化总量 (kW) | 82.5806 | 110.2787 | -27.6981 |
| 模式切换次数 | 2 | 2 | 0 |

| 成本分项 | FDP | 状态机 | FDP − 状态机 |
| --- | ---: | ---: | ---: |
| power | 0.5003 | 0.5000 | 0.0003 |
| switch | 2000.0000 | 2000.0000 | 0.0000 |
| soc_balance | 0.0750 | 0.0081 | 0.0669 |
| low_load_mode3 | 0.0000 | 0.0000 | 0.0000 |
| total | 2000.5753 | 2000.5081 | 0.0673 |

独立检查器约束违例数：FDP 0，状态机 0。

## 自动测试

- python -B -m pytest -q -p no:cacheprovider tests/test_fdp_research.py
- 结果：14 passed in 0.18s，退出码 0。

## SOC 网格与权重敏感性

| SOC 步长 | 权重方案 | outcome | safe | 轨迹 | FDP 成本 | 状态峰值 | 转移数 | 耗时 ms | Python 峰值字节 |
| ---: | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| 0.1000 | paper_reference | ok | True | [1, 3, 3, 1] | 2000.6514 | 9 | 24 | 12.637 | 260119 |
| 0.1000 | half_soc_balance | ok | True | [1, 3, 3, 1] | 2000.5764 | 9 | 24 | 12.252 | 244268 |
| 0.1000 | double_soc_balance | ok | True | [1, 3, 3, 1] | 2000.8014 | 9 | 24 | 12.532 | 249613 |
| 0.0500 | paper_reference | ok | True | [1, 3, 3, 1] | 2000.5753 | 9 | 24 | 12.127 | 218618 |
| 0.0500 | half_soc_balance | ok | True | [1, 3, 3, 1] | 2000.5378 | 9 | 24 | 11.707 | 231498 |
| 0.0500 | double_soc_balance | ok | True | [1, 3, 3, 1] | 2000.6503 | 9 | 24 | 12.603 | 235888 |
| 0.0250 | paper_reference | ok | True | [1, 3, 3, 1] | 2000.5376 | 9 | 24 | 11.381 | 235497 |
| 0.0250 | half_soc_balance | ok | True | [1, 3, 3, 1] | 2000.5188 | 9 | 24 | 11.281 | 235022 |
| 0.0250 | double_soc_balance | ok | True | [1, 3, 3, 1] | 2000.5751 | 9 | 24 | 11.332 | 234639 |

## 24 小时代表性全程计算

- 区间数：48；规划时长：24.0 小时；SOC 网格步长：0.005。
- 结果：ok，safe=True；末端 SOC=[0.46938894575634815, 0.47135179498439267]。
- 峰值状态层：32；转移数：1539；耗时：284.894 ms。
- Python 峰值分配：3906492 bytes；tracemalloc 不含本机原生分配。
- 独立检查：481/481 项通过。
- 该情景采用具名的仿真电池与需求参数，非实测全航程。

## 复现

从仓库根目录运行：

    python -m pytest -q tests/test_fdp_research.py
    python -m scripts.fdp_research_report --output-dir docs/验收

测量环境：Python 3.12.14；Windows-11-10.0.26200-SP0；CPU：Intel64 Family 6 Model 191 Stepping 2, GenuineIntel。

## 已知限制

- 接口仍标记为 research-tools/2.0-draft，尚未完成跨工具契约冻结或端到端联调。
- 网格状态向下取整以避免高估 SOC；网格上没有路径不证明连续问题或实船全局无解。
- 首版不支持充电；负母线需求返回明确的不支持结果。
- 24 小时输入使用本工具内的具名仿真假设；不能用于声称实船 SOC、节能或安全。
- Python 峰值内存使用 tracemalloc 测得，不包含本机原生库分配。
