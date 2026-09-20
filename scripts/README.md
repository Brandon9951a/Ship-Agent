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

人工验证脚本不读取实船数据，示例系数0.125与辅助2kW均为人工测试值，不写入船舶采用配置。`tools/tenergy.py`提供符合公共`ToolResponse`/`EnergyResult`契约的候选计算入口：缺模型、来源、限速或等待时间时返回`need_clarification`；仅`synthetic_demo`允许显式等待假设，并在每项结果标记不得用于实船结论；正式`approved`模式要求A批准记录且禁止默认等待时间。详见`docs/协作/D2_B_完成汇总.md`。

### 2026-09-18质量边界修复

XLSX读取器允许省略末尾空单元格：D2只在已映射时间及已知数值通道均有列位置时接受缺失的非必需尾列；缺少已知通道、超出表头的列仍标记异常。CSV/TSV仍严格检查行宽，不把少列解释成Excel空尾格。仅补足结构空值表示，不补测量值。

时间顺序按当前连续块的最高时间比较，`0,5,4,4.5,6`中4和4.5都标记倒序。无效/缺失时间之后重新建立块，并输出不同`continuity_group`，因此跨块也不能盲目积分。分组号仅是质量连续块，不是已确认的独立航次或训练分组。

准备程序新增`--max-gap-seconds`（默认300秒，沿用D1检查的质量审查阈值，不是已批准安全阈值或采样协议）。相邻时间上界间隔严格大于阈值时，右侧记录标记`time_gap`并开始新连续块；重复时间仍全部隔离，记录不删除。API `prepare_rows`/`prepare_file`同样接受该阈值。

## 成员B：D3航速搜索与SOC预算人工链路

```powershell
.\.venv\Scripts\python.exe -m scripts.validate_d3
.\.venv\Scripts\python.exe -m pytest tests/test_d3_planning.py -q
```

`validate_d3`只连接`Tenergy → Tspeed → Tmanagement`，使用100kWh容量、人工三次方系数等测试参数，并输出`real_ship_validation=false`、单位、来源和完整假设。它不包含parser、Tdata、Tseg或orchestrator，也不使用当前待审批的豫交投001参数。

Tspeed仅在显式候选网格内穷举，按总能量需求、耗时和航速序列排序；逐段核对限速与模型峰值，并检查时间和SOC预算。Tmanagement不模拟途中补能，只计算同一有效容量下的SOC轨迹、报警和最低补能缺口。`total`口径不重复加入辅助能耗；`propulsion`口径按已提供辅助功率补入。缺少来源、采用值或路线约束时返回追问，不填默认值。

## D3 A正常任务端到端验证

```powershell
.\.venv\Scripts\python.exe -m scripts.validate_d3_e2e
```

该脚本运行`parser → Tdata → Tseg → Tenergy → Tspeed → Tmanagement → report`，采用`configs/demo_policy.yaml`中A批准的`synthetic_demo`速度与单点锚定模型。输出含航段、速度、ETA、推进/辅助/总能耗、SOC、约束、来源和假设，并固定`real_ship_validation=false`。云端模型未配置或调用失败时，报告使用模板化说明；工程数字不交给大模型生成。

## 成员B：D4三场景软件级验收

```powershell
.\.venv\Scripts\python.exe -m scripts.validate_d4 --output-dir artifacts/d4-new
.\.venv\Scripts\python.exe -m pytest tests/test_d3_planning.py tests/test_d4_scenarios.py -q
```

`validate_d4`连接当前Tdata/Tseg部分结果与`Tenergy → Tspeed → Tmanagement`计算链，在同一条50.5km资料航线、同一候选航速网格及同一功率/SOC边界下运行正常、时间不可行和低SOC三类场景。输出目录必须不存在；程序生成`results.json`和`summary.csv`并拒绝覆盖旧结果。

演示使用A批准的1411.065kWh有效容量、30%规划SOC下限、30kW辅助功率，以及由经济航速11.112km/h和推进功率90kW推导的三次方系数。系数未由独立实船航次标定；逐段真实限速仍未知，11.112km/h只作为软件演示搜索上界。所有结果均为`real_ship_validation=false`，不可作为实船航行、安全或补能结论。

## 成员B：D5批量验证与同条件对照

```powershell
.\.venv\Scripts\python.exe -m scripts.validate_d5 --output-dir artifacts/d5-new
.\.venv\Scripts\python.exe -m pytest tests/test_d4_scenarios.py tests/test_d5_experiments.py -q
```

`validate_d5`展开8条正/返向子航线与5组时间/SOC条件，共40项软件实验。每项结果都由独立有限网格穷举复核状态、不可行分类和最低能耗选择；可行项再与相同航线、候选网格、时间、SOC、功率和限速约束下的“最快可行”策略比较。

输出目录必须不存在。程序生成`raw_results.json`、`aggregate.json`、`cases.csv`、`energy_comparison.svg`和`evidence_manifest.json`，最后一项记录其余文件的SHA-256。仓库冻结证据位于`docs/验证/D5_B_冻结证据/`。图表和差异比例只表示当前演示模型及离散网格内的软件对照，不是实船节能率、模型精度或运营收益。

## 成员B：D6数字、单位与冻结证据审计

```powershell
.\.venv\Scripts\python.exe -m scripts.audit_d6
.\.venv\Scripts\python.exe -m scripts.audit_d6 --output artifacts/d6-audit.json
.\.venv\Scripts\python.exe -m pytest tests/test_d5_experiments.py tests/test_d6_release_audit.py -q
```

`audit_d6`只读检查D5冻结证据的SHA-256、JSON/CSV用例与数值、统一单位、当前演示参数、README/说明书限制，并使用当前代码重跑40项实验。重跑比较只忽略运行环境和计时字段；状态、分类、距离、能耗、SOC、基线与差异字段必须一致。输出文件存在时拒绝覆盖。

审计通过只代表代码与证据一致，不是实船模型标定、航行安全认证或C的独立安装验收。仓库审计结果位于`docs/验证/D6_B_审计报告.json`。
