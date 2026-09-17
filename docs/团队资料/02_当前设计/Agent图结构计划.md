# 绿航智算智能体 —— Agent Loop 图结构计划

> 本文件是把"决策权边界"从原则落到 LangGraph 图结构的靶子稿,供后续逐点讨论与修改。
> 本周执行配套：《本周实现范围与接口要求.md》与《../00_必读/七日任务与参赛交付表.md》。下文是完整设计参考，不代表所有节点已实现。

## 0. 设计总原则(回顾)

1. **语义给 LLM,计算给工具,边界用规则兜底。**
2. **让 LLM 做"选择题",不让 LLM 做"填空题"**——LLM 决策的选项和后果由工具产出,LLM 只行使"选哪个/怎么解释"的决策权。
3. **创新主线:数值可信 Agent**——每个工程数值都能追溯到工具字段,LLM 不产数值。
4. 框架用 LangGraph 作底座(状态机 + 节点 + 条件边 + checkpoint + HITL),创新点在"领域逻辑 + 可信性",不在框架本身。

---

## 1. 状态定义(State)

LangGraph 的共享状态,贯穿全图。字段按模块 M3 的 dataclass 序列化存放。

```python
class AgentState(TypedDict):
    # 对话历史(LLM 上下文,add_messages 追加)
    messages: Annotated[list, add_messages]

    # 任务与解析
    voyage_request: dict        # VoyageRequest —— parse 输出
    need_clarify: bool          # 是否缺关键字段,需追问
    clarify_question: str       # 追问内容

    # 数据
    vessel_state: dict          # VesselState —— tdata 输出

    # 航段与能耗
    segments: list              # list[RouteSegment] —— tseg 输出
    energy_results: list        # list[EnergyCandidate] —— tenergy 输出
    optimization: dict          # OptimizationResult(含 feasible) —— tspeed 输出
    management_plan: dict       # ManagementPlan —— tmanagement 输出
    voyage_plan: dict           # VoyagePlan —— 汇总

    # 反思/重规划
    feasible: bool
    violation_reason: str       # 不可行原因
    replan_count: int           # 反思/重规划轮次
    in_transit_state: dict      # 航行进行状态(当前位置/实际SOC/已走里程),航中重规划用

    # 校验与输出
    value_lock_pass: bool       # 数值锁定是否通过
    report: str                 # 最终报告
    trace: list                 # AgentTrace(ReAct 风格,Observation 由工具填充)
```

---

## 2. 节点清单(分五类)

### 2.1 LLM 决策节点(3 个,真 Agent 的"决策权"所在)
| 节点 | 决策内容 | 方式 |
|---|---|---|
| `parse_node` | 船员口语 → `VoyageRequest` | 语义抽取,落在 schema;关键字段(起终点/SOC/时间)规则二次复核 |
| `reflection_node` | 把妥协选项文案化 + 解释推荐原因 + 和船员对话 | 呈现/解释;选项和数值由 intent_translator + 工具产出 |
| `report_node` | `VoyagePlan` → 船员语言 | 润色解释;数字必须来自 plan(数值锁定) |

### 2.2 确定性工具节点(5 个,计算权,规则锁死)
| 节点 | 职责 | 决策权 |
|---|---|---|
| `tdata_node` | 补全船舶/电池/环境状态 | 锁死:用户输入优先 → 历史分布/默认 |
| `tseg_node` | 航段划分(8 类断点 + 激活条件) | 锁死:断点识别/激活规则化 |
| `tenergy_node` | 能耗预测(物理模型) | 锁死 |
| `tspeed_node` | 航速优化(DE/网格),产出 `feasible` | 锁死 |
| `tmanagement_node` | 双电池能量管理(FDP) | 锁死 |

### 2.3 校验/安全节点(规则)
| 节点 | 职责 |
|---|---|
| `value_lock_node` | 数值锁定:报告里的数字与 `VoyagePlan` 比对,偏离即拒绝回退模板 |

### 2.4 环境节点
| 节点 | 职责 |
|---|---|
| `environment_node` | 读环境仿真器(M19),输出当前环境;判断是否触发重规划(阈值/硬事件) |

### 2.5 追问节点(规则 + LLM)
| 节点 | 职责 |
|---|---|
| `clarify_node` | 缺关键字段时生成追问,交用户补充(用户输入 → 重新 parse) |

---

## 3. 图结构:航前基线规划主流程

