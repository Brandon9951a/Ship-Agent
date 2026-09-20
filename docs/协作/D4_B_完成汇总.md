# D4成员B：三场景计算边界与软件验收

日期：2026-09-20。成员B，计算与验证负责人。分支：`codex/b-d4-lwj`，基线：`main@f076420`。

## 本轮结论

D3的Tspeed/Tmanagement已移植到A审查后的D2主线基线，并保留A对Tenergy的来源、分项和峰值功率修正。D4新增正常、时间不可行、低SOC三类可复现场景；三类结果均符合预期。这里完成的是软件边界验收，不是实船模型验证，所有输出固定`real_ship_validation=false`。

## 实现与边界

- `configs/examples/d4_scenarios.json`：固定三场景输入、同一航线和同一候选网格。
- `scripts/validate_d4.py`：读取Tdata/Tseg部分结果，显式补入演示边界，再运行Tenergy、Tspeed、Tmanagement；输出JSON/CSV且拒绝覆盖。
- `tests/test_d4_scenarios.py`：检查三场景分类、同条件对照、有效容量、功率口径、SOC轨迹、不可行停止、边界诊断、非实船标记和输出复现。

路线采用已核对PPT的平顶山港—军李船闸—马湾船闸两段距离，共50.5km。河南境内排队等待按A批准的软件演示规则取0小时，但不包含船闸内部通行时间。逐段真实限速仍未知；11.112km/h仅作为D4演示搜索上界，并在每段假设中注明“不是通航限速”。

能量预算采用A批准的1411.065kWh演示有效容量，即1567.85kWh标称容量按初始SOH 90%折算；不以标称容量直接计算可用能量。200kW限制属于推进功率边界，因此Tenergy使用`propulsion`口径，30kW辅助负载由Tspeed/Tmanagement按全程时间补入一次，避免把总功率与推进上限混比或重复计入辅助能耗。

三次方系数由A批准的演示经济工况点推导：11.112km/h对应推进功率90kW，得到0.0655942561kW/(km/h)^3。该系数仅用于软件演示，未由独立实船航次标定。

## 三场景结果

| 场景 | 变化项 | 结果 | 边界证据 |
|---|---|---|---|
| 正常 | 初始SOC 85%，最长10h | `ok` | 选择6km/h；总耗时8.4167h；推进119.2504kWh，辅助252.5kWh，总需求371.7504kWh；末端SOC 58.65% |
| 时间不可行 | 最长4h | `infeasible/time` | 网格内最快4.5446h，至少超时0.5446h；未放宽时限或安全边界 |
| 低SOC | 初始SOC 45%，最长10h | `infeasible/soc` | 30%规划下限以上可用211.6598kWh；同网格最低需求371.7504kWh，诊断缺口160.0906kWh；未伪装为已执行补能 |

上述数值由同一16组合候选网格产生，只改变用户时限或初始SOC。不可行场景不调用Tmanagement生成成功计划；缺口和最短时间仅是同网格边界诊断，不是船员已选择的调整方案。

## 验收记录

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_d3_planning.py tests\test_d4_scenarios.py
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

- D3/D4定向测试：25项通过。
- 全量测试：378项通过。
- 项目虚拟环境已按`pyproject.toml`安装LangGraph 1.2.11；此前缺包问题已解除。
- 本地复现输出：`artifacts/d4-20260920-lwj-v2/results.json`和`summary.csv`。`artifacts/`按仓库规则忽略，不随代码提交。

## 未完成与交接

1. A仍需把真实Tspeed/Tmanagement适配器接入LangGraph，并完成最多两轮的人工选择、重算、报告数值锁定和故障回退。
2. C仍需在UI接入三场景结果、边界诊断和假设标识；不可把演示搜索上界显示成真实限速。
3. 资料方仍需补逐段限速、船闸内部通行时间、充电功率和可用性；没有这些资料时不输出真实航行或补能方案。
4. B后续在可确认CSV单位、计量侧、航次独立性后才能标定真实能耗系数并开展误差/节能对照；当前数据库只用于保守质量检查，不编造真实精度。

结论：D4中B可独立完成的三类计算边界、可复现脚本、原始JSON/CSV出口和自动测试已完成。团队级D4验收仍取决于A的调整重算/可信报告和C的三场景界面接入。
