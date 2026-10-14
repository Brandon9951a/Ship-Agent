"""接口 v2 第 4 节：Tspeed → Tenergy 的批量预测请求与结果。

关键不变量：
* 每个样本表示 ``[start_offset_s, start_offset_s + duration_s)``，``duration_s > 0``；
* 候选之间可有不同长度或时长，绝不用数组下标假定同一时刻；
* 历史观测只截至规划起点，未来真实功率不得出现在请求中；
* ``motor_energy_kwh`` 与 ``predicted_motor_power_kw`` 必须落在同一积分区间。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .diagnostics import Diagnostic
from .envelope import (
    CONTRACT_SCHEMA_VERSION,
    QUALITY_FLAGS,
    ArtifactRef,
    ContractModel,
    DataQualityFlag,
    SourceRef,
    _require_finite,
    stable_hash,
)
from .interfaces import Phase


@dataclass(frozen=True)
class TrajectoryPoint(ContractModel):
    """候选轨迹中的一个时间区间；非航行阶段速度为 0 或明确未知（null）。

    ``target_speed_kmh`` 是该航段的**决策变量**（目标巡航航速）。斜坡区间的
    ``planned_speed_kmh`` 是区间平均速度，不能反过来当作决策变量；两者分开记录，
    决策向量才能从工件精确复原。
    """

    segment_id: str
    phase: str
    start_offset_s: float
    duration_s: float
    planned_speed_kmh: float | None
    environment_ref: str | None = None
    source_ref: SourceRef | None = None
    target_speed_kmh: float | None = None
    interval_kind: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.segment_id, str) or not self.segment_id.strip():
            raise ValueError("TrajectoryPoint.segment_id 必须非空")
        phase = self.phase.value if isinstance(self.phase, Phase) else self.phase
        if phase not in tuple(item.value for item in Phase):
            raise ValueError(f"未知阶段 {self.phase!r}；允许 {[i.value for i in Phase]}")
        object.__setattr__(self, "phase", phase)
        _require_finite(self.start_offset_s, "start_offset_s", minimum=0)
        duration = _require_finite(self.duration_s, "duration_s")
        if duration <= 0:
            raise ValueError("duration_s 必须大于 0")
        for name in ("planned_speed_kmh", "target_speed_kmh"):
            _require_finite(getattr(self, name), name, minimum=0, allow_none=True)
        if self.interval_kind is not None and self.interval_kind not in (
                "accel", "cruise", "decel", "waiting"):
            raise ValueError(f"未知区间类型 {self.interval_kind!r}")

    @property
    def end_offset_s(self) -> float:
        return self.start_offset_s + self.duration_s


@dataclass(frozen=True)
class CandidateTrajectory(ContractModel):
    """一个候选方案：按时间顺序排列、无重叠无间隙的区间序列。"""

    candidate_id: str
    trajectory: tuple[TrajectoryPoint, ...]
    search_seed: int | None = None
    generation: int | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "trajectory", tuple(self.trajectory))
        if not isinstance(self.candidate_id, str) or not self.candidate_id.strip():
            raise ValueError("candidate_id 必须非空")
        if not self.trajectory:
            raise ValueError(f"候选 {self.candidate_id} 的轨迹为空")
        if self.generation is not None and self.generation < 0:
            raise ValueError("generation 必须非负")

    @property
    def total_duration_s(self) -> float:
        return sum(point.duration_s for point in self.trajectory)

    def time_gaps(self, *, tolerance_s: float = 1e-6) -> list[tuple[float, float]]:
        """返回未解释的间隙/重叠；空列表表示时间轴连续。"""
        issues: list[tuple[float, float]] = []
        for previous, current in zip(self.trajectory, self.trajectory[1:]):
            delta = current.start_offset_s - previous.end_offset_s
            if abs(delta) > tolerance_s:
                issues.append((previous.end_offset_s, current.start_offset_s))
        return issues

    def speed_vector(self) -> tuple[float | None, ...]:
        """逐区间计划航速（直接对应 ``trajectory`` 的顺序）。

        注意：一个航段会展开为加速/巡航/减速多个区间，因此本序列**不是**决策向量；
        需要决策向量时使用 ``speed_by_segment()``。
        """
        return tuple(point.planned_speed_kmh for point in self.trajectory)

    def speed_by_segment(self) -> tuple[tuple[str, float | None], ...]:
        """按航段首次出现的顺序返回 (segment_id, 该段**目标航速**)。

        使用 ``target_speed_kmh``（决策变量），而不是斜坡区间的平均速度。
        """
        seen: dict[str, float | None] = {}
        order: list[str] = []
        for point in self.trajectory:
            if point.segment_id in seen or point.phase != "sailing":
                continue
            seen[point.segment_id] = point.target_speed_kmh
            order.append(point.segment_id)
        return tuple((segment_id, seen[segment_id]) for segment_id in order)

    def points_by_segment(self) -> dict[str, list[TrajectoryPoint]]:
        grouped: dict[str, list[TrajectoryPoint]] = {}
        for point in self.trajectory:
            grouped.setdefault(point.segment_id, []).append(point)
        return grouped


@dataclass(frozen=True)
class EnergyBatchRequest(ContractModel):
    """接口 v2 第 4 节的 ``EnergyBatchRequest``。"""

    candidates: tuple[CandidateTrajectory, ...]
    history_ref: str
    environment_scenario_ref: str
    model_version: str
    integration_rule: str
    schema_version: str = CONTRACT_SCHEMA_VERSION
    task_id: str | None = None
    request_revision: int | str | None = None
    snapshot_id: str | None = None
    scenario_id: str | None = None
    call_id: str | None = None
    parent_call_id: str | None = None
    source_refs: tuple[SourceRef, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))
        object.__setattr__(self, "source_refs", tuple(self.source_refs))
        if not self.candidates:
            raise ValueError("EnergyBatchRequest 至少需要一个候选")
        seen = set()
        for candidate in self.candidates:
            if candidate.candidate_id in seen:
                raise ValueError(f"候选 ID 重复：{candidate.candidate_id}")
            seen.add(candidate.candidate_id)
        for name in ("history_ref", "environment_scenario_ref", "model_version",
                     "integration_rule"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} 必须是非空字符串（缺依据时不要填未知占位）")

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        return tuple(item.candidate_id for item in self.candidates)

    def cache_key(self) -> str:
        """缓存键覆盖候选轨迹、历史窗口、环境情景、模型版本与积分规则。"""
        return stable_hash({
            "model_version": self.model_version,
            "integration_rule": self.integration_rule,
            "history_ref": self.history_ref,
            "environment_scenario_ref": self.environment_scenario_ref,
            "snapshot_id": self.snapshot_id,
            "scenario_id": self.scenario_id,
            "candidates": [
                {"candidate_id": item.candidate_id,
                 "trajectory": [point.to_dict() for point in item.trajectory]}
                for item in self.candidates
            ],
        })


@dataclass(frozen=True)
class PowerPoint(ContractModel):
    """逐时功率区间；``motor_energy_kwh`` 与功率必须同区间。"""

    segment_id: str
    phase: str
    start_offset_s: float
    duration_s: float
    planned_speed_kmh: float | None
    predicted_motor_power_kw: float | None
    motor_energy_kwh: float | None
    applicability_flag: str | None = None
    quality_flags: tuple[str, ...] = ()
    target_speed_kmh: float | None = None
    interval_kind: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "quality_flags", tuple(self.quality_flags))
        _require_finite(self.start_offset_s, "start_offset_s", minimum=0)
        duration = _require_finite(self.duration_s, "duration_s")
        if duration <= 0:
            raise ValueError("PowerPoint.duration_s 必须大于 0")
        for name in ("predicted_motor_power_kw", "motor_energy_kwh",
                     "planned_speed_kmh", "target_speed_kmh"):
            _require_finite(getattr(self, name), name, allow_none=True)
        for flag in self.quality_flags:
            if flag not in QUALITY_FLAGS:
                raise ValueError(f"未知质量标记 {flag!r}")

    @property
    def end_offset_s(self) -> float:
        return self.start_offset_s + self.duration_s

    def integrated_energy_kwh(self) -> float | None:
        """按 ``Σ P·Δt/3600`` 复算能量；功率未知返回 null。"""
        if self.predicted_motor_power_kw is None:
            return None
        return self.predicted_motor_power_kw * self.duration_s / 3600.0


@dataclass(frozen=True)
class EnergyCoverage(ContractModel):
    """有效覆盖率与异常比例：不用成功候选的均值补缺失区间。"""

    interval_count: int
    predicted_count: int
    valid_fraction: float
    extrapolated_fraction: float
    unpredicted_fraction: float
    covered_seconds: float
    total_seconds: float


@dataclass(frozen=True)
class SegmentSummary(ContractModel):
    """按航段/全程汇总：移动与等待时长、预测电机能量、峰值功率、积分覆盖率。"""

    scope_id: str
    phase: str | None
    moving_duration_s: float
    waiting_duration_s: float
    predicted_motor_energy_kwh: float | None
    peak_motor_power_kw: float | None
    covered_seconds: float
    total_seconds: float
    coverage_fraction: float
    distance_km: float | None = None


@dataclass(frozen=True)
class EnergyResultEnvelope(ContractModel):
    """单个候选的预测结果；失败候选保留自己的诊断。"""

    candidate_id: str
    outcome_code: str
    power_points: tuple[PowerPoint, ...] = ()
    segment_summaries: tuple[SegmentSummary, ...] = ()
    total_summary: SegmentSummary | None = None
    coverage: EnergyCoverage | None = None
    artifact_ref: ArtifactRef | None = None
    diagnostics: tuple[Diagnostic, ...] = ()
    model_version: str | None = None
    cache_hit: bool = False
    runtime_ms: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "power_points", tuple(self.power_points))
        object.__setattr__(self, "segment_summaries", tuple(self.segment_summaries))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        if not self.candidate_id.strip():
            raise ValueError("candidate_id 必须非空")

    @property
    def ok(self) -> bool:
        return self.outcome_code == "ok"

    @property
    def predicted_motor_energy_kwh(self) -> float | None:
        if self.total_summary is None:
            return None
        return self.total_summary.predicted_motor_energy_kwh

    @property
    def motor_energy_kwh(self) -> float | None:
        """别名：逐时预测电机侧电耗汇总（kW·h = kWh）。"""
        return self.predicted_motor_energy_kwh

    @property
    def peak_motor_power_kw(self) -> float | None:
        if self.total_summary is None:
            return None
        values = [p.predicted_motor_power_kw for p in self.power_points
                  if p.predicted_motor_power_kw is not None]
        return max(values) if values else None

    def energies_match(self, *, tolerance_kwh: float = 1e-9) -> bool:
        """逐时能量与 ``P·Δt/3600`` 一致，且与汇总自洽。"""
        for point in self.power_points:
            expected = point.integrated_energy_kwh()
            if expected is None:
                continue
            if point.motor_energy_kwh is None:
                return False
            if abs(point.motor_energy_kwh - expected) > tolerance_kwh:
                return False
        return True


@dataclass(frozen=True)
class EnergyBatchResult(ContractModel):
    """接口 v2 第 4 节的 ``EnergyBatchResult``；逐候选成功或诊断。"""

    results: tuple[EnergyResultEnvelope, ...]
    model_version: str
    integration_rule: str
    schema_version: str = CONTRACT_SCHEMA_VERSION
    call_id: str | None = None
    parent_call_id: str | None = None
    batch_runtime_ms: float | None = None
    cache_hits: int = 0
    diagnostics: tuple[Diagnostic, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "results", tuple(self.results))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))

    def by_candidate(self) -> dict[str, EnergyResultEnvelope]:
        return {item.candidate_id: item for item in self.results}

    def require_all_candidates(self, candidate_ids: tuple[str, ...]) -> None:
        """批内结果顺序变化不得改变候选 ID 与结果的对应关系。"""
        got = [item.candidate_id for item in self.results]
        if sorted(got) != sorted(candidate_ids):
            raise ValueError("批量结果与请求候选 ID 不一致")
