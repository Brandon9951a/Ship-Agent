"""构建测试用的典型场景：17 航段（含港口/船闸固定航段）与解析功率模型。"""

from __future__ import annotations

from research.contracts import SourceRef, TimeGridSpec
from research.contracts.speed_optimization import SegmentConstraint, SpeedOptimizationRequest
from research.demand import AuxiliaryModel, ConversionModel, DemandBuilderSpec
from research.tspeed.eval import EnergyModelSpec
from research.tspeed.predictor import AnalyticPowerPredictor, EvaluationGrid

DOC = SourceRef(source_id="doc.豫交投001参数", kind="document", locator="docs/团队资料/04_船舶与航线/豫交投001参数.docx",
                confirmed=True)
ASSUMPTION = SourceRef(source_id="assumption.A-power-curve", kind="assumption",
                       locator="research/tests/fixtures.py", confirmed=False,
                       mode="simulation", note="验收用解析功率函数，不代表实船标定")
AUX_ASSUMPTION = SourceRef(source_id="assumption.A-auxiliary", kind="assumption",
                           locator="research/tests/fixtures.py", mode="simulation")
EFFICIENCY_DOC = SourceRef(source_id="doc.推进系统效率", kind="document",
                           locator="docs/团队资料/04_船舶与航线/豫交投001纯电动船调研报告.docx",
                           confirmed=False, note="待现场标定；此处仅作具名情景")
DEADLINE_SOURCE = SourceRef(source_id="user.deadline", kind="user", confirmed=True)

# 参考任务书第 3 节：导师论文以港口/船闸为断点把周口—平顶山划为 17 段。
SEGMENTS = (
    # (id, distance_km, min, max, fixed, waiting_s, waiting_rule)
    ("S01", 1.8, 4.0, 9.0, 6.0, 0.0, None),
    ("S02", 6.4, 6.0, 12.0, None, 0.0, None),
    ("S03", 8.1, 6.0, 12.0, None, 0.0, None),
    ("S04", 5.2, 5.0, 11.0, None, 0.0, None),
    ("S05", 0.9, 3.0, 6.0, 4.0, 0.0, None),
    ("S06", 7.6, 6.0, 12.0, None, 0.0, None),
    ("S07", 9.3, 6.0, 12.5, None, 0.0, None),
    ("S08", 4.4, 5.0, 10.0, None, 0.0, None),
    ("S09", 0.7, 3.0, 6.0, 3.5, 900.0, "船闸等待窗口（研究情景）"),
    ("S10", 6.9, 6.0, 12.0, None, 0.0, None),
    ("S11", 8.8, 6.0, 12.0, None, 0.0, None),
    ("S12", 3.6, 5.0, 10.0, None, 0.0, None),
    ("S13", 0.8, 3.0, 6.0, 4.5, 0.0, None),
    ("S14", 7.1, 6.0, 12.0, None, 0.0, None),
    ("S15", 5.9, 5.0, 11.0, None, 0.0, None),
    ("S16", 2.4, 4.0, 9.0, None, 0.0, None),
    ("S17", 1.2, 3.0, 6.0, 4.0, 0.0, None),
)


def segment_constraints() -> tuple[SegmentConstraint, ...]:
    return tuple(
        SegmentConstraint(
            segment_id=segment_id, distance_km=distance, min_speed_kmh=low,
            max_speed_kmh=high, fixed_speed_kmh=fixed,
            phase_or_segment_type=("port_or_lock" if fixed is not None else "cruise"),
            waiting_rule=rule, waiting_duration_s=waiting,
            origin_entity_id=f"node-{index:02d}", destination_entity_id=f"node-{index + 1:02d}",
            source_refs=(DOC,), quality_flags=("simulation",))
        for index, (segment_id, distance, low, high, fixed, waiting, rule)
        in enumerate(SEGMENTS, start=1))


def time_grid(*, prediction_step_s: float = 5.0, verification_step_s: float = 5.0,
              max_acceleration_mps2: float = 0.15,
              max_deceleration_mps2: float = 0.20,
              decelerate_to_stop_at_end: bool = False) -> TimeGridSpec:
    """验收网格。

    ``decelerate_to_stop_at_end=False``：末段 S17 是固定航速的港内航段，其限速
    应按"通过速度"处理，否则减速到 0 的斜坡会低于该航段限速下限。
    """
    return TimeGridSpec(
        prediction_step_s=prediction_step_s,
        verification_step_s=verification_step_s,
        max_acceleration_mps2=max_acceleration_mps2,
        max_deceleration_mps2=max_deceleration_mps2,
        min_speed_kmh=3.0, max_speed_kmh=13.0,
        distance_tolerance_km=1e-6,
        decelerate_to_stop_at_end=decelerate_to_stop_at_end)


