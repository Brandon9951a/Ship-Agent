"""样例与 schema 验收：合法样例结构完整、非法样例确实被实现拒绝。"""

from __future__ import annotations

import json
from pathlib import Path

from research.contracts import EnergyBatchResult, PowerDemandSeries

EXAMPLES = Path(__file__).resolve().parents[1] / "schemas" / "examples"
SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"


def _load(*parts: str) -> dict:
    path = EXAMPLES.joinpath(*parts)
    if not path.exists():
        raise AssertionError(
            f"缺少样例 {path}；请先运行 python -m research.scripts.make_samples")
    return json.loads(path.read_text(encoding="utf-8"))


def test_schema_files_are_valid_json():
    schemas = sorted(SCHEMAS.glob("*.schema.json"))
    assert schemas, "必须提供机器可读 schema"
    for path in schemas:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload.get("$schema"), path.name
        assert payload.get("title"), path.name


def test_valid_tool_run_sample_has_envelope_fields():
    payload = _load("valid", "speed_optimization_success.tool_run.json")
    for field in ("schema_version", "tool_name", "call_id", "execution_status",
                  "outcome_code", "started_at", "finished_at", "diagnostics"):
        assert field in payload, field
    assert payload["outcome_code"] == "ok"
    assert payload["execution_status"] == "completed"
    assert payload["payload"]["search_status"] == "candidate_found"
    failed = [check for check in payload["payload"]["constraint_checks"]
              if not check["passed"]]
    assert failed == [], "选定方案的样例不得含未通过的检查"


def test_valid_motor_only_sample_marks_bus_side_unavailable():
    payload = _load("valid", "speed_optimization_motor_only.result.json")
    assert payload["bus_side_available"] is False
    assert payload["selected_demand_ref"] is None
    assert payload["objective_terms"]["energy_scope"] == "motor"
    assert any("电机侧对照实验" in note for note in payload["assumptions"])


def test_valid_unresolved_sample_has_no_selected_candidate():
    payload = _load("valid", "speed_optimization_search_unresolved.tool_run.json")
    assert payload["outcome_code"] == "search_unresolved"
    assert payload["execution_status"] == "completed"
    assert payload["payload"]["selected_candidate_id"] is None
    assert payload["payload"]["search_trace"]["diagnostic_candidates"], \
        "未解决样例必须带诊断候选"


def _power_demand_series(payload: dict) -> PowerDemandSeries:
    from research.contracts import PowerDemandPoint

    points = tuple(PowerDemandPoint(
        phase=item["phase"], start_offset_s=item["start_offset_s"],
        duration_s=item["duration_s"], motor_power_kw=item["motor_power_kw"],
        propulsion_bus_kw=item["propulsion_bus_kw"],
        auxiliary_bus_kw=item["auxiliary_bus_kw"],
        demand_bus_kw=item["demand_bus_kw"],
        conversion_model_ref=item.get("conversion_model_ref"),
        segment_id=item.get("segment_id"),
        planned_speed_kmh=item.get("planned_speed_kmh"),
        quality_flags=tuple(item.get("quality_flags") or ()))
        for item in payload["points"])
    return PowerDemandSeries(
        series_id=payload["series_id"], points=points,
        conversion_model_ref=payload["conversion_model_ref"],
        conversion_config_hash=payload["conversion_config_hash"],
        input_energy_ref=payload.get("input_energy_ref"),
        bus_side_available=payload["bus_side_available"],
        auxiliary_model_ref=payload.get("auxiliary_model_ref"),
        schema_version=payload["schema_version"],
        note=payload.get("note"))


def test_valid_power_demand_sample_is_self_consistent():
    payload = _load("valid", "power_demand_series.json")
    series = _power_demand_series(payload)
    assert series.bus_side_available is True
    assert series.demand_energy_kwh() is not None
    balance = _load("valid", "power_demand_energy_balance.json")
    assert balance["passed"] is True


