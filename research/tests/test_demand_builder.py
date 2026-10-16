"""共同需求构建器验收：单一转换位置、null 传播、能量守恒与来源强制。"""

from __future__ import annotations

import dataclasses

from research.contracts import EnergyCoverage, OutcomeCode, SegmentSummary, SourceRef
from research.demand import (
    AuxiliaryModel,
    ConversionModel,
    DemandBuilderSpec,
    build_power_demand,
    bus_power_limit_check,
    energy_balance_check,
)
from research.tests import fixtures as f
from research.tspeed.profile import TrajectoryProfile
from research.tspeed.trajectory import expand_trajectory


def _outcome(segments: int = 3, speed: float = 8.0, *, waiting: bool = True,
             prediction_step_s: float = 5.0):
    """构造一个真实展开 + 真实预测的候选结果，供需求构建器消费。"""
    request = f.request(grid=f.time_grid(prediction_step_s=prediction_step_s))
    request = dataclasses.replace(request,
                                  segments=request.segments[:segments])
    vector = [speed] * len(request.optimizable_segment_ids)
    expanded = expand_trajectory(vector, request=request, max_step_s=60.0)
    profile = TrajectoryProfile.from_expansion(expanded)
    spec = f.model_spec(grid=request.time_grid)
    predictor = spec.predictor
    # 让预测器缓存上下文就绪：先跑一次同配置的批量请求
    from research.contracts import CandidateTrajectory, EnergyBatchRequest

    candidate = CandidateTrajectory(
        f"probe-{segments}-{speed}", expanded.to_trajectory_points())
    predictor.predict(EnergyBatchRequest(
        candidates=(candidate,), history_ref=spec.history_ref,
        environment_scenario_ref=spec.environment_ref,
        model_version=predictor.model_version,
        integration_rule=predictor.integration_rule), grid=spec.grid)
    outcome = predictor.predict_profile(profile, candidate_id="cand-1", grid=spec.grid)
    return outcome, expanded


def test_named_assumption_conversion_is_labelled():
    outcome, _ = _outcome()
    spec = f.demand_spec(efficiency=0.94, auxiliary_kw=30.0)
    series = build_power_demand(outcome, spec=spec, series_id="demand-1")
    assert series.bus_side_available is True
    assert series.conversion_model_ref == "conversion.assumption-A2"
    assert series.conversion_config_hash == spec.config_hash()
    codes = {item.code for item in series.diagnostics}
    assert "conversion_is_named_assumption" in codes, \
        "具名假设必须如实标注，不能当作实船标定"
    for point in series.points:
        if point.motor_power_kw is None:
            assert point.demand_bus_kw is None
            continue
        assert abs(point.propulsion_bus_kw - point.motor_power_kw / 0.94) < 1e-9
        assert abs(point.demand_bus_kw - (point.propulsion_bus_kw + 30.0)) < 1e-9


def test_missing_conversion_keeps_bus_side_null():
    outcome, _ = _outcome()
    spec = f.motor_side_only_spec()
    series = build_power_demand(outcome, spec=spec, series_id="demand-2")
    assert series.bus_side_available is False
    assert series.demand_energy_kwh() is None
    assert series.config_hash()  # 配置哈希仍可用，便于版本核对
    assert all(point.demand_bus_kw is None for point in series.points)
    assert all(point.propulsion_bus_kw is None for point in series.points)
    assert all("missing" in point.quality_flags for point in series.points)
    codes = {item.code for item in series.diagnostics}
    assert "conversion_basis_missing" in codes
    balance = energy_balance_check(series)
    assert balance["applicable"] is False, "母线侧无依据时不得给出守恒通过结论"


def test_energy_conservation_passes_for_constant_efficiency():
    outcome, _ = _outcome()
    series = build_power_demand(outcome, spec=f.demand_spec(), series_id="demand-3")
    balance = energy_balance_check(series)
    assert balance["applicable"] is True
    assert balance["passed"] is True
    assert abs(balance["residual_kwh"]) <= 1e-9


