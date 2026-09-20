# D3成员B：航速可行性与SOC能量管理阶段交付

2026-09-19，成员B（计算与验证负责人）。D3正式任务为航速求解、时间/SOC/功率可行性检查、SOC轨迹和补能需求；共同目标为一条正常任务端到端跑通。当前交付完成B侧确定性计算链，不宣称团队端到端或实船方案已完成。

## 分支与基线

- 远程main核查为`9cf4b82`，与本地main一致。
- D2尚未合入main；D3依赖D2的Tenergy和清洗/数值基础，因此从`81eb6b8`创建`codex/b-d3-lwj`。
- main未被修改。后续A应先处理D2依赖，再决定采用堆叠PR或合并后的D3重基线；不得把D3分支上的D2代码误报为main已有能力。

## 本轮实现

| 文件 | 内容 | 边界 |
|---|---|---|
| `tools/tenergy.py` | 输出恒速模型的显式峰值功率，供同边界功率检查 | 峰值是模型值，不是实测瞬时峰值；演示结果保留非实船标记 |
| `tools/tspeed.py` | 对每段候选做有限网格穷举；检查限速、功率、时间、SOC预算；返回OptimizationResult | 只保证给定离散网格内最优，不声称连续/全局最优，不放宽硬约束 |
| `tools/tmanagement.py` | 区分total/propulsion口径，形成逐段SOC、报警、辅助能耗和补能缺口 | 不模拟途中补能或双电池FDP；容量按一个采用有效预算，不重复计组 |
| `tests/test_d3_planning.py` | 正常、缺参、功率/时间/SOC不可行、口径、重复候选、网格上限、冲突和补能测试 | 使用人工参数，不是实船效果测试 |
| `scripts/validate_d3.py` | 可复现人工三工具链与结构化输出 | 明确不包含parser/Tdata/Tseg/orchestrator |

### Tspeed规则

输入必须包含完整Segment、Tenergy候选、任务时间约束以及带来源的有效容量、初始SOC、规划SOC下限、同边界功率上限和辅助功率。候选缺峰值、航段缺限速/等待/来源时返回`need_clarification`。候选耗时必须等于`distance/speed + waiting`；重复航段、重复航速、混合能耗口径、非有限数和超大组合均拒绝。

先逐段筛除超速/低于最低速度或超功率候选，再穷举跨段组合。可行组合同时满足用户最严格时间约束和`capacity × (soc_initial - soc_min)`预算；按总需求、耗时、航速序列确定唯一稳定结果。propulsion口径的排序与SOC初检包含辅助能量。不可行分类为power、route、time、soc或combined，结果不夹带伪造的选定方案。

### Tmanagement规则

只接收Tspeed可行方案。total口径直接采用总能耗，辅助分项只展示、不重复加入；propulsion口径按辅助功率乘每段总时长补入。SOC逐段按同一有效容量扣减，不允许凭空增加。若低于规划下限，返回`soc`不可行、`safe=false`和最低补能缺口，不把未执行补能的任务标为安全；数学余量低于0时轨迹以物理下界0记录，能量缺口仍完整保留。

Tdata遗留缺项和未解决冲突会阻止能量管理。当前算法不擅自把25%报警、20%断电或任一候选值设为规划下限。

## 人工可复现结果

执行：

```powershell
.\.venv\Scripts\python.exe -m scripts.validate_d3
```

人工任务为两段8km/4km、候选4/5km/h、最长2.5h；容量100kWh、初始SOC 0.8、规划下限0.2、功率上限50kW、辅助2kW、三次方系数0.125均为测试值。4种组合中选择两段5km/h：总时长2.4h、total能耗42.3kWh、模型峰值17.625kW、SOC轨迹0.518→0.377、补能缺口0。末端低于人工报警值0.4，仍输出警告。

输出含`real_ship_validation=false`，只证明计算、约束和结构可复现。不能引用为豫交投001的航速、能耗、续航或安全结论。

## 验收

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

完整测试329项通过；依赖无损坏；空白检查通过。D3新增测试覆盖正常结果、公共契约、峰值不由平均值替代、缺参、来源、限速、重复候选、组合上限、时间/功率/SOC分类、辅助能耗一次计入、补能缺口、Tdata缺项/冲突和上游不可行停止。

提交`abfe446`后，从该分支创建无硬链接临时克隆`ship-agent-b-d3-8a9382a3983a43a68150ad0fa7692e96`。确认导入的`tools/tspeed.py`来自克隆目录；使用项目现有Python环境执行全量测试仍为329项通过，人工D3链返回ok且`real_ship_validation=false`。运行前后克隆内均无`artifacts`目录，工作区保持干净。该检查证明仓库/输出状态可复现，但不等于在全新环境重装依赖。

## 未完成与协作阻塞

1. A仍需批准有效容量、同边界功率上限、辅助功率、规划SOC下限、当前BMS设置及能耗边界。
2. C/资料方仍需补全逐段限速、等待、当前补能能力；现有`route_facts.yaml`对应字段为null。
3. CSV单位、功率测量侧、负值、多快照与航次独立性未确认，无法完成真实模型标定和误差验证。
4. Tdata/Tseg真实工具、A的parser/orchestrator、主流程工具注册、报告/value lock及C界面尚未接通，因此未达到“一条正常实船任务端到端跑通”。
5. 双电池真实拓扑与分组容量未批准，本轮只做单一有效能量预算；未实现途中补能执行、充电曲线、FDP或全局优化。

下一步由A先确认参数与D2/D3依赖合并策略，C提供完整航段约束；B再以批准配置替换人工值、标定Tenergy并参与完整正常任务联调。
