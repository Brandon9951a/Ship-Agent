"""v1（``schemas/types.py`` 演示链路）→ v2 的显式迁移层。

接口 v2 第 8 节第 4 条：旧接口通过明确的迁移层过渡，缺少新接口必需信息时返回
不支持或缺参，**不以默认值补齐**：

* 旧 ``EnergyResult`` 只有分段平均功率，不能逆推出逐时功率；
* 旧 ``OptimizationResult`` 只有航速列表与汇总，没有逐时功率与选取轨迹工件；
* 旧 ``ManagementPlan`` 只有合并 SOC，不能拆成两组独立状态。

迁移层只做字段搬运与如实标注，不发明数值。
"""

from __future__ import annotations

from dataclasses import dataclass

from research.contracts import (
    CONTRACT_SCHEMA_VERSION,
    Diagnostic,
    OutcomeCode,
    SourceRef,
    ToolRun,
    finish_run,
    start_run,
)

MIGRATION_LAYER_VERSION = "v1-to-v2/1.0"

# 旧 v1 状态 → v2 结果码的映射；`infeasible` 不能直接等同证明性分类。
V1_STATUS_MAP = {
    "ok": OutcomeCode.OK,
    "need_clarification": OutcomeCode.NEEDS_INPUT,
    "invalid_input": OutcomeCode.INVALID_INPUT,
    "infeasible": OutcomeCode.INFEASIBLE_UNDER_ASSUMPTIONS,
    "failed": OutcomeCode.TOOL_ERROR,
    "awaiting_choice": OutcomeCode.NEEDS_INPUT,
}


@dataclass(frozen=True)
class MigrationResult:
    """迁移结果：v2 信封 + 无法补齐的字段清单（不填默认值）。"""

    run: ToolRun
    unsupported_fields: tuple[str, ...]
    notes: tuple[str, ...]


def _source_from_v1(source) -> SourceRef | None:
    if source is None:
        return None
    kind = getattr(source, "kind", None)
    mapping = {"document": "document", "user": "user", "measurement": "measurement",
               "assumption": "assumption", "tool": "tool"}
    return SourceRef(
        source_id=getattr(source, "source_id", "v1.source"),
        kind=mapping.get(kind, "tool"),
        locator=getattr(source, "locator", None),
        confirmed=bool(getattr(source, "confirmed", False)),
        note=getattr(source, "note", None),
        mode="replay",
    )


def migrate_tool_response(response, *, call_id: str | None = None,
                          parent_call_id: str | None = None) -> MigrationResult:
    """把 v1 ``ToolResponse`` 迁到 v2 信封。

    分段平均能耗无法还原为逐时功率，因此 ``selected_demand_ref``、
    ``selected_energy_ref``、``arrival_at``（无绝对基准时）等字段不填。
    """
    status = getattr(response, "status", None)
    status_value = getattr(status, "value", status) or "failed"
    outcome = V1_STATUS_MAP.get(status_value, OutcomeCode.TOOL_ERROR)
    tool = getattr(response, "tool", "unknown") or "unknown"
    payload = getattr(response, "payload", None) or {}
    unsupported: list[str] = []
    notes: list[str] = []

    if status_value == "infeasible":
        infeasible_type = getattr(response, "infeasible_type", None)
        notes.append(
            "v1 `infeasible` 仅表示所给候选网格内不可行；已降级为 "
            "`infeasible_under_assumptions`，不等同 v2 的证明性分类"
            f"（v1 infeasible_type={getattr(infeasible_type, 'value', infeasible_type)}）")
    if payload:
        if "energy_results" in payload:
            unsupported.append("energy_results（分段平均无法还原逐时功率）")
        if not payload.get("checks"):
            unsupported.append("constraint_checks（v1 未提供独立检查项）")
        for field in ("selected_candidate_id", "selected_trajectory_ref",
                      "selected_demand_ref", "search_trace_ref"):
            if not payload.get(field):
                unsupported.append(field)
    diagnostics: list[Diagnostic] = []
    if unsupported:
        diagnostics.append(Diagnostic(
            stage="migration", code="v1_information_insufficient",
            detail="v1 结果无法补齐 v2 必需字段：" + "、".join(unsupported),
            field_ref="payload", retryable=False))
    if outcome is OutcomeCode.INFEASIBLE_UNDER_ASSUMPTIONS:
        diagnostics.append(Diagnostic(
            stage="migration", code="v1_infeasible_downgraded",
            detail="v1 infeasible 已降级为「已记录假设下不可行」，需结合证据重新判定",
            field_ref="outcome_code", retryable=True))

    envelope = start_run(
        tool_name=f"{tool}(migrated-v1)",
        call_id=call_id or f"migrate-{tool}",
        parent_call_id=parent_call_id,
        tool_version=MIGRATION_LAYER_VERSION)
    # 迁移层不生成选定方案：payload 原样保留，但明确标注来源为 v1。
    payload_out = dict(payload)
    payload_out["migrated_from"] = "v1"
    payload_out["migration_layer_version"] = MIGRATION_LAYER_VERSION
    payload_out["unsupported_fields"] = unsupported
    run = finish_run(envelope, outcome=outcome, diagnostics=diagnostics,
                     payload=payload_out,
                     execution_status=None)
    return MigrationResult(run=run, unsupported_fields=tuple(unsupported),
                           notes=tuple(notes))


def migrate_management_plan(plan, *, call_id: str | None = None) -> MigrationResult:
    """旧 ``ManagementPlan``：合并 SOC 不能拆成两组独立状态。"""
    payload = plan.to_dict() if hasattr(plan, "to_dict") else dict(plan or {})
    unsupported = ["battery_1_power_ref", "battery_2_power_ref",
                   "battery_1_soc_ref", "battery_2_soc_ref",
                   "mode_trajectory_ref"]
    diagnostics = [Diagnostic(
        stage="migration", code="merged_battery_state_not_splittable",
        detail="v1 ManagementPlan 只有合并 SOC 与合并容量，不能反推两组电池实测状态",
        field_ref="soc_trajectory", retryable=False)]
    envelope = start_run(tool_name="Tmanagement(migrated-v1)",
                         call_id=call_id or "migrate-tmanagement",
                         tool_version=MIGRATION_LAYER_VERSION)
    payload["migrated_from"] = "v1"
    payload["unsupported_fields"] = unsupported
    run = finish_run(envelope, outcome=OutcomeCode.NOT_SUPPORTED,
                     diagnostics=diagnostics, payload=payload)
    return MigrationResult(run=run, unsupported_fields=tuple(unsupported),
                           notes=("合并 SOC 迁移后仅可用于演示对比，"
                                  "不得作为两组电池的运行状态",))


def version_banner() -> str:
    """迁移说明：调用方应在日志中写明 v1 与 v2 未混用。"""
    return (f"{MIGRATION_LAYER_VERSION}（v1 演示链路 → {CONTRACT_SCHEMA_VERSION}）；"
            "v1 分段平均与合并 SOC 一律不补齐为 v2 逐时/两组字段")
