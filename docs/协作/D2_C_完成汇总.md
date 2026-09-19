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
- 未确认限速或等待时间时返回 `need_clarification`，不按 0 填充。
- 未采用船舶参数或规划 SOC 下限时保留缺项，不伪造 `ok`。
- Tdata/Tseg 返回前调用 `validate`。

## 环境与测试

- D1 环境检查发现本机 `python` 为 Windows 商店占位符，pytest 尚未能执行。
- 本日新增测试命令：`python -m pytest tests/test_d2_tools.py -q`。
- 需要先安装 Python 3.10+，再执行 `python -m pip install -e ".[dev]"`。

## 未完成与阻塞

- 当前环境尚未完成 Python/pytest 安装，因此没有虚构测试通过结果。
- 航段限速、等待时间和 A 采用的船舶安全参数仍需确认，Tseg 当前按契约追问。
- UI 是文本骨架，尚未接入真实能耗、航速和 SOC 结果。
- PPT 未修改；后续应根据真实 UI 截图、B 的冻结结果和 A 的架构页面意见进行最小更新。
