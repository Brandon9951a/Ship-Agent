"""Tests for the infeasible-choice translator (D4 task 14)."""

from pathlib import Path

from core.intent_translator import (
    DIRECTIONS_BY_TYPE,
    build_boundary_diagnostics,
    enumerate_options,
    translate_choice,
)
from schemas.types import (
    DataContext, EnergyResult, InfeasibleType, OptimizationResult, Segment,
    VoyageRequest,
)
from tools.tdata import load_config, tdata
from tools.tenergy import EnergyModel, run_tenergy
from tools.tseg import segment
from tools.tspeed import run_tspeed

ROOT = Path(__file__).resolve().parents[1]


def _configs() -> dict:
    return {
        "route": load_config(ROOT / "configs/route_facts.yaml"),
        "aliases": load_config(ROOT / "configs/aliases.yaml"),
        "vessel": load_config(ROOT / "configs/vessel_facts.yaml"),
        "limits": load_config(ROOT / "configs/limits.yaml"),
        "policy": load_config(ROOT / "configs/demo_policy.yaml"),
    }


def _run(origin, destination, soc_initial, max_duration_h):
    """Run Tdata→Tseg→Tenergy→Tspeed on a sub-route and return the pieces."""
    configs = _configs()
    request = VoyageRequest(
        origin=origin, destination=destination,
        max_duration_h=max_duration_h, soc_initial=soc_initial, load_state="半载",
    )
    data = DataContext.from_dict(tdata(
        request, route_config=configs["route"], aliases_config=configs["aliases"],
        vessel_config=configs["vessel"], limits_config=configs["limits"],
        demo_policy_config=configs["policy"],
    ).payload)
    segments = [Segment.from_dict(item) for item in segment(
        request, data, route_config=configs["route"], aliases_config=configs["aliases"],
        demo_policy_config=configs["policy"],
    ).payload["segments"]]
    model = EnergyModel(
        model_id="test-cubic", coefficient_kw_per_kmh3=0.06559425611908812,
        auxiliary_power_kw=30.0, energy_scope="propulsion", usage="synthetic_demo",
    )
    candidates = [EnergyResult.from_dict(item) for item in run_tenergy(
        segments, configs["policy"]["voyage_demo"]["candidate_speeds_kmh"], model,
    ).payload["candidate_results"]]
    speed = run_tspeed(request, segments, candidates, data.vessel)
    return request, data, segments, candidates, speed


def test_directions_mapping_is_rule_based():
    assert DIRECTIONS_BY_TYPE[InfeasibleType.TIME] == [
        "accept_late", "adjust_departure", "shorten_route", "give_up",
    ]
    assert "recharge" in DIRECTIONS_BY_TYPE[InfeasibleType.SOC]
    assert "accept_lower_soc" not in {
        direction for directions in DIRECTIONS_BY_TYPE.values() for direction in directions
    }
    assert "slow_down" not in DIRECTIONS_BY_TYPE[InfeasibleType.SOC]
    assert DIRECTIONS_BY_TYPE[InfeasibleType.POWER] == ["shorten_route", "give_up"]


def test_time_infeasible_offers_accept_late_with_quantified_shortfall():
    request, data, segments, candidates, speed = _run(
        "平顶山港", "马湾船闸", 0.85, 4.0,
    )
    assert speed.status.value == "infeasible"
    assert speed.infeasible_type == InfeasibleType.TIME
    optimization = OptimizationResult.from_dict(speed.payload)
    options = enumerate_options(optimization, request, data.vessel, segments, candidates, 30.0)
    by_dir = {item.direction: item for item in options}
    assert "accept_late" in by_dir
    assert by_dir["accept_late"].quantified["time_shortfall_h"] > 0
    assert by_dir["accept_late"].verified is True
    assert by_dir["accept_late"].modification["max_duration_h"] > 4.0
    assert "give_up" in by_dir


def test_soc_infeasible_offers_operator_actions_without_lowering_safety_floor():
    request, data, segments, candidates, speed = _run(
        "平顶山港", "马湾船闸", 0.30, 10.0,
    )
    assert speed.infeasible_type == InfeasibleType.SOC
    optimization = OptimizationResult.from_dict(speed.payload)
    options = enumerate_options(optimization, request, data.vessel, segments, candidates, 30.0)
    by_dir = {item.direction: item for item in options}
    assert "recharge" in by_dir
    assert "shorten_route" not in by_dir
    assert "accept_lower_soc" not in by_dir
    assert by_dir["recharge"].quantified["minimum_charge_required_kwh"] > 0
    assert by_dir["recharge"].modification["soc_initial"] > request.soc_initial
    assert all(
        item.direction == "give_up" or (item.verified and item.modification)
        for item in options
    )


def test_single_segment_low_soc_omits_unverified_slow_and_subroute_advice():
    request, data, segments, candidates, speed = _run(
        "平顶山港", "军李船闸", 0.31, 6.0,
    )
    optimization = OptimizationResult.from_dict(speed.payload)
    options = enumerate_options(optimization, request, data.vessel, segments, candidates, 30.0)
    assert [item.direction for item in options] == ["recharge", "give_up"]
    recharge = options[0]
    assert recharge.modification["soc_initial"] == 0.325
    assert recharge.preview["duration_h"] < 6.0
    assert recharge.preview["soc_final"] >= data.vessel.soc_min

    _, _, _, _, rerun = _run(
        "平顶山港", "军李船闸", recharge.modification["soc_initial"], 6.0,
    )
    assert rerun.status.value == "ok"


def test_translate_accept_late_extends_only_the_duration_constraint():
    request, data, segments, candidates, speed = _run(
        "平顶山港", "马湾船闸", 0.85, 4.0,
    )
    optimization = OptimizationResult.from_dict(speed.payload)
    diagnostics = build_boundary_diagnostics(segments, candidates, data.vessel, request, 30.0)
    modified = translate_choice("accept_late", request, diagnostics=diagnostics)
    raw_duration = request.max_duration_h + diagnostics["time_shortfall_h"]
    assert modified.max_duration_h >= raw_duration
    assert modified.max_duration_h - raw_duration < 0.001
    assert modified.origin == request.origin
    assert modified.soc_initial == request.soc_initial


def test_translate_give_up_returns_unchanged_request():
    request, _, _, _, _ = _run("平顶山港", "马湾船闸", 0.85, 4.0)
    assert translate_choice("give_up", request) == request


def test_translate_recharge_writes_computed_soc_and_route_change_needs_modification():
    request, data, segments, candidates, speed = _run(
        "平顶山港", "马湾船闸", 0.30, 10.0,
    )
    optimization = OptimizationResult.from_dict(speed.payload)
    options = enumerate_options(optimization, request, data.vessel, segments, candidates, 30.0)
    diagnostics = build_boundary_diagnostics(
        segments, candidates, data.vessel, request, 30.0,
    )
    modified = translate_choice("recharge", request, diagnostics=diagnostics)
    assert modified.soc_initial > request.soc_initial
    try:
        translate_choice("shorten_route", request, diagnostics=diagnostics)
    except ValueError:
        pass
    else:
        raise AssertionError("shorten_route 必须使用后端返回的具体端点 modification")
