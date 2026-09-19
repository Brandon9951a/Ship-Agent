# Scripts

Utility scripts for local development, data preparation, and validation will live here.

## 成员B：D1只读数据检查

```powershell
.\.venv\Scripts\python.exe scripts/inspect_data.py --mapping configs/data_mapping.d1.json --output artifacts/d1-inspection.json
.\.venv\Scripts\python.exe -m tools.vessel_facts
```

在项目根目录执行。检查脚本递归查找三份指定历史数据，或用位置参数/`--search-root`指定资料路径。退出码0为检查完成、2为文件缺失、1为失败；输出不代表模型或船舶安全验收。事实审计退出码2表示待确认参数，1为非法配置，0仅表示最低预算输入齐备。

CSV/XLSX源文件只读；XLSX不重算公式，使用缓存。Excel相对秒使用`elapsed_seconds`，不生成日期或时区；单位必须有依据。无映射也可检查结构，但时间保持未映射。统计区分含全空记录数与非空记录数，报告不导出原坐标、原始行或文本样例。实际报告见`docs/数据/D1_数据检查报告.md`。

## 成员B：D2保守清洗与人工数值验证

```powershell
.\.venv\Scripts\python.exe -m scripts.prepare_data --output-dir artifacts/d2-prepared-new
.\.venv\Scripts\python.exe -m scripts.validate_energy
.\.venv\Scripts\python.exe -m pytest tests/test_prepare_data.py tests/test_energy_math.py -q
```

准备程序复用D1真实字段映射。每个源逻辑记录输出一条JSONL审计记录；只输出已明确单位的数值通道、时间、记录号及质量标记，不输出原行、GPS或未知意义通道的值。源表不修改、不排序、不填零、不插值。Excel只读取公式缓存，相对秒不变成日期；CSV无时区时保持无时区。

同时间戳的全部成员均待确认，包括相同记录和不同快照。未知速度/功率单位不转成模型输入；已知速度和功率不因未确认SOC字段而消失。`candidate`仅表示通过结构检查，不能直接当稳定航行训练样本。全部输出固定`training_ready=false`。

`primary_counts`按照代码中的`PRIORITY`逐条互斥归类，合计等于含全空记录的总数；`flag_counts_overlapping`可重叠，不可相加求总数。重复标记数指重复组全部成员，与D1“多余重复次数”不同。重复数值采集的真实合并策略仍待协议。

输出只允许项目`artifacts/`内的新目录，现有目录拒绝覆盖；复跑请换目录名。正常准备退出0，默认资料检索缺部分预期文件退出2，解析/映射/输出错误退出1。不提交JSONL审计产物，也不把它当作合法公开数据样本。

人工验证脚本不读取实船数据，示例系数0.125与辅助2kW均为人工测试值，不写入船舶采用配置。`tools/tenergy.py`现提供符合公共`ToolResponse`/`EnergyResult`契约的候选计算入口：缺模型、来源、限速或等待时间时返回`need_clarification`；仅`synthetic_demo`允许显式等待假设，并在每项结果标记禁止用于实船结论；正式`approved`模式要求A批准记录且禁止默认等待时间。该入口尚未注册到占位主流程，真实标定和A参数批准仍未完成。详见`docs/协作/D2_B_完成汇总.md`。

### 2026-09-18质量边界修复

XLSX读取器允许省略末尾空单元格：D2只在已映射时间及已知数值通道均有列位置时接受缺失的非必需尾列；缺少已知通道、超出表头的列仍标记异常。CSV/TSV仍严格检查行宽，不把少列解释成Excel空尾格。仅补足结构空值表示，不补测量值。

时间顺序按当前连续块的最高时间比较，`0,5,4,4.5,6`中4和4.5都标记倒序。无效/缺失时间之后重新建立块，并输出不同`continuity_group`，因此跨块也不能盲目积分。分组号仅是质量连续块，不是已确认的独立航次或训练分组。

准备程序新增`--max-gap-seconds`（默认300秒，沿用D1检查的质量审查阈值，不是已批准安全阈值或采样协议）。相邻时间上界间隔严格大于阈值时，右侧记录标记`time_gap`并开始新连续块；重复时间仍全部隔离，记录不删除。API `prepare_rows`/`prepare_file`同样接受该阈值。
