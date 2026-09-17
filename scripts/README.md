# Scripts

Utility scripts for local development, data preparation, and validation will live here.

## 成员B：D1只读数据检查

```powershell
.\.venv\Scripts\python.exe scripts/inspect_data.py --mapping configs/data_mapping.d1.json --output artifacts/d1-inspection.json
.\.venv\Scripts\python.exe -m tools.vessel_facts
```

在项目根目录执行。检查脚本递归查找三份指定历史数据，或用位置参数/`--search-root`指定资料路径。退出码0为检查完成、2为文件缺失、1为失败；输出不代表模型或船舶安全验收。事实审计退出码2表示待确认参数，1为非法配置，0仅表示最低预算输入齐备。

CSV/XLSX源文件只读；XLSX不重算公式，使用缓存。Excel相对秒使用`elapsed_seconds`，不生成日期或时区；单位必须有依据。无映射也可检查结构，但时间保持未映射。统计区分含全空记录数与非空记录数，报告不导出原坐标、原始行或文本样例。实际报告见`docs/数据/D1_数据检查报告.md`。
