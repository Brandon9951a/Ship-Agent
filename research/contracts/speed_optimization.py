"""接口 v2 第 6、7 节：Tspeed 的请求/结果与 FDP 反馈。

``SpeedOptimizationResult`` 只声明字段与取值边界；所有检查项都必须来自
独立约束检查器，不能由算法内部标记“通过”代替。
"""

from __future__ import annotations

from dataclasses import dataclass

from .diagnostics import Diagnostic
from .envelope import (
    CONTRACT_SCHEMA_VERSION,
    ArtifactRef,
    ContractModel,
    SourceRef,
    _require_finite,
    stable_hash,
)

SPEED_OPTIMIZATION_SCHEMA_VERSION = "research-tools/2.0-draft/speed-optimization"

# 已实现的目标类型；未实现的目标必须返回不支持，不能悄悄改用另一目标。
SUPPORTED_OBJECTIVES = ("min_energy", "min_time")

FAILURE_SCOPES = ("specific_interval", "power_upper_bound_provable",
                  "soc_path_dependent", "grid_unresolved", "config_mismatch")


@dataclass(frozen=True)
class SegmentConstraint(ContractModel):
    """有来源的逐航段约束；语言模型不得改动这些边界。

    等待时间必须由 Tseg 的规则显式给出（``waiting_rule`` + ``waiting_duration_s``），
    不由本工具自行编造；等待时间单列计入总时长与辅助能耗。
    """

    segment_id: str
    distance_km: float
    min_speed_kmh: float | None
    max_speed_kmh: float | None
    phase_or_segment_type: str | None = None
    wait_or_window_rule: str | None = None
    source_refs: tuple[SourceRef, ...] = ()
    quality_flags: tuple[str, ...] = ()
    fixed_speed_kmh: float | None = None
    waiting_rule: str | None = None
    waiting_duration_s: float = 0.0
    origin_entity_id: str | None = None
    destination_entity_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_refs", tuple(self.source_refs))
        object.__setattr__(self, "quality_flags", tuple(self.quality_flags))
        if not self.segment_id.strip():
            raise ValueError("segment_id 必须非空")
        _require_finite(self.distance_km, "distance_km", minimum=0)
        for name in ("min_speed_kmh", "max_speed_kmh", "fixed_speed_kmh"):
            _require_finite(getattr(self, name), name, minimum=0, allow_none=True)
        if self.min_speed_kmh is not None and self.max_speed_kmh is not None \
                and self.min_speed_kmh > self.max_speed_kmh:
            raise ValueError(f"{self.segment_id}: min_speed_kmh 不得大于 max_speed_kmh")
        _require_finite(self.waiting_duration_s, "waiting_duration_s", minimum=0)
        if self.waiting_duration_s > 0 and not (self.waiting_rule or "").strip():
            raise ValueError(
                f"{self.segment_id}: 给出等待时间时必须同时给出 wait_or_window_rule 来源说明")
        if self.fixed_speed_kmh is not None:
            if self.min_speed_kmh is not None and self.fixed_speed_kmh < self.min_speed_kmh:
                raise ValueError(f"{self.segment_id}: 固定航速低于限速下限")
            if self.max_speed_kmh is not None and self.fixed_speed_kmh > self.max_speed_kmh:
                raise ValueError(f"{self.segment_id}: 固定航速高于限速上限")

    @property
    def optimizable(self) -> bool:
        """固定航速航段（港口/船闸等）不进入决策向量。"""
        return self.fixed_speed_kmh is None


@dataclass(frozen=True)
class TimeGridSpec(ContractModel):
    """时间网格映射：预测步长与核验精度可不同，但都须显式记录。"""

    prediction_step_s: float
    verification_step_s: float
    max_acceleration_mps2: float
    max_deceleration_mps2: float
    min_speed_kmh: float
    max_speed_kmh: float
    distance_tolerance_km: float = 1e-6
    # 是否在终点减速停车。真实靠泊会停车，因此默认 True；若末段限速当按"通过速度"
    # 处理，则由调用方显式关闭，避免展开出低于限速下限的斜坡速度。
    decelerate_to_stop_at_end: bool = True

    def __post_init__(self) -> None:
        for name in ("prediction_step_s", "verification_step_s"):
            value = _require_finite(getattr(self, name), name)
            if value <= 0:
                raise ValueError(f"{name} 必须大于 0")
        for name in ("max_acceleration_mps2", "max_deceleration_mps2"):
            value = _require_finite(getattr(self, name), name)
            if value <= 0:
                raise ValueError(f"{name} 必须大于 0")
        low = _require_finite(self.min_speed_kmh, "min_speed_kmh")
        high = _require_finite(self.max_speed_kmh, "max_speed_kmh")
        if low > high:
            raise ValueError("min_speed_kmh 不得大于 max_speed_kmh")
        _require_finite(self.distance_tolerance_km, "distance_tolerance_km", minimum=0)


