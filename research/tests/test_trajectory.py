"""轨迹展开验收：距离/时间一致、加减速上限、固定航段、等待阶段与错误分类。"""

from __future__ import annotations

import dataclasses

from research.contracts import SegmentConstraint, TimeGridSpec
from research.tests import fixtures as f
from research.tspeed.trajectory import expand_trajectory, plan_segment_phases


def _expand(vector_speed: float = 8.0, *, segments: int | None = None, **kwargs):
    request = f.request(grid=f.time_grid(**kwargs))
    if segments is not None:
        request = dataclasses.replace(request, segments=request.segments[:segments])
    vector = [vector_speed] * len(request.optimizable_segment_ids)
    return request, expand_trajectory(vector, request=request, max_step_s=60.0)


def test_distance_matches_requested():
    _, expanded = _expand(8.0)
    assert expanded.distance_consistent, expanded.distance_residual_km
    assert abs(expanded.distance_residual_km) < 1e-9
    for fact in expanded.segment_facts:
        assert fact.distance_consistent, fact.segment_id


def test_interval_durations_positive_and_ordered():
    _, expanded = _expand(7.5)
    previous_end = 0.0
    for interval in expanded.ordered_intervals():
        _, interval = interval
        assert interval.duration_s > 0
        assert interval.start_offset_s >= previous_end - 1e-9, "区间不得重叠"
        assert abs(interval.start_offset_s - previous_end) < 1e-6, "区间不得有未解释间隙"
        previous_end = interval.end_offset_s


def test_acceleration_within_limits():
    request, expanded = _expand(3.0, max_acceleration_mps2=0.10,
                                max_deceleration_mps2=0.10)
    assert expanded.audit_acceleration(request.time_grid) == []
    _, fast = _expand(12.0, max_acceleration_mps2=0.10, max_deceleration_mps2=0.10)
    assert fast.audit_acceleration(request.time_grid) == []


def test_fixed_segments_keep_their_cruise_speed():
    """固定航段的巡航区间必须等于其固定航速；过渡斜坡只允许在限速区间内取值。"""
    request, expanded = _expand(11.0)
    fixed_speeds = {segment.segment_id: segment.fixed_speed_kmh
                    for segment in request.segments if not segment.optimizable}
    assert fixed_speeds, "夹具必须包含固定航速航段（港口/船闸）"
    for segment_id, speed in fixed_speeds.items():
        fact = expanded.segment_fact(segment_id)
        cruise = [interval for interval in fact.sampled if interval.kind == "cruise"]
        assert cruise, f"{segment_id} 必须有巡航区间"
        for interval in cruise:
            assert abs(interval.planned_speed_kmh - speed) < 1e-9, \
                f"{segment_id} 固定航速被改写"
        for interval in fact.sampled:
            assert interval.planned_speed_kmh >= min(speed, 3.0) - 1e-9

def test_speed_respects_segment_limits():
    request, expanded = _expand(9.0)
    for segment in request.segments:
        fact = expanded.segment_fact(segment.segment_id)
        for interval in fact.sampled:
            if segment.max_speed_kmh is not None:
                assert interval.planned_speed_kmh <= segment.max_speed_kmh + 1e-9
            if segment.min_speed_kmh is not None and interval.kind == "cruise":
                assert interval.planned_speed_kmh >= segment.min_speed_kmh - 1e-9


def test_waiting_is_listed_separately():
    request, expanded = _expand(8.0, segments=9)
    assert expanded.waiting_duration_s == 900.0
    phases = [interval.phase for _, interval in expanded.ordered_intervals()]
    assert "waiting" in phases
    assert expanded.total_duration_s == expanded.sailing_duration_s \
        + expanded.waiting_duration_s


def test_decision_vector_length_is_checked():
    request, _ = _expand()
    try:
        expand_trajectory([8.0] * (len(request.optimizable_segment_ids) + 1),
                          request=request)
    except ValueError:
        return
    raise AssertionError("决策向量长度不匹配必须被拒绝")


def test_current_speed_is_required():
    request, _ = _expand()
    patched = dataclasses.replace(request, current_speed_kmh=None)
    try:
        expand_trajectory([8.0] * len(patched.optimizable_segment_ids), request=patched)
    except ValueError:
        return
    raise AssertionError("缺少当前航速时必须取缺参，而不是默认为 0")


