# Configs

This directory stores machine-readable configuration for `agent_v2`.

`settings.example.yaml` is the minimal runtime template for the M2 skeleton.

## 成员B的D1配置

- `vessel_facts.yaml`：有来源的候选船舶事实；提案不是采用值。
- `limits.yaml`：SOC候选、独立规划下限与容量/功率/拓扑/能耗口径，待A批准。
- `data_mapping.d1.json`：三份真实历史表的首轮体检映射；未知CSV单位与capacity语义仅作原值统计，不用于计算转换。
- `examples/data_mapping.example.json`：人工结构示范，不是本次真实列映射。

两份.yaml特意使用JSON兼容的YAML 1.2子集，由Python标准库读取，暂不新增公共依赖。A批准采用后须填写`adopted`、来源、`confirmed_by`和`confirmation_status=approved_A`，再复跑审计；助手不自行批准安全参数。

## 航线与地点配置

- `route_facts.yaml`：距离已对照原始港闸PPT第1–8页及第9页核源；各段带SourceRef、完整ID与假设，运营限制仍未知。
- `aliases.yaml`：港口和船闸独立归一化，未知地点必须追问。

以上两份同样使用JSON兼容YAML 1.2子集，`json.load`即可读取。配置`id`在工具实现中映射为`Segment.segment_id`，不能把有未知约束的配置直接当成已通过契约校验的成功结果。