def request(*, remaining_time_s: float | None = 86400.0, objective: str = "min_energy",
            speed_penalty_lambda: float = 10.0,
            battery_budget_kwh: float | None = None,
            grid: TimeGridSpec | None = None) -> SpeedOptimizationRequest:
    return SpeedOptimizationRequest(
        segments=segment_constraints(),
        time_grid=grid or time_grid(),
        objective=objective,
        current_position_km=0.0,
        current_speed_kmh=6.0,
        remaining_time_s=remaining_time_s,
        planning_origin_at="2026-09-25T09:00:00+08:00",
        absolute_deadline_at=("2026-09-25T09:00:00+08:00" if remaining_time_s is None
                              else None),
        speed_penalty_lambda=speed_penalty_lambda,
        battery_budget_kwh=battery_budget_kwh,
        battery_budget_source_ref=(DEADLINE_SOURCE if battery_budget_kwh else None),
        history_ref="history.ref-001",
        environment_scenario_ref="scenario.env-001",
        scenario_id="scenario.de-acceptance",
        snapshot_id="snapshot.d1-fixture",
        request_revision=1,
        task_id="task.de-acceptance",
        call_id="tspeed-fixture",
        source_refs=(DOC,),
        assumptions=("S09 船闸等待 900s 为研究情景取值",),
    )


def analytic_predictor(*, coefficient: float = 0.055, idle_power_kw: float = 0.0,
                       environment_factor: float = 1.0,
                       model_version: str = "analytic-cubic-acceptance/1.0"
                       ) -> AnalyticPowerPredictor:
    """P = c·v³。c=0.055 kW/(km/h)³ 使 10 km/h 约 55 kW，量级接近内河纯电货船。"""
    return AnalyticPowerPredictor(coefficient=coefficient, idle_power_kw=idle_power_kw,
                                  environment_factor=environment_factor,
                                  model_version=model_version)


def model_spec(*, predictor: AnalyticPowerPredictor | None = None,
               max_step_s: float | None = None,
               demand_spec: DemandBuilderSpec | None = None,
               demand_limit_kw: float | None = None,
               energy_budget_kwh: float | None = None,
               grid: TimeGridSpec | None = None) -> EnergyModelSpec:
    active = grid or time_grid()
    # 展开步长默认等于预测步长：Tenergy 在一个预测区间内按恒定航速处理，
    # 展开得比预测更细不会提高预测精度。
    step = active.prediction_step_s if max_step_s is None else max_step_s
    return EnergyModelSpec(
        predictor=predictor or analytic_predictor(),
        grid=EvaluationGrid(
            prediction_step_s=active.prediction_step_s,
            verification_step_s=active.verification_step_s,
            distance_km_by_segment={segment.segment_id: segment.distance_km
                                    for segment in segment_constraints()}),
        environment_ref="scenario.env-001",
        history_ref="history.ref-001",
        max_step_s=step,
        demand_spec=demand_spec,
        demand_limit_kw=demand_limit_kw,
        energy_budget_kwh=energy_budget_kwh,
    )


def demand_spec(*, efficiency: float = 0.94, auxiliary_kw: float = 30.0,
                kind: str = "constant_efficiency") -> DemandBuilderSpec:
    """母线侧需求构建配置。

    efficiency=0.94 与 auxiliary_kw=30kW 是**具名研究假设**（沿用演示配置量级），
    必须等待 Tdata 提供有来源的转换参数后才能升为采用值。
    """
    conversion = ConversionModel(
        model_id="conversion.assumption-A2", kind=kind, efficiency=efficiency,
        source_ref=AUX_ASSUMPTION, note="驱动效率与损耗模型待现场标定")
    return DemandBuilderSpec(
        builder_id="demand-builder.A2",
        conversion=conversion,
        auxiliary=AuxiliaryModel(model_id="auxiliary.assumption-A3",
                                 sailing_kw=auxiliary_kw,
                                 waiting_kw=auxiliary_kw,
                                 berthing_kw=auxiliary_kw,
                                 source_ref=AUX_ASSUMPTION,
                                 note="辅助负载待现场确认，未默认为 0"))


def motor_side_only_spec() -> DemandBuilderSpec:
    """没有取得转换依据时的配置：母线侧保持 null，只做电机侧对照实验。"""
    return DemandBuilderSpec(
        builder_id="demand-builder.A2-no-conversion",
        conversion=None,
        auxiliary=AuxiliaryModel(model_id="auxiliary.assumption-A3", sailing_kw=30.0,
                                 waiting_kw=30.0, berthing_kw=30.0,
                                 source_ref=AUX_ASSUMPTION),
        reason="尚未取得电机侧→母线侧的转换依据（Tdata 未提供有来源效率参数）")
