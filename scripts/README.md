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

人工验证脚本不读取实船数据，示例系数0.125与辅助2kW均为人工测试值，不写入船舶采用配置。数值函数未注册到五工具主流程。详见`docs/协作/D2_B_完成汇总.md`。
