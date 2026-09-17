# 成员B：D1程序与实际验收记录

实际开发日期：2026-09-17；计划D1：2026-09-18。成员B负责计算与验证，参数采用与公共接口由A确认。

## 分支和提交

本次从最新main基线`9f00970`创建独立分支，现统一命名为`codex/b-d1-lwj`。不向main直接提交、不自动合并。较早的计划分支现命名为`codex/b-plan-lwj`，原计划提交完整保留。

提交标题：`D1: add vessel fact audit and historical data inspector`。在GitHub Desktop顶部的Current branch查看当前分支；Changes查看待提交文件，History查看提交。发布后由A审查，再决定是否合入main。

后续每日从同步后的main建立任务分支，统一使用简洁名称`codex/b-d2-lwj`至`codex/b-d7-lwj`，所有成员B分支以`-lwj`结尾。提交标题以`D2:`开头，D3至D7同理。D1–D7是任务日标签，不必创建同名目录或七个空提交。若后续任务依赖尚未合并的D1代码，先确认依赖分支和PR顺序，不丢弃成果。

## 本次交付与边界

| 任务 | 文件 | 实际状态 |
|---|---|---|
| B1船舶事实与冲突 | `configs/vessel_facts.yaml`、`configs/limits.yaml`、`docs/数据/船舶参数与冲突清单.md` | 文档核对和候选记录完成；采用值待A确认 |
| B1采用记录审计程序 | `tools/vessel_facts.py`、`tests/test_d1_facts.py` | 已实现来源、确认人、数值、单位、SOC顺序和计量边界检查；不隐式批准候选 |
| B2历史数据体检程序 | `scripts/inspect_data.py`、`tests/test_inspect_data.py`、`configs/examples/data_mapping.example.json` | 标准库CSV/XLSX只读检查已实现；人工文件测试通过 |
| B2字段与实际报告 | `docs/数据/字段字典.md`、`docs/数据/D1_数据检查报告.md` | 逻辑字段要求和检索结果完成；真实表头与统计待原始数据 |
| B3能耗基线与验证设计 | `docs/算法/能耗基线与验证设计.md` | 物理基线、积分边界、航次划分、公平对照及指标设计完成；未拟合模型 |
| 环境 | 本地`.venv`，现有`requirements.txt` | 安装成功；未变更公共依赖或schemas |

纠正的关键误用风险：设计文档3.20m是型深；经济航速是6km/h而非6节；两组783.93kWh注释说的是36TEU相关电源，本船30TEU适用性未核实；140/250/200kW不能混作同一边界的功率上限。

## 复现与实测结果

在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe main.py
.\.venv\Scripts\python.exe -m core
.\.venv\Scripts\python.exe -m tools.vessel_facts
.\.venv\Scripts\python.exe scripts/inspect_data.py --search-root .
```

- 自动测试：125 passed；包含main已有85项与D1新增40项。新增测试全部使用人工构造值，覆盖编码、稀疏XLSX、缓存缺失、重复与负值、时间错误/倒序/时区、单位、非法配置及源文件防覆盖。
- 依赖检查：`No broken requirements found.`
- 两个原有运行入口通过；五工具仍是占位流程，不是能耗预测或优化验收。
- 事实审计：`need_clarification`、`errors=[]`、`adopted={}`、`calculation_ready=false`；退出码2符合当前未批准事实的预期。
- 真实文件递归检查：`missing_data`、`files_inspected=0`；三份预期数据缺失，退出码2符合预期。生成报告仅放在Git忽略的`artifacts/`。

## 待A/C或资料提供者处理

1. 提供`data.xlsx`、`log4p-10-29(1).csv`、`log4p-10-31(1).csv`和原始船舶调研报告。已检索当前项目全部子文件夹及隐藏目录；当前没有这些文件或压缩资料包，未声称已搜索整个磁盘。
2. A确认有效容量、峰值功率的计量边界、实际辅助负载、规划SOC下限、电池拓扑和能耗口径，并记录依据。报警/停机阈值不能直接当成规划下限。
3. C提供航段距离、限速、等待时间及载况关联资料。涉及补能时提供真实站点与能力，不由充满时长反推沿途充电。
4. 文件到位后B填写真实列映射并更新体检报告，再进入D2清洗和能耗基线拟合；不得报告尚不存在的模型误差、节能率或实船安全结论。

结论：D1不依赖原始文件的程序、事实整理和验证设计已交付；原始数据体检与采用参数审批尚未完成，D1整体不能标为全部验收通过。
