"""Tenergy's typed v2 draft proposal; does not replace the shared v1 schemas."""

from dataclasses import asdict, dataclass
import math
import re
from typing import Any

VERSION = "research-tools/2.0-draft"


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def valid_ref(ref):
    return (isinstance(ref,dict) and set(ref)=={"artifact_id","sha256","row_count","schema_version"} and
            isinstance(ref["sha256"],str) and re.fullmatch(r"[a-f0-9]{64}",ref["sha256"]) is not None and
            ref["artifact_id"]==ref["sha256"] and type(ref["row_count"]) is int and ref["row_count"]>=0 and
            ref["schema_version"]==VERSION)


@dataclass(frozen=True)
class EnergyBatchRequest:
    schema_version: str
    task_id: str
    request_revision: int
    snapshot_id: str
    scenario_id: str
    call_id: str
    parent_call_id: str | None
    planning_origin_at: str | None
    usage: str
    model_version: str
    integration_rule: str
    history_ref: dict | None
    environment_scenario_ref: dict | None
    candidates: list[dict]

    @classmethod
    def from_dict(cls, value: Any):
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ValueError("EnergyBatchRequest requires exactly the documented fields")
        request = cls(**value)
        if request.schema_version != VERSION:
            raise ValueError("Unsupported schema_version")
        for field in ("task_id", "snapshot_id", "scenario_id", "call_id", "model_version"):
            if not isinstance(value[field], str) or not value[field].strip():
                raise ValueError(f"{field} must be a nonempty string")
        if type(request.request_revision) is not int or request.request_revision < 0:
            raise ValueError("request_revision must be a nonnegative integer")
        if request.parent_call_id is not None and not isinstance(request.parent_call_id, str):
            raise ValueError("parent_call_id must be string or null")
        if request.usage not in ("research", "planning"):
            raise ValueError("usage must be research or planning")
        if request.integration_rule != "right_endpoint":
            raise ValueError("Only right_endpoint integration is supported")
        for field in ("history_ref","environment_scenario_ref"):
            if value[field] is not None and not valid_ref(value[field]):
                raise ValueError(f"{field} must be ArtifactRef or null")
        if request.planning_origin_at is not None:
            from datetime import datetime
            if not isinstance(request.planning_origin_at, str):
                raise ValueError("planning_origin_at must be a timestamp or null")
            stamp = datetime.fromisoformat(request.planning_origin_at.replace("Z", "+00:00"))
            if stamp.utcoffset() is None:
                raise ValueError("planning_origin_at must include timezone")
        if not isinstance(request.candidates, list) or not request.candidates:
            raise ValueError("candidates must be a nonempty list")
        ids = [c.get("candidate_id") if isinstance(c, dict) else None for c in request.candidates]
        if any(not isinstance(i, str) or not i.strip() for i in ids) or len(set(ids)) != len(ids):
            raise ValueError("candidate_id must be nonempty and unique within the batch")
        return request

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class ToolRun:
    schema_version: str
    tool_name: str
    task_id: str | None
    call_id: str | None
    parent_call_id: str | None
    input_hash: str
    request_revision: int | None
    snapshot_id: str | None
    scenario_id: str | None
    started_at: str
    finished_at: str
    execution_status: str
    outcome_code: str
    payload_ref: dict | None
    diagnostics: list[dict]
    model_or_algorithm_version: str | None
    config_hash: str

    def to_dict(self):
        return asdict(self)
