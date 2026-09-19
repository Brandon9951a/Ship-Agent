# D2 C 完成汇总

日期：2026-09-19
成员：C
分支：`d2-lgz`

## 已完成

- 新增 `tools/tdata.py`：地点归一化、船舶/路线事实收集、来源、缺项和冲突保留。
- 新增 `tools/tseg.py`：正向、返程和连续子航线选择；路线不连续时追问。
- 新增 `tests/test_d2_tools.py`：覆盖未知地点、未知限速/等待、子航线和返程。
- 新增 `configs/examples/segments_d2_subroute.json` 和 `configs/examples/segments_d2_reverse.json`。
- 新增 `ui/app.py`：文本输入、Tdata/Tseg 状态和 JSON 输出骨架。
- 新增 `docs/协作/D2_C_环境记录.md`，记录 Bundled Python、pytest 和实际测试结果。
- 补充需求素材索引中的 D2 实现边界。
- 未修改 PPT，仅保留后续修改意见。

## 关键行为

- 内部单位遵循契约：km、km/h、h、kW、kWh、SOC 0-1。
- 未确认限速时返回 `need_clarification`，不伪造安全约束。
- 河南境内船闸采用 A 批准的演示规则：排队等待默认 0 小时且允许人工覆盖；该值不包含船闸内部通行时间。规则仅作用于明确登记的河南航线。
- 未采用船舶参数或规划 SOC 下限时保留缺项，不伪造 `ok`。
- Tdata/Tseg 返回前调用 `validate`。

## 环境与测试

- C 原始环境记录见 `D2_C_环境记录.md`。
- A 集成审核已使用项目虚拟环境执行全量测试：353 passed。

## 未完成与阻塞

- 航段限速仍需补充，Tseg 当前按契约追问；河南境外船闸等待时间仍必须由用户输入。
- UI 是文本骨架，尚未接入真实能耗、航速和 SOC 结果。
- PPT 未修改；后续应根据真实 UI 截图、B 的冻结结果和 A 的架构页面意见进行最小更新。

## A 集成修正

- 同步 `battery_group_capacity_kwh=783.925kWh`、初始 `SOH=90%` 和 `SOC 35%` 软件关注线。
- 为河南航线注入已批准的排队等待演示默认值，同时保留来源与假设说明。
- 修正 UI 配置路径，使其不依赖启动时的当前工作目录。
- `need_clarification` 状态也返回已选航段，便于 UI 展示缺项与已确认数据。