```
START
  │
  ▼
parse_node(LLM)         口语 → VoyageRequest
  │
  ▼
{关键字段缺失?}         ← 规则(字段齐全性检查)
  ├─ 是 ──▶ clarify_node(追问) ──▶ END(等待用户补充)
  │
  └─ 否
       ▼
tdata_node(工具)        补全 VesselState
       │
       ▼
tseg_node(工具)         航段划分(含补能迭代,见 §6)
       │
       ▼
tenergy_node(工具)      能耗预测
       │
       ▼
tspeed_node(工具)       航速优化 → 产出 feasible + 到港SOC + ETA
       │
       ▼
{feasible?}             ← 规则(读 feasible bool,不交给 LLM 判断)
  ├─ 是 ──▶ tmanagement_node(工具) ──▶ report_node(LLM) ──▶ value_lock_node(规则) ──▶ END
  │
  └─ 否 ──▶ intent_translator(枚举妥协选项) ──▶ reflection_node(文案化) ──▶ interrupt(问船员)
                │
                ▼
            {船员选择?}
                ├─ 有方向 ──▶ intent_translator(翻译) ──▶ 回到 tseg_node(修改输入重算,最多2轮)
                └─ 放弃 ──▶ 输出"不可行报告 + 最接近的可行方案" ──▶ END
```

---

## 4. 图结构:航中滚动重规划

> 触发源 = 环境仿真器(M19)。环境变化是仿真器内生的,不是演示者假装。

```
(航行中,按已生成的方案推进)
  │
  ▼
environment_node(仿真器)  读当前环境 / 实际SOC / 临时限航通告
  │
  ▼
{偏差超阈值 or 硬事件?}   ← 规则(阈值比较 / 事件类型)
  ├─ 否 ──▶ 继续航行(不打断)
  │
  └─ 是 ──▶ ① 锁定已走完的航段
                │
                ▼
            ② 以"当前位置 + 当前真实状态"为新起点
                │
                ▼
            ③ 重跑 tseg → tenergy → tspeed → tmanagement(只重划剩余航程)
                │
                ▼
            ④ report_node(解释"方案为什么变"+ 前后对比) ──▶ value_lock_node ──▶ END
```

---

## 5. 补能迭代闭环(航段划分内部)

> 补能点是否停靠,依赖 SOC 规划;SOC 规划又依赖分段 → 迭代解耦。

```
tseg(初始分段:硬断点 + 任务断点 + 约束断点,【不含补能点】)
  │
  ▼
tenergy + tspeed  算全程 SOC 轨迹
  │
  ▼
{途中 SOC 跌破报警阈值(25%)?}   ← 规则
  ├─ 否 ──▶ 分段确定,继续主流程
  │
  └─ 是 ──▶ 在跌破点上游最近的补能点插入"补能断点"
                │
                ▼
            {迭代 < 3 轮?}   ← 规则
                ├─ 是 ──▶ 回到 tseg 重新分段
                └─ 否 ──▶ 判定不可行,转 reflection_node
```

**补能决策的 LLM 落点**:当存在多个候选补能方案时(如"在漯河港补 40 分钟" vs "全程降速 1 节省电不停靠"),工具枚举候选,`reflection_node` 做**选择题**并解释权衡。MVP 阶段先用纯规则(SOC 硬阈值 + 最近补能点),LLM 选择题作为亮点后加。

---

## 6. 决策权边界总表(节点级)

| 节点/分支 | 类型 | 决策内容 | 锁死 / 自主 |
|---|---|---|---|
| parse | LLM | 口语 → 结构化约束 | 自主(关键字段规则复核) |
| 缺字段判断 | 规则 | 是否追问 | 锁死 |
| tdata | 工具 | 数据补全 | 锁死 |
| tseg | 工具 | 断点识别 + 激活 | 锁死 |
| 补能决策 | LLM(后加) | 停不停 / 在哪停 | 自主(规则兜底) |
| tenergy | 工具 | 能耗计算 | 锁死 |
| tspeed | 工具 | 航速求解 + 可行性 | 锁死 |
| feasible 分支 | 规则 | 可行 / 不可行 | 锁死(读 bool,不给 LLM) |
| reflection | LLM | 调整策略 | 自主(轮次规则兜底) |
| tmanagement | 工具 | 能量分配 | 锁死 |
| report | LLM | 生成解释 | 自主(数值锁定) |
| value_lock | 规则 | 数值校验 | 锁死 |
| environment | 规则 | 环境读取 + 重规划触发 | 锁死 |

**一句话**:LLM 自主的只有 3 处(parse / reflection / report),且每处都被"schema + 工具验证 + 规则兜底"三层约束;其余全是锁死的工具与规则。

---

## 7. 数值锁定的落点(已定稿)

**落点:独立节点 `value_lock_node`,放在 `report_node` 之后、END 之前。**

**策略:② 纠错重试 + ① 回退模板兜底(组合)。**

