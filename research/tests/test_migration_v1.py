"""v1 → v2 迁移层验收：不补齐缺失字段、不把旧结果当作新接口结果。"""

from __future__ import annotations

from research.migration import (
    MIGRATION_LAYER_VERSION,
    migrate_management_plan,
    migrate_tool_response,
    version_banner,
)
from research.contracts import OutcomeCode


class _V1Source:
    def __init__(self, source_id, kind="document", confirmed=True):
        self.source_id = source_id
        self.kind = kind
        self.locator = "docs/v1.md"
        self.confirmed = confirmed
        self.note = None


class _V1Status:
    def __init__(self, value):
        self.value = value


class _V1Response:
    def __init__(self, status, payload=None, infeasible_type=None, tool="Tspeed",
                 missing_fields=None):
        self.tool = tool
        self.status = _V1Status(status)
        self.payload = payload
        self.infeasible_type = infeasible_type
        self.missing_fields = list(missing_fields or [])
        self.questions = []
        self.reason = None


class _V1Plan:
    def to_dict(self):
        return {"safe": True, "soc_initial": 0.85, "soc_final": 0.42,
                "capacity_kwh": 1567.85,
                "soc_trajectory": [{"segment_id": "S01", "soc": 0.8}]}


def test_ok_response_is_migrated_with_unsupported_fields_listed():
    response = _V1Response("ok", payload={
        "energy_scope": "total", "total_energy_kwh": 273.1,
        "energy_results": [{"segment_id": "S01", "power_kw": 55.0}]})
    migrated = migrate_tool_response(response, call_id="migrate-1")
    assert migrated.run.outcome is OutcomeCode.OK
    assert migrated.run.call_id == "migrate-1"
    assert migrated.run.tool_name.endswith("(migrated-v1)")
    assert migrated.run.model_or_algorithm_version == MIGRATION_LAYER_VERSION
    assert "energy_results（分段平均无法还原逐时功率）" in migrated.unsupported_fields
    assert "selected_demand_ref" in migrated.unsupported_fields
    codes = {item.code for item in migrated.run.diagnostics}
    assert "v1_information_insufficient" in codes
    assert migrated.run.payload["migrated_from"] == "v1"


def test_infeasible_is_downgraded_not_proved():
    response = _V1Response("infeasible", payload={"feasible": False},
                           infeasible_type=_V1Status("soc"))
    migrated = migrate_tool_response(response)
    assert migrated.run.outcome is OutcomeCode.INFEASIBLE_UNDER_ASSUMPTIONS, \
        "v1 infeasible 不得直接等同 v2 的证明性分类"
    codes = {item.code for item in migrated.run.diagnostics}
    assert "v1_infeasible_downgraded" in codes
    assert any("不得" in item or "不等同" in item for item in migrated.notes)


def test_need_clarification_maps_to_needs_input():
    response = _V1Response("need_clarification", missing_fields=["segments"])
    migrated = migrate_tool_response(response)
    assert migrated.run.outcome is OutcomeCode.NEEDS_INPUT
    assert migrated.run.execution_status == "completed", \
        "缺参是正常返回的工程结果，不是执行失败"


def test_management_plan_is_not_splittable():
    migrated = migrate_management_plan(_V1Plan(), call_id="migrate-mgmt")
    assert migrated.run.outcome is OutcomeCode.NOT_SUPPORTED
    for field in ("battery_1_soc_ref", "battery_2_soc_ref", "mode_trajectory_ref"):
        assert field in migrated.unsupported_fields
    codes = {item.code for item in migrated.run.diagnostics}
    assert "merged_battery_state_not_splittable" in codes
    assert migrated.run.payload["migrated_from"] == "v1"


def test_migration_banner_states_no_silent_upgrade():
    banner = version_banner()
    assert "v1" in banner and "不补齐" in banner