def test_short_segment_reports_speed_limit_conflict():
    """距离过短、无法达到限速下限的航段必须如实报告，而不是造假速度。"""
    grid = f.time_grid(max_acceleration_mps2=0.05, max_deceleration_mps2=0.05)
    segment = SegmentConstraint(segment_id="T01", distance_km=0.02, min_speed_kmh=6.0,
                                max_speed_kmh=12.0)
    phases, violations = plan_segment_phases(
        segment, cruise_speed_kmh=10.0, entry_speed_kmh=0.0, exit_speed_kmh=0.0,
        grid=grid, fixed=False, clamp_exit_to_floor=False)
    assert violations, "过短航段必须产生违规而不是静默接受"
    codes = {item.code for item in violations}
    assert "distance_too_short_for_speed_limit" in codes
    total_distance = sum(phase.distance_km for phase in phases)
    assert abs(total_distance - segment.distance_km) < 1e-9, \
        "即使报告违规，展开的距离仍须等于航段距离"


def test_peak_speed_solution_covers_distance_exactly():
    grid = f.time_grid(max_acceleration_mps2=0.30, max_deceleration_mps2=0.30)
    segment = SegmentConstraint(segment_id="T02", distance_km=0.15, min_speed_kmh=3.0,
                                max_speed_kmh=12.0)
    phases, violations = plan_segment_phases(
        segment, cruise_speed_kmh=12.0, entry_speed_kmh=3.0, exit_speed_kmh=3.0,
        grid=grid, fixed=False)
    assert violations == []
    assert abs(sum(phase.distance_km for phase in phases) - 0.15) < 1e-9


def test_reversed_segment_limits_rejected():
    try:
        SegmentConstraint(segment_id="X", distance_km=1.0, min_speed_kmh=10.0,
                          max_speed_kmh=5.0)
    except ValueError:
        return
    raise AssertionError("min > max 必须被拒绝")


def test_waiting_requires_rule_source():
    try:
        SegmentConstraint(segment_id="X", distance_km=1.0, min_speed_kmh=3.0,
                          max_speed_kmh=8.0, waiting_duration_s=600.0)
    except ValueError:
        return
    raise AssertionError("给出等待时间必须同时给出规则/来源说明")


def test_no_deceleration_ramp_is_dropped():
    """默认按"终点减速停车"展开时，末段必须包含减速斜坡。"""
    request = f.request(grid=f.time_grid(decelerate_to_stop_at_end=True),
                        remaining_time_s=None)
    expanded = expand_trajectory([12.0] * len(request.optimizable_segment_ids),
                                 request=request, max_step_s=60.0)
    last = expanded.segment_facts[-1]
    assert last.exit_speed_kmh == 0.0
    kinds = [interval.kind for interval in last.sampled]
    assert "decel" in kinds, "末段必须有减速阶段，不能以巡航速度直接到港"


def test_stop_speed_is_explicit_not_silent():
    """关闭"终点停车"后末段出口速度取该航段通过速度下限，而不是悄悄按 0 处理。"""
    request = f.request(grid=f.time_grid(decelerate_to_stop_at_end=False))
    expanded = expand_trajectory([12.0] * len(request.optimizable_segment_ids),
                                 request=request, max_step_s=60.0)
    last = expanded.segment_facts[-1]
    segment = request.segments[-1]
    assert last.exit_speed_kmh == segment.fixed_speed_kmh
    assert all(interval.planned_speed_kmh >= segment.min_speed_kmh - 1e-9
               for interval in last.sampled)


def test_rolling_replan_does_not_touch_past_segments():
    """滚动重规划只接收剩余航段；已执行航段不出现在请求里。"""
    request, expanded = _expand(8.0)
    remaining = dataclasses.replace(request, segments=request.segments[4:], current_speed_kmh=7.0)
    vector = [8.0] * len(remaining.optimizable_segment_ids)
    replanned = expand_trajectory(vector, request=remaining, max_step_s=60.0)
    ids = [fact.segment_id for fact in replanned.segment_facts]
    assert ids == [segment.segment_id for segment in request.segments[4:]]
    assert replanned.segment_facts[0].start_offset_s == 0.0, \
        "重规划的时间轴从当前位置重新起算，不回滚已执行部分"
    assert replanned.segment_facts[0].entry_speed_kmh == 7.0


def test_single_segment_voyage():
    request = f.request()
    request = dataclasses.replace(request, segments=request.segments[1:2],
                                  current_speed_kmh=7.0)
    expanded = expand_trajectory([9.0], request=request, max_step_s=60.0)
    assert expanded.distance_consistent
    assert expanded.total_duration_s > 0


def test_grid_tolerance_parameter_is_used():
    tight = TimeGridSpec(prediction_step_s=5.0, verification_step_s=5.0,
                         max_acceleration_mps2=0.15, max_deceleration_mps2=0.2,
                         min_speed_kmh=3.0, max_speed_kmh=13.0,
                         distance_tolerance_km=1e-9)
    request = f.request(grid=tight)
    expanded = expand_trajectory([8.0] * len(request.optimizable_segment_ids),
                                 request=request, max_step_s=60.0)
    assert expanded.distance_consistent