```
report_node(LLM 生成报告)
      │
      ▼
value_lock_node(校验数字)
      │
      ├─ 通过 ──► 直接用(保留自然表达)
      │
      └─ 不通过 ──► 带纠错提示重试 1 次("耗电 780 与工具 812 不符,请改")
                     │
                     ├─ 改对 ──► 直接用
                     └─ 还错 ──► 回退模板报告(底线,必对)
```

**实现要点**:
- `value_lock_node` 抽取报告数字 → 与 `VoyagePlan` 字段逐一比对。
- 不通过 → 带纠错提示回到 `report_node`(重试 1 次)→ 再校验。
- 重试后仍不通过 → 用模板重新渲染(只引用 plan 字段,数字必然正确),`value_lock_pass=False`。
- 独立节点:职责单一、可单测、显式出现在 trace 里(答辩加分)。

---

## 8. 反思循环(已定稿)

**完整机制详见 [`反思循环与意图翻译器设计计划.md`](./反思循环与意图翻译器设计计划.md),这里是图结构层面的定稿。**

```
tspeed → feasible=False + infeasible_type
        │
        ▼
intent_translator(确定性) —— 按 infeasible_type 查表,枚举相关 2~3 个妥协选项(调真工具试算)
        │
        ▼
reflection_node(LLM) —— 把选项文案化 + 附推荐原因,和船员对话
        │
        ▼
interrupt(问船员:"你能接受哪种妥协?")
        │
        ▼
船员选择(演示时用户就是船员)
        │
        ▼
intent_translator —— 把选择翻译成输入修改
        │
        ▼
Tseg/Tspeed 重算(修改后的输入)
        │
        ├─ 可行 → tmanagement → report
        └─ 仍不可行 → 沿所选方向增量搜索(最多 2 轮)→ 再问船员
```

**关键点**:
- 反思 = 工具枚举选项 + **船员拍板(HITL)** + 意图翻译 + 重算,不是"LLM 自由提策略"。
- **船员选方向、工具算数值**,LLM 只负责呈现和解释(选项文案化 + 推荐原因)。
- "意图 → 输入修改"的映射、"不可行类型 → 候选意图"的映射,都在 `intent_translator`(确定性组件)。
- 工具输出不可行时,需带结构化 `infeasible_type` 字段(不只是人话 reason)。

---

## 9. 条件边的路由归属(已定稿)

| # | 条件边 | 谁决定 | 依据 |
|---|---|---|---|
| 1 | 关键字段缺失?(parse 后) | **规则** | 字段齐全性检查 |
| 2 | feasible?(tspeed 后) | **规则** | 求解器输出的 `feasible` bool |
| 3 | 补能 / 降速选哪个?(反思) | **船员拍板(HITL)** | 工具枚举选项,船员选 |
| 4 | replan_count 超限?(反思循环) | **规则** | 轮次计数(≤2) |
| 5 | 途中 SOC 跌破阈值?(补能迭代) | **规则** | SOC 轨迹 vs 25% 报警线 |
| 6 | 补能迭代超限?(补能迭代) | **规则** | 计数(≤3) |
| 7 | 环境变化触发重规划?(航中) | **规则** | 偏差阈值 / 硬事件 |

**核心原则**:凡"客观可判定"的分支(缺字段/可行/超限/阈值/触发)全部规则锁死;凡"有取舍权衡"的分支(补能还是降速)交给船员拍板。LLM 不参与路由判断——它的价值在"呈现选项、解释利弊",不在"决定走哪条路"。

---

## 10. 与 LangGraph 概念的对应

| LangGraph 概念 | 本项目落地 |
|---|---|
| State + reducer | §1 的 `AgentState` |
| Node(工具) | tdata/tseg/tenergy/tspeed/tmanagement |
| Node(LLM) | parse/reflection/report |
| Conditional edge | §9 的四条规则分支 + 一条 LLM 选择题 |
| 循环边 | reflection → tseg(反思重规划)、补能迭代闭环 |
| Checkpointer | 支撑"可回溯"与 HITL |
| HITL(`interrupt`) | 反思循环问船员拍板;双电池切供能模式、不可行强制降速的人工确认 |
| 子图 | 环境仿真器(M19)作为独立子图/节点 |

---

## 11. 设计点定稿记录(全部已定)

1. **数值锁定落点**:独立节点 + 纠错重试 + 回退模板兜底(见 §7)。
2. **反思循环深度**:枚举选项 + 船员拍板 + 意图翻译器(见 §8 及《反思循环与意图翻译器设计计划.md》)。
3. **补能决策**:航前补能迭代纯规则(§5);反思阶段补能作为妥协选项由船员拍板(§8)。两层不冲突。

> 港口停靠任务来源(用户输入 / 固定班次表)由A在D1确认，采用值与依据写入事实配置。
