"""Tenergy 批量预测器的接口协议，以及仅用于验收的解析后端。

DE 只依赖本协议，不依赖具体模型。正式研究模型是 LSTM（罗负责，尚未冻结
``evaluate_batch``），因此这里：
* 冻结 DE 侧的调用契约（批量、候选 ID 配对、逐候选诊断、缓存键、耗时记录）；
* 提供 ``AnalyticPowerPredictor`` 作为**验收用**确定性功率函数，用于算法正确性、
  距离/时间/能量一致性和约束检查的独立验证；
* LSTM 适配器为薄封装，见 ``research/tspeed/lstm_adapter.py``。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol, Sequence, runtime_checkable

from research.contracts import (
    CandidateTrajectory,
    Diagnostic,
    EnergyBatchRequest,
    EnergyBatchResult,
    EnergyCoverage,
    EnergyResultEnvelope,
    OutcomeCode,
    Phase,
    PowerPoint,
    SegmentSummary,
    stable_hash,
)
from research.tspeed.profile import TrajectoryProfile

ANALYTIC_MODEL_VERSION = "analytic-cubic/1.0"
DEFAULT_INTEGRATION_RULE = "E = sum(P_k * duration_s / 3600)"


@dataclass(frozen=True)
class EvaluationGrid:
    """一次评估共用的时间网格与几何信息（候选之间的距离基准）。"""

    prediction_step_s: float
    verification_step_s: float
    distance_km_by_segment: dict[str, float]
    phase_by_segment: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("prediction_step_s", "verification_step_s"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} 必须为正数")


@dataclass(frozen=True)
class PredictionOutcome:
    """一个候选的预测结果与诊断；失败候选保留自己的诊断，不用均值补齐。

    ``motor_energy_kwh`` 由后端直接给出，因此搜索期可以在保留汇总与诊断的同时
    释放 ``power_points``（逐时工件），避免 DE 多代评估把内存吃满。
    """

    candidate_id: str
    outcome_code: str
    motor_energy_kwh: float | None = None
    peak_power_kw: float | None = None
    power_points: tuple[PowerPoint, ...] = ()
    segment_summaries: tuple[SegmentSummary, ...] = ()
    total_summary: SegmentSummary | None = None
    coverage: EnergyCoverage | None = None
    model_version: str | None = None
    cache_hit: bool = False
    diagnostics: tuple[Diagnostic, ...] = ()
    artifact_ref: ArtifactRef | None = None
    runtime_ms: float | None = None

    @property
    def ok(self) -> bool:
        return self.outcome_code == OutcomeCode.OK.value

    @property
    def peak_motor_power_kw(self) -> float | None:
        """优先使用后端直接给出的峰值；否则从逐时工件求最大值。"""
        if self.peak_power_kw is not None:
            return self.peak_power_kw
        values = [point.predicted_motor_power_kw for point in self.power_points
                  if point.predicted_motor_power_kw is not None]
        return max(values) if values else None

    @property
    def extrapolated_fraction(self) -> float | None:
        return self.coverage.extrapolated_fraction if self.coverage else None

    def without_power_points(self) -> "PredictionOutcome":
        """释放逐时工件、保留决策所需信息（用于搜索期长期驻留的候选）。"""
        return PredictionOutcome(
            candidate_id=self.candidate_id, outcome_code=self.outcome_code,
            motor_energy_kwh=self.motor_energy_kwh,
            peak_power_kw=self.peak_motor_power_kw, power_points=(),
            segment_summaries=self.segment_summaries,
            total_summary=self.total_summary, coverage=self.coverage,
            model_version=self.model_version, cache_hit=self.cache_hit,
            diagnostics=self.diagnostics, artifact_ref=self.artifact_ref,
            runtime_ms=self.runtime_ms)

    def to_envelope(self) -> EnergyResultEnvelope:
        """转换为 v2 逐候选结果工件（核验与落盘路径使用）。"""
        return EnergyResultEnvelope(
            candidate_id=self.candidate_id,
            outcome_code=self.outcome_code,
            power_points=self.power_points,
            segment_summaries=self.segment_summaries,
            total_summary=self.total_summary,
            coverage=self.coverage,
            artifact_ref=self.artifact_ref,
            diagnostics=self.diagnostics,
            model_version=self.model_version,
            cache_hit=self.cache_hit,
            runtime_ms=self.runtime_ms,
        )

    @property
    def interval_count(self) -> int:
        return len(self.power_points)


@runtime_checkable
class EnergyPredictor(Protocol):
    """DE 侧唯一依赖的预测器接口。"""

    @property
    def model_version(self) -> str: ...

    @property
    def integration_rule(self) -> str: ...

    def predict(self, request: EnergyBatchRequest, *,
                grid: EvaluationGrid) -> EnergyBatchResult: ...

    def predict_profile(self, profile: "TrajectoryProfile", *,
                        candidate_id: str, grid: EvaluationGrid
                        ) -> "PredictionOutcome": ...


class ProfilePredictor(Protocol):
    """搜索期使用的轻量剖面协议；正式 LSTM 适配器须与 ``predict`` 数值一致。"""

    @property
    def model_version(self) -> str: ...

    @property
    def integration_rule(self) -> str: ...

    def predict_profile(self, profile: "TrajectoryProfile", *,
                        candidate_id: str, grid: EvaluationGrid
                        ) -> "PredictionOutcome": ...


class AnalyticPowerPredictor:
    """验收用解析功率函数：``P_motor = c · v³ · env_factor + P0``。

    该后端**不是**正式研究模型，只用于：
    * 验证 DE 能找到解析最优解；
    * 验证距离、时间、能量积分与约束检查的一致性；
    * 在没有 LSTM 时给出可复现的耗时基线。

    环境因子只影响功率，不影响几何或时间，因此最优航速仍可通过穷举独立求出。
    缓存键只用「速度向量 + 模型配置 + 时间网格」：轨迹由速度向量确定性生成，
    但同一速度向量在不同环境情景下会产生不同环境引用，故环境情景也入键。
    """

    def __init__(self, *, coefficient: float, exponent: float = 3.0,
                 idle_power_kw: float = 0.0, model_version: str = ANALYTIC_MODEL_VERSION,
                 integration_rule: str = DEFAULT_INTEGRATION_RULE,
                 environment_factor: float = 1.0,
                 max_cache_entries: int = 512) -> None:
        if coefficient <= 0:
            raise ValueError("coefficient 必须为正")
        if exponent <= 0:
            raise ValueError("exponent 必须为正")
        if idle_power_kw < 0:
            raise ValueError("idle_power_kw 不得为负")
        if environment_factor <= 0:
            raise ValueError("environment_factor 必须为正")
        if max_cache_entries < 1:
            raise ValueError("max_cache_entries 必须为正整数")
        self._coefficient = float(coefficient)
        self._exponent = float(exponent)
        self._idle = float(idle_power_kw)
        self._model_version = model_version
        self._integration_rule = integration_rule
        self._environment_factor = float(environment_factor)
        # 有界缓存：DE 会评估数千个候选，逐时工件不能无界驻留内存。
        self._cache: dict[str, PredictionOutcome] = {}
        self._cache_order: list[str] = []
        self._max_cache_entries = int(max_cache_entries)
        self._last_key_context: str | None = None

    def _remember(self, key: str, outcome: PredictionOutcome) -> None:
        if key not in self._cache:
            self._cache_order.append(key)
        self._cache[key] = outcome
        while len(self._cache_order) > self._max_cache_entries:
            oldest = self._cache_order.pop(0)
            self._cache.pop(oldest, None)

    @property
    def cache_size(self) -> int:
        return len(self._cache)

    @property
    def model_version(self) -> str:
        return self._model_version

    @property
    def integration_rule(self) -> str:
        return self._integration_rule

    @property
    def config_hash(self) -> str:
        return stable_hash({"model_version": self._model_version,
                            "coefficient": self._coefficient,
                            "exponent": self._exponent,
                            "idle_power_kw": self._idle,
                            "environment_factor": self._environment_factor})

    def power_kw(self, speed_kmh: float) -> float:
        """单点电机侧功率（kW）；参数与单位都来自本后端的显式配置。"""
        if speed_kmh < 0:
            raise ValueError("航速不得为负")
        return self._coefficient * speed_kmh ** self._exponent * self._environment_factor \
            + self._idle

    # --- EnergyPredictor 协议 -------------------------------------------------
    def predict(self, request: EnergyBatchRequest, *,
                grid: EvaluationGrid) -> EnergyBatchResult:
        started = time.perf_counter()
        self._last_key_context = stable_hash({
            "model": self.config_hash,
            "environment": request.environment_scenario_ref,
            "history": request.history_ref,
            "prediction_step_s": grid.prediction_step_s,
            "verification_step_s": grid.verification_step_s,
        })
        results: list[EnergyResultEnvelope] = []
        cache_hits = 0
        for candidate in request.candidates:
            outcome = self._predict_candidate(candidate, grid=grid)
            if outcome.cache_hit:
                cache_hits += 1
            results.append(EnergyResultEnvelope(
                candidate_id=outcome.candidate_id, outcome_code=outcome.outcome_code,
                power_points=outcome.power_points,
                segment_summaries=outcome.segment_summaries,
                total_summary=outcome.total_summary, coverage=outcome.coverage,
                artifact_ref=outcome.artifact_ref, diagnostics=outcome.diagnostics,
                model_version=outcome.model_version, cache_hit=outcome.cache_hit,
                runtime_ms=outcome.runtime_ms))
        return EnergyBatchResult(
            results=tuple(results),
            model_version=self._model_version,
            integration_rule=self._integration_rule,
            call_id=request.call_id,
            parent_call_id=request.parent_call_id,
            batch_runtime_ms=(time.perf_counter() - started) * 1000.0,
            cache_hits=cache_hits,
        )

    def _cache_key(self, candidate: CandidateTrajectory) -> str:
        return stable_hash({
            "context": self._last_key_context,
            "speeds": [None if point.planned_speed_kmh is None
                       else round(point.planned_speed_kmh, 9)
                       for point in candidate.trajectory],
            "durations": [round(point.duration_s, 9) for point in candidate.trajectory],
            "phases": [point.phase for point in candidate.trajectory],
        })

    # --- 轻量剖面路径：搜索期使用，避免实例化上万个 v2 对象 --------------------
    def predict_profile(self, profile: "TrajectoryProfile", *,
                        candidate_id: str, grid: EvaluationGrid,
                        keep_power_points: bool = True) -> PredictionOutcome:
        key = stable_hash({
            "context": self._last_key_context,
            "kind": "profile",
            "speeds": [None if speed is None else round(speed, 9)
                       for speed in profile.speeds],
            "durations": [round(value, 9) for value in profile.durations],
            "phases": list(profile.phases),
            "keep_power_points": keep_power_points,
        })
        cached = self._cache.get(key)
        if cached is not None:
            return PredictionOutcome(
                candidate_id=candidate_id, outcome_code=cached.outcome_code,
                motor_energy_kwh=cached.motor_energy_kwh,
                peak_power_kw=cached.peak_power_kw,
                power_points=cached.power_points,
                segment_summaries=cached.segment_summaries,
                total_summary=cached.total_summary, coverage=cached.coverage,
                model_version=cached.model_version, cache_hit=True,
                diagnostics=cached.diagnostics, artifact_ref=cached.artifact_ref,
                runtime_ms=0.0)
        started = time.perf_counter()
        power_points: list[PowerPoint] = []
        diagnostics: list[Diagnostic] = []
        unpredicted_duration = 0.0
        unpredicted_intervals = 0
        predicted_duration = 0.0
        interval_energy = 0.0
        peak_power: float | None = None
        complete = True
        for index, (segment_id, phase, start, duration, speed) in enumerate(zip(
                profile.segment_ids, profile.phases, profile.starts,
                profile.durations, profile.speeds)):
            target = profile.targets[index] if profile.targets else None
            kind = profile.kinds[index] if profile.kinds else None
            if phase != Phase.SAILING.value:
                power: float | None = 0.0
                flag = "model_rule_zero_propulsion"
                flags: tuple[str, ...] = ("assumption",)
            elif speed is None:
                power = None
                flag = "speed_unknown"
                flags = ("unpredicted",)
                complete = False
                unpredicted_duration += duration
                unpredicted_intervals += 1
                diagnostics.append(Diagnostic(
                    stage="predict", code="speed_unknown",
                    time_interval_s=(start, start + duration),
                    segment_id=segment_id, field_ref="planned_speed_kmh",
                    detail="航行阶段缺少计划航速，不能给出预测功率", retryable=False))
            else:
                power = self.power_kw(speed)
                flag = "in_domain"
                flags = ()
            energy = None if power is None else power * duration / 3600.0
            if energy is not None:
                interval_energy += energy
                predicted_duration += duration
            if power is not None and (peak_power is None or power > peak_power):
                peak_power = power
            if keep_power_points:
                power_points.append(PowerPoint(
                    segment_id=segment_id, phase=phase, start_offset_s=start,
                    duration_s=duration, planned_speed_kmh=speed,
                    predicted_motor_power_kw=power, motor_energy_kwh=energy,
                    applicability_flag=flag, quality_flags=flags,
                    target_speed_kmh=target, interval_kind=kind))
        total_seconds = sum(profile.durations)
        coverage = EnergyCoverage(
            interval_count=profile.interval_count,
            predicted_count=(len(power_points) if keep_power_points
                             else profile.interval_count - unpredicted_intervals),
            valid_fraction=predicted_duration / total_seconds if total_seconds else 0.0,
            extrapolated_fraction=0.0,
            unpredicted_fraction=(unpredicted_duration / total_seconds
                                  if total_seconds else 0.0),
            covered_seconds=predicted_duration, total_seconds=total_seconds)
        summaries = (self._summaries_from_points(power_points, grid)
                     if keep_power_points
                     else self._summaries_from_profile(profile, grid))
        total_summary = SegmentSummary(
            scope_id="voyage", phase=None,
            moving_duration_s=sum(duration for phase, duration in
                                  zip(profile.phases, profile.durations)
                                  if phase == Phase.SAILING.value),
            waiting_duration_s=sum(duration for phase, duration in
                                   zip(profile.phases, profile.durations)
                                   if phase != Phase.SAILING.value),
            predicted_motor_energy_kwh=(interval_energy if complete else None),
            peak_motor_power_kw=peak_power,
            covered_seconds=predicted_duration, total_seconds=total_seconds,
            coverage_fraction=(predicted_duration / total_seconds
                               if total_seconds else 0.0),
            distance_km=sum(grid.distance_km_by_segment.get(segment_id, 0.0)
                            for segment_id in profile.segment_order()))
        outcome = PredictionOutcome(
            candidate_id=candidate_id,
            outcome_code=(OutcomeCode.OK.value if complete
                          else OutcomeCode.NEEDS_INPUT.value),
            motor_energy_kwh=(interval_energy if complete else None),
            peak_power_kw=peak_power,
            power_points=(tuple(power_points) if keep_power_points else ()),
            segment_summaries=summaries,
            total_summary=total_summary, coverage=coverage,
            model_version=self._model_version, cache_hit=False,
            diagnostics=tuple(diagnostics),
            runtime_ms=(time.perf_counter() - started) * 1000.0)
        self._remember(key, outcome)
        return outcome

    def _summaries_from_profile(self, profile: "TrajectoryProfile",
                                grid: EvaluationGrid) -> tuple[SegmentSummary, ...]:
        """不保留逐时工件时的逐航段汇总；单位能量按该段平均功率复算。"""
        order: list[str] = []
        buckets: dict[str, list[int]] = {}
        for index, segment_id in enumerate(profile.segment_ids):
            bucket = buckets.get(segment_id)
            if bucket is None:
                bucket = []
                buckets[segment_id] = bucket
                order.append(segment_id)
            bucket.append(index)
        summaries: list[SegmentSummary] = []
        for segment_id in order:
            total_seconds = 0.0
            covered = 0.0
            moving = 0.0
            waiting = 0.0
            peak: float | None = None
            energy = 0.0
            complete = True
            for index in buckets[segment_id]:
                duration = profile.durations[index]
                total_seconds += duration
                if profile.phases[index] == Phase.SAILING.value:
                    moving += duration
                else:
                    waiting += duration
                speed = profile.speeds[index]
                if profile.phases[index] == Phase.SAILING.value and speed is None:
                    complete = False
                    continue
                power = 0.0 if profile.phases[index] != Phase.SAILING.value \
                    else self.power_kw(speed)
                covered += duration
                if peak is None or power > peak:
                    peak = power
                energy += power * duration / 3600.0
            summaries.append(SegmentSummary(
                scope_id=segment_id, phase=None,
                moving_duration_s=moving, waiting_duration_s=waiting,
                predicted_motor_energy_kwh=(energy if complete else None),
                peak_motor_power_kw=peak,
                covered_seconds=covered, total_seconds=total_seconds,
                coverage_fraction=(covered / total_seconds) if total_seconds else 0.0,
                distance_km=grid.distance_km_by_segment.get(segment_id)))
        return tuple(summaries)

    def _summaries_from_points(self, points: Sequence[PowerPoint],
                               grid: EvaluationGrid) -> tuple[SegmentSummary, ...]:
        """单趟扫描即可得到逐航段汇总，避免对每个航段重复过滤整条序列。"""
        order: list[str] = []
        buckets: dict[str, list[PowerPoint]] = {}
        for point in points:
            bucket = buckets.get(point.segment_id)
            if bucket is None:
                bucket = []
                buckets[point.segment_id] = bucket
                order.append(point.segment_id)
            bucket.append(point)
        summaries: list[SegmentSummary] = []
        for segment_id in order:
            own = buckets[segment_id]
            total_seconds = 0.0
            covered = 0.0
            moving = 0.0
            waiting = 0.0
            peak: float | None = None
            energy = 0.0
            complete = True
            for point in own:
                total_seconds += point.duration_s
                if point.phase == Phase.SAILING.value:
                    moving += point.duration_s
                else:
                    waiting += point.duration_s
                power = point.predicted_motor_power_kw
                if power is None:
                    complete = False
                    continue
                covered += point.duration_s
                if peak is None or power > peak:
                    peak = power
                if point.motor_energy_kwh is not None:
                    energy += point.motor_energy_kwh
            summaries.append(SegmentSummary(
                scope_id=segment_id, phase=None,
                moving_duration_s=moving, waiting_duration_s=waiting,
                predicted_motor_energy_kwh=(energy if complete else None),
                peak_motor_power_kw=peak,
                covered_seconds=covered, total_seconds=total_seconds,
                coverage_fraction=(covered / total_seconds) if total_seconds else 0.0,
                distance_km=grid.distance_km_by_segment.get(segment_id)))
        return tuple(summaries)

    def _predict_candidate(self, candidate: CandidateTrajectory, *,
                           grid: EvaluationGrid) -> PredictionOutcome:
        """把 v2 轨迹转成轻量剖面后走同一后端逻辑，保证两条路径数值一致。"""
        profile = TrajectoryProfile(
            segment_ids=tuple(point.segment_id for point in candidate.trajectory),
            phases=tuple(point.phase for point in candidate.trajectory),
            starts=tuple(point.start_offset_s for point in candidate.trajectory),
            durations=tuple(point.duration_s for point in candidate.trajectory),
            speeds=tuple(point.planned_speed_kmh for point in candidate.trajectory),
            kinds=tuple(point.interval_kind or "" for point in candidate.trajectory),
            targets=tuple(point.target_speed_kmh for point in candidate.trajectory))
        return self.predict_profile(profile, candidate_id=candidate.candidate_id,
                                    grid=grid)
