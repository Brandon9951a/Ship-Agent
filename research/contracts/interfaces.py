"""v2 状态信封：``ToolRun`` 与失败语义枚举。

接口 v2 第 3 节规定：``execution_status`` 只区分 completed/failed/cancelled，
工程结果由 ``outcome_code`` 表达。``ok`` 只表示该工具按约定完成，
不自动等于完整航行方案安全。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .diagnostics import Diagnostic
from .envelope import (
    CONTRACT_SCHEMA_VERSION,
    ContractModel,
    utc_now_iso,
)


class ExecutionStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OutcomeCode(str, Enum):
    """接口 v2 第 3 节失败语义表。"""

    OK = "ok"
    NEEDS_INPUT = "needs_input"
    INVALID_INPUT = "invalid_input"
    MODEL_OUT_OF_DOMAIN = "model_out_of_domain"
    SEARCH_UNRESOLVED = "search_unresolved"
    INFEASIBLE_UNDER_ASSUMPTIONS = "infeasible_under_assumptions"
    TOOL_ERROR = "tool_error"
    GRID_UNRESOLVED = "grid_unresolved"          # Tmanagement/FDP 专用
    NOT_SUPPORTED = "not_supported"              # 未实现目标/补能试算等显式不支持


class Phase(str, Enum):
    """时序区间阶段；非航行阶段速度可为 0 或明确未知。"""

    SAILING = "sailing"
    WAITING = "waiting"
    BERTHING = "berthing"
    CHARGING = "charging"


class SearchStatus(str, Enum):
    """``SpeedOptimizationResult.search_status``（接口 v2 第 6 节）。"""

    CANDIDATE_FOUND = "candidate_found"
    SEARCH_UNRESOLVED = "search_unresolved"
    INPUT_INVALID = "input_invalid"
    MODEL_OUT_OF_DOMAIN = "model_out_of_domain"
    TOOL_ERROR = "tool_error"


# 结果码 → 工具执行状态的默认映射，避免各处自行拼装。
# ``needs_input`` / ``invalid_input`` / ``not_supported`` 是**正常返回的工程结果**
# （工具按约定给出了诊断），不是执行失败；只有真正的运行异常才是 failed。
_OUTCOME_TO_EXECUTION = {
    OutcomeCode.OK: ExecutionStatus.COMPLETED,
    OutcomeCode.SEARCH_UNRESOLVED: ExecutionStatus.COMPLETED,
    OutcomeCode.INFEASIBLE_UNDER_ASSUMPTIONS: ExecutionStatus.COMPLETED,
    OutcomeCode.GRID_UNRESOLVED: ExecutionStatus.COMPLETED,
    OutcomeCode.NEEDS_INPUT: ExecutionStatus.COMPLETED,
    OutcomeCode.INVALID_INPUT: ExecutionStatus.COMPLETED,
    OutcomeCode.NOT_SUPPORTED: ExecutionStatus.COMPLETED,
    OutcomeCode.MODEL_OUT_OF_DOMAIN: ExecutionStatus.FAILED,
    OutcomeCode.TOOL_ERROR: ExecutionStatus.FAILED,
}

_OUTCOME_TO_SEARCH_STATUS = {
    OutcomeCode.OK: SearchStatus.CANDIDATE_FOUND,
    OutcomeCode.SEARCH_UNRESOLVED: SearchStatus.SEARCH_UNRESOLVED,
    OutcomeCode.INFEASIBLE_UNDER_ASSUMPTIONS: SearchStatus.SEARCH_UNRESOLVED,
    OutcomeCode.INVALID_INPUT: SearchStatus.INPUT_INVALID,
    OutcomeCode.NEEDS_INPUT: SearchStatus.INPUT_INVALID,
    OutcomeCode.MODEL_OUT_OF_DOMAIN: SearchStatus.MODEL_OUT_OF_DOMAIN,
    OutcomeCode.TOOL_ERROR: SearchStatus.TOOL_ERROR,
    OutcomeCode.NOT_SUPPORTED: SearchStatus.INPUT_INVALID,
}


def execution_status_for(outcome: OutcomeCode) -> ExecutionStatus:
    return _OUTCOME_TO_EXECUTION[outcome]


def search_status_for(outcome: OutcomeCode) -> SearchStatus | None:
    return _OUTCOME_TO_SEARCH_STATUS.get(outcome)


@dataclass(frozen=True)
class ToolRun(ContractModel):
    """每次工具返回的公共信封（接口 v2 第 3 节字段顺序）。"""

    schema_version: str
    tool_name: str
    call_id: str
    execution_status: str | Enum
    outcome_code: str | Enum
    parent_call_id: str | None = None
    input_hash: str | None = None
    request_revision: int | str | None = None
    snapshot_id: str | None = None
    scenario_id: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    payload_ref: str | None = None
    diagnostics: tuple[Diagnostic, ...] = ()
    model_or_algorithm_version: str | None = None
    config_hash: str | None = None
    runtime_ms: float | None = None
    payload: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        status = self.execution_status
        if isinstance(status, ExecutionStatus):
            object.__setattr__(self, "execution_status", status.value)
        elif status not in tuple(item.value for item in ExecutionStatus):
            raise ValueError(f"execution_status 只允许 {[i.value for i in ExecutionStatus]}")
        outcome = self.outcome_code
        if isinstance(outcome, OutcomeCode):
            object.__setattr__(self, "outcome_code", outcome.value)
        elif outcome not in tuple(item.value for item in OutcomeCode):
            raise ValueError(f"未知 outcome_code {outcome!r}")

    @property
    def outcome(self) -> OutcomeCode:
        return OutcomeCode(self.outcome_code)

    @property
    def status(self) -> ExecutionStatus:
        return ExecutionStatus(self.execution_status)

    @property
    def ok(self) -> bool:
        """只表示本工具按约定完成；调用方仍须读取检查项。"""
        return self.outcome is OutcomeCode.OK

    @property
    def search_status(self) -> SearchStatus | None:
        return search_status_for(self.outcome)


def start_run(tool_name: str, *, call_id: str, parent_call_id: str | None = None,
              input_hash: str | None = None, request_revision: int | str | None = None,
              snapshot_id: str | None = None, scenario_id: str | None = None,
              tool_version: str | None = None, config_hash: str | None = None
              ) -> dict[str, Any]:
    """收集信封公共字段；``finish_run`` 补齐结束时刻与结果码。"""
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "tool_name": tool_name,
        "call_id": call_id,
        "parent_call_id": parent_call_id,
        "input_hash": input_hash,
        "request_revision": request_revision,
        "snapshot_id": snapshot_id,
        "scenario_id": scenario_id,
        "started_at": utc_now_iso(),
        "tool_version": tool_version,
        "config_hash": config_hash,
    }


def finish_run(envelope: dict[str, Any], *, outcome: OutcomeCode,
               diagnostics: list[Diagnostic] | None = None,
               payload: dict[str, Any] | None = None,
               payload_ref: str | None = None,
               execution_status: ExecutionStatus | None = None,
               runtime_ms: float | None = None,
               allow_missing_envelope: bool = False) -> ToolRun:
    """按结果码生成信封；成功结果缺 payload 视为内部错误。"""
    tool_version = envelope.pop("tool_version", None)
    if not allow_missing_envelope:
        for required in ("call_id", "tool_name", "started_at"):
            if not envelope.get(required):
                raise ValueError(f"信封缺少 {required}")
    if outcome is OutcomeCode.OK and payload is None and payload_ref is None:
        if not allow_missing_envelope:
            raise ValueError("outcome=ok 必须给出 payload 或 payload_ref")
    return ToolRun(
        schema_version=envelope.get("schema_version", CONTRACT_SCHEMA_VERSION),
        tool_name=envelope.get("tool_name", "unknown"),
        call_id=envelope.get("call_id", "unknown"),
        parent_call_id=envelope.get("parent_call_id"),
        input_hash=envelope.get("input_hash"),
        request_revision=envelope.get("request_revision"),
        snapshot_id=envelope.get("snapshot_id"),
        scenario_id=envelope.get("scenario_id"),
        started_at=envelope.get("started_at"),
        finished_at=utc_now_iso(),
        execution_status=execution_status or execution_status_for(outcome),
        outcome_code=outcome,
        payload_ref=payload_ref,
        diagnostics=tuple(diagnostics or ()),
        model_or_algorithm_version=tool_version,
        config_hash=envelope.get("config_hash"),
        runtime_ms=runtime_ms,
        payload=payload,
    )
