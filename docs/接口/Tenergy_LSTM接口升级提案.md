# Tenergy 条件式 LSTM 接口升级提案（待三工具负责人确认）

现有 `EnergyResult` 以恒速航段的单个功率/能量表示候选，未表达逐时步预测、测量边界和模型适用性。建议增加独立的 `EnergyForecastV1`，保留现行 `EnergyResult` 与 `run_tenergy` 行为。LSTM 工件为实验状态，不能直接接入正式 Tspeed/Tmanagement。

## 候选输入

`evaluate(speed_plan, environment_scenario, history, model_version)` 和 `batch_evaluate(plans, environment_scenarios, histories, model_version)`。`speed_plan` 按时间排列，每项含 `segment_id`、`speed_kmh`、`dt_seconds`、`waiting_seconds`、可选 `time`。等待时长必须显式填写，确认没有等待时填 0；此字段为**该时步**对应的等待秒数。`history` 至少 12 点，含以前的航速、观测电机功率和相邻时间差。Excel 六输入扩展另需每个未来时步有来源的水深、水流、风速、风向；共同字段模型不使用这些变量。未来真实功率不得放入模型张量。

当前只支持未来 1–5 步、约 5 秒步长、训练航速范围内的移动工况。大于 5 步返回 `long_horizon_unvalidated`；无历史、缺环境、未知等待时长、越训练域分别返回明确状态。前端不得将这些状态展示为成功计划。

## 候选输出

`status`、`model_version`、`measurement_boundary=propulsion_motor_observation`、`integration_rule=right_endpoint`、`steps[]`、`segments[]`。每个时步含时间、航段 ID、候选航速、预测电机功率、该时步电机电耗、环境来源、质量标记和模型版本。航段汇总含移动秒数、等待秒数、电机侧电耗、峰值电机功率、积分覆盖率。仅对已覆盖的移动时步求和 `Σ P_k Δt_k / 3600`。等待与辅助负载电耗为 `null`。电池侧电耗为 `null`，不得据此计算完整 SOC 约束。

当前返回 `status=experimental`，即使数值可计算也不等于可作为优化目标。按来源留出的 MAE 未优于持续功率基线，正式启用的预测精度门槛未通过。CSV 的速度/功率单位按 LSTM 任务书暂用节/kW；D1 字段字典仍标记未确认。电机测量边界及至电池母线的转换也未核实。

## 待共识字段

1. Tspeed：候选航速轨迹是否逐 5 秒提供，长航次是否接受独立的延长时域模型；批量调用规模、缓存键至少包含模型版本、历史窗口、未来航速和环境情景。
2. Tmanagement：测量边界、电机侧到母线功率的转换位置和来源；在此之前不填电池功率或 SOC 可行性。
3. 三方：新增结构与现有 `EnergyResult` 的适配时机、正式可用状态枚举、环境情景来源格式。

本提案不变更公共 schema，也不改变现行五工具调用链。实验实现与数据报告见 `tools/lstm_energy.py` 和 `models/tenergy-lstm-v5/acceptance.json`。