@dataclass(frozen=True)
class SpeedOptimizationRequest(ContractModel):
    """接口 v2 第 6 节的 ``SpeedOptimizationRequest``。"""

    segments: tuple[SegmentConstraint, ...]
    time_grid: TimeGridSpec
    objective: str = "min_energy"
    current_position_km: float | None = None
    current_speed_kmh: float | None = None
    remaining_time_s: float | None = None
    absolute_deadline_at: str | None = None
    planning_origin_at: str | None = None
    speed_penalty_lambda: float = 0.0
    power_limit_kw: float | None = None
    power_limit_source_ref: SourceRef | None = None
    battery_budget_kwh: float | None = None
    battery_budget_source_ref: SourceRef | None = None
    energy_ref: str | None = None
    demand_builder_ref: str | None = None
    history_ref: str | None = None
    environment_scenario_ref: str | None = None
    prior_feedback_ref: str | None = None
    schema_version: str = SPEED_OPTIMIZATION_SCHEMA_VERSION
    task_id: str | None = None
    request_revision: int | str | None = None
    snapshot_id: str | None = None
    scenario_id: str | None = None
    call_id: str | None = None
    parent_call_id: str | None = None
    algorithm_config: dict[str, object] | None = None
    source_refs: tuple[SourceRef, ...] = ()
    assumptions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "segments", tuple(self.segments))
        object.__setattr__(self, "source_refs", tuple(self.source_refs))
        object.__setattr__(self, "assumptions", tuple(self.assumptions))
        if not self.segments:
            raise ValueError("至少需要一个航段")
        seen = set()
        for segment in self.segments:
            if segment.segment_id in seen:
                raise ValueError(f"航段 ID 重复：{segment.segment_id}")
            seen.add(segment.segment_id)
        if self.objective not in SUPPORTED_OBJECTIVES:
            raise ValueError(
                f"目标类型 {self.objective!r} 未实现；只允许 {SUPPORTED_OBJECTIVES}")
        _require_finite(self.speed_penalty_lambda, "speed_penalty_lambda", minimum=0)
        for name in ("current_position_km", "current_speed_kmh", "remaining_time_s",
                     "power_limit_kw", "battery_budget_kwh"):
            _require_finite(getattr(self, name), name, minimum=0, allow_none=True)
        if self.power_limit_kw is not None and self.power_limit_source_ref is None:
            raise ValueError("给出功率上限时必须同时给出 power_limit_source_ref")
        if self.battery_budget_kwh is not None and self.battery_budget_source_ref is None:
            raise ValueError("给出电池预算时必须同时给出 battery_budget_source_ref")

    @property
    def total_distance_km(self) -> float:
        return sum(segment.distance_km for segment in self.segments)

    @property
    def optimizable_segment_ids(self) -> tuple[str, ...]:
        return tuple(segment.segment_id for segment in self.segments if segment.optimizable)

    def input_hash(self) -> str:
        return stable_hash(self.to_dict())


@dataclass(frozen=True)
class ConstraintCheckRecord(ContractModel):
    """逐项检查：时间/速度/加速度/距离/功率/电量。"""

    name: str
    passed: bool
    actual: float | None = None
    limit: float | None = None
    unit: str | None = None
    reason: str | None = None
    segment_id: str | None = None
    time_interval_s: tuple[float, float] | None = None
    source_ref: SourceRef | None = None
    tolerance: float | None = None

    def __post_init__(self) -> None:
        _require_finite(self.actual, "actual", allow_none=True)
        _require_finite(self.limit, "limit", allow_none=True)
        _require_finite(self.tolerance, "tolerance", minimum=0, allow_none=True)


@dataclass(frozen=True)
class ObjectiveTerms(ContractModel):
    """目标函数分项：必须注明能量口径与惩罚项量纲处理。"""

    energy_kwh: float | None
    penalty: float
    lambda_value: float
    total: float | None
    energy_scope: str
    penalty_definition: str
    penalty_scale_note: str | None = None

    def __post_init__(self) -> None:
        _require_finite(self.energy_kwh, "energy_kwh", minimum=0, allow_none=True)
        _require_finite(self.penalty, "penalty", minimum=0)
        _require_finite(self.lambda_value, "lambda_value", minimum=0)
        _require_finite(self.total, "total", allow_none=True)