def test_energy_conservation_passes_for_affine_loss():
    outcome, _ = _outcome()
    spec = f.demand_spec(efficiency=0.92, auxiliary_kw=25.0, kind="affine_loss")
    conversion = dataclasses.replace(spec.conversion, offset_kw=1.5)
    series = build_power_demand(outcome, spec=dataclasses.replace(spec, conversion=conversion),
                                series_id="demand-4")
    balance = energy_balance_check(series)
    assert balance["passed"] is True


def test_auxiliary_power_splits_by_phase():
    outcome, _ = _outcome(segments=9)  # 前 9 段含 S09 的船闸等待
    spec = f.demand_spec(auxiliary_kw=30.0)
    series = build_power_demand(outcome, spec=spec, series_id="demand-5")
    phases = {point.phase: point.auxiliary_bus_kw for point in series.points}
    assert phases.get("waiting") == 30.0
    assert phases.get("sailing") == 30.0
    waiting = [point for point in series.points if point.phase == "waiting"]
    assert waiting, "S09 的船闸等待必须出现在时间轴上"
    assert all(point.motor_power_kw == 0.0 for point in waiting), \
        "等待阶段主推进电机功率为 0（模型规则），不是缺测"


def test_efficiency_must_be_below_one():
    source = SourceRef(source_id="a", kind="assumption")
    for value in (1.0, 1.2, 0.0, -0.5):
        try:
            ConversionModel(model_id="m", kind="constant_efficiency",
                            efficiency=value, source_ref=source)
        except ValueError:
            continue
        raise AssertionError(f"efficiency={value} 必须被拒绝（不得默认为 1）")


def test_auxiliary_model_requires_source():
    try:
        AuxiliaryModel(model_id="a", sailing_kw=0.0, waiting_kw=0.0, berthing_kw=0.0)
    except ValueError:
        return
    raise AssertionError("未知辅助功率不得默认为 0，必须给出 source_ref")


def test_builder_without_conversion_requires_reason():
    try:
        DemandBuilderSpec(builder_id="b", conversion=None,
                          auxiliary=f.demand_spec().auxiliary)
    except ValueError:
        return
    raise AssertionError("未取得转换依据时必须写明 reason")


def test_power_limit_check_is_not_applicable_without_bus_side():
    outcome, _ = _outcome()
    series = build_power_demand(outcome, spec=f.motor_side_only_spec(),
                                series_id="demand-6")
    result = bus_power_limit_check(series, 400.0)
    assert result["applicable"] is False, \
        "母线侧无依据时不得用电机侧数值代替功率上限检查"


def test_power_limit_check_reports_peak():
    outcome, _ = _outcome()
    series = build_power_demand(outcome, spec=f.demand_spec(), series_id="demand-7")
    over = bus_power_limit_check(series, 1.0)
    assert over["applicable"] is True and over["passed"] is False
    generous = bus_power_limit_check(series, 10_000.0)
    assert generous["passed"] is True
    assert generous["peak_demand_kw"] == series.peak_demand_kw()


def test_series_requires_conversion_reference():
    outcome, _ = _outcome()
    series = build_power_demand(outcome, spec=f.demand_spec(), series_id="demand-8")
    try:
        dataclasses.replace(series, conversion_model_ref="")
    except ValueError:
        return
    raise AssertionError("conversion_model_ref 必须非空")


def test_bus_side_available_requires_all_points():
    outcome, _ = _outcome()
    series = build_power_demand(outcome, spec=f.demand_spec(), series_id="demand-9")
    broken = list(series.points)
    broken[0] = dataclasses.replace(broken[0], demand_bus_kw=None)
    try:
        dataclasses.replace(series, points=tuple(broken))
    except ValueError:
        return
    raise AssertionError("bus_side_available=true 时不允许出现 null 区间")


def test_coverage_and_summary_fields_present():
    outcome, _ = _outcome()
    assert isinstance(outcome.total_summary, SegmentSummary)
    assert isinstance(outcome.coverage, EnergyCoverage)
    assert outcome.outcome_code == OutcomeCode.OK.value
    assert abs(outcome.coverage.valid_fraction - 1.0) < 1e-12
    assert outcome.total_summary.predicted_motor_energy_kwh > 0.0
