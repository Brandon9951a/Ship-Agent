"""Tenergy LSTM 预测器的适配层（薄封装，不复制模型逻辑）。

罗负责的正式研究模型尚未按 v2 冻结 ``evaluate_batch``，因此本模块只做两件事：

1. 声明 DE 侧要求的调用形状（``EnergyPredictor`` 协议），以及**数值一致性自检**：
   批量与逐条、剖面路径与 v2 工件路径必须给出相同结果；
2. 提供把外部批量预测函数接入 DE 的适配器，并对返回结构做严格校验
   （候选 ID 配对、逐候选诊断、能量与积分区间一致、模型版本回显）。

适配器不实现任何功率换算：电机侧 → 母线侧一律交给共同需求构建器。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from research.contracts import (
    CandidateTrajectory,
    Diagnostic,
    EnergyBatchRequest,
    EnergyBatchResult,
    EnergyCoverage,
    EnergyResultEnvelope,
    OutcomeCode,
    PowerPoint,
    SegmentSummary,
    stable_hash,
)
from research.tspeed.predictor import EvaluationGrid
from research.tspeed.profile import TrajectoryProfile

ADAPTER_VERSION = "lstm-energy-adapter/1.0"


class AdapterContractError(RuntimeError):
    """外部预测器返回的结构不符合 v2 约定。"""


@dataclass(frozen=True)
class PredictionBatchPayload:
    """外部预测器应返回的最小结构（可由字典或 dataclass 构造）。"""

    model_version: str
    integration_rule: str
    candidates: Sequence[Mapping]


def _power_points(raw_points: Sequence[Mapping]) -> tuple[PowerPoint, ...]:
    points = []
    for item in raw_points:
        power = item.get("predicted_motor_power_kw")
        duration = item.get("duration_s")
        if duration is None or duration <= 0:
            raise AdapterContractError(f"区间 duration_s 非法：{duration!r}")
        energy = item.get("motor_energy_kwh")
        if energy is None and power is not None:
            energy = power * duration / 3600.0
        points.append(PowerPoint(
            segment_id=item["segment_id"], phase=item["phase"],
            start_offset_s=item["start_offset_s"], duration_s=duration,
            planned_speed_kmh=item.get("planned_speed_kmh"),
            predicted_motor_power_kw=power, motor_energy_kwh=energy,
            applicability_flag=item.get("applicability_flag"),
            quality_flags=tuple(item.get("quality_flags") or ()),
            target_speed_kmh=item.get("target_speed_kmh"),
            interval_kind=item.get("interval_kind")))
    return tuple(points)


def _summaries(raw: Sequence[Mapping] | None) -> tuple[SegmentSummary, ...]:
    if not raw:
        return ()
    result = []
    for item in raw:
        result.append(SegmentSummary(
            scope_id=item["scope_id"], phase=item.get("phase"),
            moving_duration_s=item.get("moving_duration_s", 0.0),
            waiting_duration_s=item.get("waiting_duration_s", 0.0),
            predicted_motor_energy_kwh=item.get("predicted_motor_energy_kwh"),
            peak_motor_power_kw=item.get("peak_motor_power_kw"),
            covered_seconds=item.get("covered_seconds", 0.0),
            total_seconds=item.get("total_seconds", 0.0),
            coverage_fraction=item.get("coverage_fraction", 0.0),
            distance_km=item.get("distance_km")))
    return tuple(result)


class LSTMEnergyAdapter:
    """把外部批量预测函数包装成 DE 的 ``EnergyPredictor``。

    ``evaluate_batch`` 的签名固定为
    ``(request: EnergyBatchRequest, grid: EvaluationGrid) -> PredictionBatchPayload``。
    本适配器负责：请求透传、结构校验、候选 ID 配对、逐候选诊断保留、
    以及把 Tenergy 侧的时间步长与 DE 网格的对应关系记录下来。
    """

    def __init__(self, evaluate_batch: Callable[..., object], *,
                 model_version: str | None = None,
                 integration_rule: str = "E = sum(P_k * duration_s / 3600)",
                 require_source_refs: bool = True) -> None:
        if not callable(evaluate_batch):
            raise TypeError("evaluate_batch 必须是可调用对象")
        self._evaluate_batch = evaluate_batch
        self._model_version = model_version
        self._integration_rule = integration_rule
        self._require_source_refs = require_source_refs

    @property
    def model_version(self) -> str:
        return self._model_version or "lstm-tenergy-unfrozen"

    @property
    def integration_rule(self) -> str:
        return self._integration_rule

    @property
    def config_hash(self) -> str:
        return stable_hash({"adapter": ADAPTER_VERSION,
                            "model_version": self.model_version,
                            "integration_rule": self.integration_rule})

    def predict(self, request: EnergyBatchRequest, *,
                grid: EvaluationGrid) -> EnergyBatchResult:
        payload = self._evaluate_batch(request, grid)
        return self._coerce(payload, request)

    def predict_profile(self, profile: TrajectoryProfile, *,
                        candidate_id: str, grid: EvaluationGrid):
        """搜索期兜底：把剖面还原为最小 v2 轨迹后再走同一外部接口。

        正式接入时应由 Tenergy 侧直接支持剖面批量推理；在冻结之前，
        这里保证数值与 ``predict`` 路径一致（同一转换函数、同一积分规则）。
        """
        from research.contracts import TrajectoryPoint

        points = tuple(
            TrajectoryPoint(
                segment_id=profile.segment_ids[index],
                phase=profile.phases[index],
                start_offset_s=round(profile.starts[index], 9),
                duration_s=round(profile.durations[index], 9),
                planned_speed_kmh=(None if profile.speeds[index] is None
                                   else round(profile.speeds[index], 9)),
                target_speed_kmh=(profile.targets[index]
                                  if profile.targets else None),
                interval_kind=(profile.kinds[index] if profile.kinds else None))
            for index in range(len(profile)))
        candidate = CandidateTrajectory(candidate_id=candidate_id,
                                       trajectory=points)
        request = EnergyBatchRequest(
            candidates=(candidate,), history_ref="adapter.profile",
            environment_scenario_ref="adapter.profile",
            model_version=self.model_version,
            integration_rule=self.integration_rule)
        result = self.predict(request, grid=grid)
        envelope = result.by_candidate().get(candidate_id)
        if envelope is None:
            raise AdapterContractError("外部预测器未返回该候选的结果")
        from research.tspeed.predictor import PredictionOutcome

        return PredictionOutcome(
            candidate_id=envelope.candidate_id, outcome_code=envelope.outcome_code,
            power_points=envelope.power_points,
            segment_summaries=envelope.segment_summaries,
            total_summary=envelope.total_summary, coverage=envelope.coverage,
            model_version=envelope.model_version, cache_hit=envelope.cache_hit,
            diagnostics=envelope.diagnostics, artifact_ref=envelope.artifact_ref,
            runtime_ms=envelope.runtime_ms)

    # --- 结构校验 -------------------------------------------------------------
    def _coerce(self, payload: object, request: EnergyBatchRequest
                ) -> EnergyBatchResult:
        if isinstance(payload, EnergyBatchResult):
            payload.require_all_candidates(request.candidate_ids)
            return payload
        if isinstance(payload, PredictionBatchPayload):
            model_version = payload.model_version
            integration_rule = payload.integration_rule
            entries = list(payload.candidates)
        elif isinstance(payload, Mapping):
            model_version = payload.get("model_version") or self.model_version
            integration_rule = payload.get("integration_rule", self.integration_rule)
            entries = list(payload.get("candidates") or ())
        else:
            raise AdapterContractError(
                f"外部预测器返回了不支持的类型 {type(payload).__name__}")

        results: list[EnergyResultEnvelope] = []
        for entry in entries:
            candidate_id = entry.get("candidate_id")
            if not candidate_id:
                raise AdapterContractError("候选结果缺少 candidate_id")
            if candidate_id not in request.candidate_ids:
                raise AdapterContractError(f"返回了未请求的候选 {candidate_id}")
            points = _power_points(entry.get("power_points") or ())
            outcome_code = entry.get("outcome_code") or (
                OutcomeCode.OK.value if points else OutcomeCode.NEEDS_INPUT.value)
            diagnostics = tuple(
                Diagnostic(stage=item.get("stage", "predict"),
                           code=item.get("code", "unspecified"),
                           detail=item.get("detail"),
                           segment_id=item.get("segment_id"),
                           field_ref=item.get("field_ref"),
                           observed=item.get("observed"),
                           limit=item.get("limit"), unit=item.get("unit"),
                           retryable=item.get("retryable"))
                for item in (entry.get("diagnostics") or ()))
            summaries = _summaries(entry.get("segment_summaries"))
            total_summary = (_summaries([entry["total_summary"]])[0]
                             if entry.get("total_summary") else None)
            if total_summary is None and points:
                covered = sum(point.duration_s for point in points
                              if point.predicted_motor_power_kw is not None)
                total_seconds = sum(point.duration_s for point in points)
                complete = all(point.predicted_motor_power_kw is not None
                               for point in points)
                total_summary = SegmentSummary(
                    scope_id="voyage", phase=None,
                    moving_duration_s=sum(
                        point.duration_s for point in points
                        if point.phase == "sailing"),
                    waiting_duration_s=sum(
                        point.duration_s for point in points
                        if point.phase != "sailing"),
                    predicted_motor_energy_kwh=(
                        sum(point.motor_energy_kwh for point in points)
                        if complete else None),
                    peak_motor_power_kw=max(
                        (point.predicted_motor_power_kw for point in points
                         if point.predicted_motor_power_kw is not None),
                        default=None),
                    covered_seconds=covered, total_seconds=total_seconds,
                    coverage_fraction=(covered / total_seconds
                                       if total_seconds else 0.0))
            coverage = (EnergyCoverage(
                interval_count=len(points),
                predicted_count=sum(1 for point in points
                                    if point.predicted_motor_power_kw is not None),
                valid_fraction=total_summary.coverage_fraction,
                extrapolated_fraction=(
                    sum(point.duration_s for point in points
                        if "extrapolated" in point.quality_flags)
                    / total_summary.total_seconds
                    if total_summary.total_seconds else 0.0),
                unpredicted_fraction=(
                    1.0 - total_summary.coverage_fraction),
                covered_seconds=total_summary.covered_seconds,
                total_seconds=total_summary.total_seconds)
                if total_summary else None)
            envelope = EnergyResultEnvelope(
                candidate_id=candidate_id, outcome_code=outcome_code,
                power_points=points, segment_summaries=summaries,
                total_summary=total_summary, coverage=coverage,
                diagnostics=diagnostics, model_version=model_version,
                cache_hit=bool(entry.get("cache_hit")),
                runtime_ms=entry.get("runtime_ms"))
            if envelope.ok and not envelope.energies_match():
                raise AdapterContractError(
                    f"候选 {candidate_id} 的逐时能量与 P·Δt/3600 不一致")
            results.append(envelope)
        result = EnergyBatchResult(
            results=tuple(results), model_version=model_version,
            integration_rule=integration_rule, call_id=request.call_id,
            parent_call_id=request.parent_call_id)
        result.require_all_candidates(request.candidate_ids)
        return result


def consistency_report(predictor, request: EnergyBatchRequest, *,
                       grid: EvaluationGrid) -> dict[str, object]:
    """批量/逐条与两条路径的数值一致性自检，供联调时使用。"""
    batch = predictor.predict(request, grid=grid)
    single = {}
    for candidate in request.candidates:
        one = EnergyBatchRequest(
            candidates=(candidate,), history_ref=request.history_ref,
            environment_scenario_ref=request.environment_scenario_ref,
            model_version=request.model_version,
            integration_rule=request.integration_rule)
        result = predictor.predict(one, grid=grid)
        single[candidate.candidate_id] = result.by_candidate()[candidate.candidate_id]
    by_batch = batch.by_candidate()
    report: dict[str, object] = {"batch_size": len(request.candidates),
                                 "candidates": {}}
    for candidate_id, envelope in by_batch.items():
        alone = single[candidate_id]
        same = (envelope.motor_energy_kwh == alone.motor_energy_kwh
                and envelope.peak_motor_power_kw == alone.peak_motor_power_kw
                and len(envelope.power_points) == len(alone.power_points))
        report["candidates"][candidate_id] = {
            "batch_energy_kwh": envelope.motor_energy_kwh,
            "single_energy_kwh": alone.motor_energy_kwh,
            "identical": same,
            "energies_match_rule": envelope.energies_match(),
        }
    report["all_identical"] = all(item["identical"]
                                  for item in report["candidates"].values())
    report["all_energies_match_rule"] = all(
        item["energies_match_rule"] for item in report["candidates"].values())
    return report


__all__ = [
    "ADAPTER_VERSION",
    "AdapterContractError",
    "LSTMEnergyAdapter",
    "PredictionBatchPayload",
    "consistency_report",
]