@dataclass(frozen=True)
class ManagementFeedback(ContractModel):
    """接口 v2 第 7 节的 ``ManagementFeedback``（FDP → Tspeed）。"""

    selected_candidate_id: str
    selected_demand_ref: str
    battery_config_hash: str
    failure_scope: str
    violated_rule: str
    first_failure_interval: tuple[float, float] | None = None
    demand_kw: float | None = None
    deliverable_kw: float | None = None
    soc_1: float | None = None
    soc_2: float | None = None
    limit_source_ref: SourceRef | None = None
    diagnostic_certainty: str | None = None
    suggested_search_action: str | None = None
    detail: str | None = None
    schema_version: str = CONTRACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.failure_scope not in FAILURE_SCOPES:
            raise ValueError(f"failure_scope 只允许 {FAILURE_SCOPES}")
        for name in ("demand_kw", "deliverable_kw"):
            _require_finite(getattr(self, name), name, minimum=0, allow_none=True)
        for name in ("soc_1", "soc_2"):
            _require_finite(getattr(self, name), name, minimum=0, maximum=1, allow_none=True)
        if self.first_failure_interval is not None:
            start, end = self.first_failure_interval
            _require_finite(start, "first_failure_interval.start", minimum=0)
            _require_finite(end, "first_failure_interval.end", minimum=0)
            if end < start:
                raise ValueError("失败区间结束不得早于开始")
        if self.failure_scope == "power_upper_bound_provable" and self.limit_source_ref is None:
            raise ValueError("声称可证明的功率上限必须给出 limit_source_ref")

    @property
    def becomes_hard_constraint(self) -> bool:
        """仅当对候选族可证明时才允许转成新的硬功率/能量约束。

        SOC 路径相关失败只能记录为候选拒绝或条件化搜索提示。
        """
        return self.failure_scope == "power_upper_bound_provable"

    def config_hash(self) -> str:
        return stable_hash(self.to_dict())


@dataclass(frozen=True)
class SearchTrace(ContractModel):
    """DE 搜索记录：迭代轨迹、可行个体比例与违反约束统计。"""

    trace_id: str
    seed: int
    population: int
    generations_requested: int
    generations_run: int
    evaluations: int
    best_objective_by_generation: tuple[float | None, ...]
    feasible_fraction_by_generation: tuple[float, ...]
    violation_counts: dict[str, int]
    restarts: int = 0
    cache_hits: int = 0
    diagnostic_candidates: tuple[dict[str, object], ...] = ()
    artifact_ref: ArtifactRef | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "best_objective_by_generation",
                           tuple(self.best_objective_by_generation))
        object.__setattr__(self, "feasible_fraction_by_generation",
                           tuple(self.feasible_fraction_by_generation))
        object.__setattr__(self, "diagnostic_candidates",
                           tuple(self.diagnostic_candidates))
        if not self.trace_id.strip():
            raise ValueError("trace_id 必须非空")


@dataclass(frozen=True)
class SpeedOptimizationResult(ContractModel):
    """接口 v2 第 6 节的 ``SpeedOptimizationResult``。"""

    search_status: str
    selected_candidate_id: str | None
    selected_trajectory_ref: str | None
    selected_energy_ref: str | None
    selected_demand_ref: str | None
    segment_summary: tuple[dict[str, object], ...]
    total_duration_s: float | None
    arrival_at: str | None
    objective_terms: ObjectiveTerms | None
    constraint_checks: tuple[ConstraintCheckRecord, ...]
    search_trace_ref: str | None
    seed: int
    population: int
    generations: int
    evaluations: int
    runtime_ms: float
    schema_version: str = SPEED_OPTIMIZATION_SCHEMA_VERSION
    selected_trajectory: tuple[dict[str, object], ...] = ()
    selected_speed_vector: tuple[float, ...] = ()
    selected_candidate_ids_by_segment: dict[str, str] | None = None
    total_energy_kwh: float | None = None
    total_distance_km: float | None = None
    search_trace: SearchTrace | None = None
    demand_config_hash: str | None = None
    bus_side_available: bool = False
    prior_feedback_ref: str | None = None
    rerun_count: int = 0
    diagnostics: tuple[Diagnostic, ...] = ()
    assumptions: tuple[str, ...] = ()
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "segment_summary", tuple(self.segment_summary))
        object.__setattr__(self, "constraint_checks", tuple(self.constraint_checks))
        object.__setattr__(self, "selected_trajectory", tuple(self.selected_trajectory))
        object.__setattr__(self, "selected_speed_vector", tuple(self.selected_speed_vector))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        object.__setattr__(self, "assumptions", tuple(self.assumptions))
        valid = ("candidate_found", "search_unresolved", "input_invalid",
                 "model_out_of_domain", "tool_error")
        if self.search_status not in valid:
            raise ValueError(f"search_status 只允许 {valid}")
        if self.search_status == "candidate_found":
            if self.selected_candidate_id is None:
                raise ValueError("candidate_found 必须给出 selected_candidate_id")
            failed = [check.name for check in self.constraint_checks if not check.passed]
            if failed:
                raise ValueError(
                    f"选定方案存在未通过的检查 {failed}；不可执行候选不得放入选定字段")

    @property
    def all_checks_passed(self) -> bool:
        return all(check.passed for check in self.constraint_checks)