def test_valid_energy_batch_samples_pair_by_candidate_id():
    request = _load("valid", "energy_batch_request.json")
    result = _load("valid", "energy_batch_result.json")
    requested = [item["candidate_id"] for item in request["candidates"]]
    returned = [item["candidate_id"] for item in result["results"]]
    assert sorted(requested) == sorted(returned), "候选 ID 必须逐一对齐"
    for item in result["results"]:
        for point in item["power_points"]:
            if isinstance(point, dict) and "motor_energy_kwh" in point:
                expected = point["predicted_motor_power_kw"] * point["duration_s"] / 3600.0
                assert abs(point["motor_energy_kwh"] - expected) < 1e-9


def test_valid_feedback_samples_distinguish_scope():
    provable = _load("valid", "management_feedback_provable_limit.json")
    soc_path = _load("valid", "management_feedback_soc_path.json")
    assert provable["failure_scope"] == "power_upper_bound_provable"
    assert provable["limit_source_ref"], "可证明上限必须给出来源"
    assert soc_path["failure_scope"] == "soc_path_dependent"
    assert not soc_path.get("limit_source_ref"), \
        "SOC 路径相关失败不得携带可证明上限的来源"


def test_invalid_sample_tool_run_mismatched_status_is_rejected():
    """该样例证明：结果码与执行状态的映射是固定的，不能自由组合。

    实现侧 ``execution_status_for`` 是唯一映射来源；样例里手工把 ok 写成 failed，
    因此它不属于任何由工具生成的合法信封。
    """
    payload = _load("invalid", "tool_run_ok_but_failed.json")
    assert payload["outcome_code"] == "ok"
    assert payload["execution_status"] == "failed"
    assert payload["_violation"], "非法样例必须写明它证明了哪条约定"
    from research.contracts import ExecutionStatus, OutcomeCode, execution_status_for

    assert execution_status_for(OutcomeCode.OK) is ExecutionStatus.COMPLETED
    assert payload["execution_status"] != execution_status_for(
        OutcomeCode(payload["outcome_code"])).value, \
        "样例必须确实偏离唯一映射，否则它不能作为反例"


def test_invalid_sample_energy_rule_is_rejected():
    payload = _load("invalid", "energy_result_energy_rule_broken.json")
    assert payload["_violation"]
    broken = payload["results"][0]["power_points"][0]
    expected = broken["predicted_motor_power_kw"] * broken["duration_s"] / 3600.0
    assert abs(broken["motor_energy_kwh"] - expected) > 1e-9, "该样例必须确实违反积分规则"
    from research.contracts import EnergyResultEnvelope, PowerPoint

    envelope = EnergyResultEnvelope(
        candidate_id="c1", outcome_code="ok",
        power_points=(PowerPoint(
            segment_id=broken["segment_id"], phase=broken["phase"],
            start_offset_s=broken["start_offset_s"], duration_s=broken["duration_s"],
            planned_speed_kmh=broken["planned_speed_kmh"],
            predicted_motor_power_kw=broken["predicted_motor_power_kw"],
            motor_energy_kwh=broken["motor_energy_kwh"]),))
    assert envelope.energies_match() is False, "实现必须识别该违规"


def test_invalid_sample_null_bus_power_is_rejected():
    payload = _load("invalid", "power_demand_null_but_available.json")
    assert payload["bus_side_available"] is True
    assert payload["_violation"]
    from research.contracts import PowerDemandPoint, PowerDemandSeries

    first = payload["points"][0]
    assert first["demand_bus_kw"] is None
    try:
        _power_demand_series(payload)
    except ValueError:
        return
    raise AssertionError("bus_side_available=true 且存在 null 区间必须被拒绝")


def test_invalid_sample_soc_path_as_limit_is_rejected():
    payload = _load("invalid", "management_feedback_soc_as_limit.json")
    assert payload["failure_scope"] == "soc_path_dependent"
    assert payload["_violation"]
    from research.contracts import ManagementFeedback

    feedback = ManagementFeedback(
        selected_candidate_id=payload["selected_candidate_id"],
        selected_demand_ref=payload["selected_demand_ref"],
        battery_config_hash=payload["battery_config_hash"],
        failure_scope=payload["failure_scope"],
        violated_rule=payload["violated_rule"])
    assert feedback.becomes_hard_constraint is False, \
        "SOC 路径相关失败不得被当成对新轨迹成立的硬限制"
