"""Translate the actual Tenergy v2 artifact API into Tspeed's predictor protocol.

This bridge keeps the research/planning boundary explicit. Tenergy owns model
approval and its short forecast domain; rejected candidates stay rejected.
"""

from __future__ import annotations

import json
from math import isclose

from research.contracts import (
    ArtifactRef, CandidateTrajectory, Diagnostic, EnergyBatchRequest,
    EnergyBatchResult, EnergyCoverage, EnergyResultEnvelope, PowerPoint,
    SegmentSummary, TrajectoryPoint,
)
from research.tspeed.lstm_adapter import AdapterContractError
from research.tspeed.predictor import EvaluationGrid, PredictionOutcome
from research.tspeed.profile import TrajectoryProfile
from schemas.tenergy_v2 import VERSION, valid_ref
from tools.tenergy_v2 import JsonArtifactStore, TenergyV2


_PHASE_TO_TENERGY = {"sailing": "moving", "waiting": "waiting", "berthing": "stopped"}
_PHASE_FROM_TENERGY = {value: key for key, value in _PHASE_TO_TENERGY.items()}
_MODEL_QUALITY = {"provisional_csv_units", "excel_only"}


class TenergyV2Predictor:
    """An EnergyPredictor backed by TenergyV2 and verified JSON artifacts.

    ``history_ref`` and ``environment_ref`` are Tenergy ArtifactRefs. The
    Tspeed request must refer to their artifact IDs; a null environment uses
    the explicit ``no-environment`` scenario key. No synthetic history is made.
    """

    tenergy_artifact_api = True

    def __init__(self, predictor: TenergyV2, store: JsonArtifactStore, *,
                 history_ref: dict, environment_ref: dict | None,
                 snapshot_id: str, scenario_id: str, model_version: str,
                 task_id: str, usage: str = "research") -> None:
        if usage not in ("research", "planning"):
            raise ValueError("usage must be research or planning")
        if not valid_ref(history_ref) or (environment_ref is not None and not valid_ref(environment_ref)):
            raise ValueError("Tenergy history/environment references must be valid ArtifactRefs")
        store.get(history_ref, kind="history")
        if environment_ref is not None:
            store.get(environment_ref, kind="environment")
        for name, value in (("snapshot_id", snapshot_id), ("scenario_id", scenario_id),
                            ("model_version", model_version), ("task_id", task_id)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be nonempty")
        self._predictor = predictor
        self._store = store
        self._history_ref = history_ref
        self._environment_ref = environment_ref
        self._snapshot_id = snapshot_id
        self._scenario_id = scenario_id
        self._model_version = model_version
        self._task_id = task_id
        self._usage = usage

    @property
    def history_id(self) -> str:
        return self._history_ref["artifact_id"]

    @property
    def environment_id(self) -> str:
        return (self._environment_ref["artifact_id"]
                if self._environment_ref is not None else "no-environment")

    @property
    def model_version(self) -> str:
        return self._model_version

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def integration_rule(self) -> str:
        return "right_endpoint"

    @property
    def usage(self) -> str:
        return self._usage

    @property
    def planner_approved(self) -> bool:
        """Early graph gate only; Tenergy repeats the authoritative check."""
        manifest = json.loads((self._predictor.model_directory / "manifest.json").read_text(encoding="utf-8"))
        return manifest.get("planner_approved") is True and bool(manifest.get("approval_ref"))

    def predict(self, request: EnergyBatchRequest, *, grid: EvaluationGrid) -> EnergyBatchResult:
        if request.history_ref != self.history_id or request.environment_scenario_ref != self.environment_id:
            raise AdapterContractError("Tspeed history/environment ID differs from the verified Tenergy artifact")
        for name, expected in (("model_version", self.model_version),
                               ("integration_rule", self.integration_rule),
                               ("task_id", self._task_id),
                               ("snapshot_id", self._snapshot_id),
                               ("scenario_id", self._scenario_id)):
            observed = getattr(request, name)
            if observed is not None and observed != expected:
                raise AdapterContractError(f"{name} does not match the frozen Tenergy context")
        if type(request.request_revision) is not int or request.request_revision < 0:
            raise AdapterContractError("Tenergy request_revision must be a nonnegative integer")
        candidates = []
        for candidate in request.candidates:
            if candidate.time_gaps() or candidate.trajectory[0].start_offset_s != 0:
                raise AdapterContractError("Tenergy trajectory must be continuous from zero")
            rows = []
            for point in candidate.trajectory:
                phase = _PHASE_TO_TENERGY.get(point.phase)
                if phase is None:
                    raise AdapterContractError(f"Tenergy does not support phase {point.phase}")
                if point.environment_ref not in (None, self.environment_id):
                    raise AdapterContractError("interval environment differs from frozen scenario")
                if point.planned_speed_kmh is None:
                    raise AdapterContractError("unknown speed cannot be sent to Tenergy as zero")
                rows.append({"segment_id": point.segment_id, "phase": phase,
                             "start_offset_s": point.start_offset_s,
                             "duration_s": point.duration_s,
                             "planned_speed_kmh": point.planned_speed_kmh,
                             "environment_ref": None})
            candidates.append({"candidate_id": candidate.candidate_id, "trajectory": rows})
        raw = {"schema_version": VERSION, "task_id": request.task_id or self._task_id,
               "request_revision": request.request_revision,
               "snapshot_id": self._snapshot_id, "scenario_id": self._scenario_id,
               "call_id": request.call_id or "tspeed-tenergy-bridge",
               "parent_call_id": request.parent_call_id,
               "planning_origin_at": None, "usage": self._usage,
               "model_version": self.model_version, "integration_rule": self.integration_rule,
               "history_ref": self._history_ref,
               "environment_scenario_ref": self._environment_ref,
               "candidates": candidates}
        run = self._predictor.evaluate_batch(raw)
        if run["payload_ref"] is None:
            raise AdapterContractError(f"Tenergy produced no result artifact: {run['outcome_code']}")
        payload = self._store.get(run["payload_ref"], kind="energy_batch_result")
        if (payload.get("snapshot_id") != self._snapshot_id or
                payload.get("scenario_id") != self._scenario_id or
                payload.get("model_identity", {}).get("model_version") != self.model_version):
            raise AdapterContractError("Tenergy result context/model does not match request")
        entries = payload.get("candidates")
        if not isinstance(entries, list) or len(entries) != len(candidates):
            raise AdapterContractError("Tenergy returned an incomplete candidate batch")
        requested = {item.candidate_id: item for item in request.candidates}
        seen = set()
        results = []
        for entry in entries:
            cid = entry.get("candidate_id")
            if cid not in requested or cid in seen:
                raise AdapterContractError("Tenergy returned an unexpected or duplicate candidate ID")
            seen.add(cid)
            results.append(self._convert(entry, requested[cid], grid))
        result = EnergyBatchResult(tuple(results), self.model_version, self.integration_rule,
                                   call_id=request.call_id, parent_call_id=request.parent_call_id)
        result.require_all_candidates(request.candidate_ids)
        return result

    def _convert(self, entry: dict, candidate: CandidateTrajectory,
                 grid: EvaluationGrid) -> EnergyResultEnvelope:
        diagnostics = [Diagnostic(stage="Tenergy", code=item.get("code", "unknown"),
                                  detail=item.get("detail") or None,
                                  field_ref=item.get("field_ref"),
                                  segment_id=item.get("time_interval_or_segment_id"),
                                  retryable=item.get("retryable"))
                       for item in entry.get("diagnostics", [])]
        points: list[PowerPoint] = []
        ref = entry.get("power_series_ref")
        if ref is not None:
            if not valid_ref(ref):
                raise AdapterContractError("Tenergy returned an invalid power series ref")
            series = self._store.get(ref, kind="power_series")
            if (series.get("snapshot_id") != self._snapshot_id or
                    series.get("scenario_id") != self._scenario_id or
                    series.get("model_identity", {}).get("model_version") != self.model_version):
                raise AdapterContractError("power series context/model mismatch")
            raw_points = series.get("points")
            if not isinstance(raw_points, list) or len(raw_points) != len(candidate.trajectory):
                raise AdapterContractError("power series length differs from requested trajectory")
            for raw, requested in zip(raw_points, candidate.trajectory):
                coordinates_match = all(
                    isinstance(raw.get(name), (int, float)) and
                    isclose(raw[name], getattr(requested, name), rel_tol=0, abs_tol=1e-6)
                    for name in ("start_offset_s", "duration_s", "planned_speed_kmh"))
                if (raw.get("segment_id") != requested.segment_id or
                        raw.get("phase") != _PHASE_TO_TENERGY[requested.phase] or
                        not coordinates_match):
                    raise AdapterContractError("power series interval differs from candidate trajectory")
                quality = tuple(raw.get("quality_flags") or ())
                if quality and quality[0] not in (_MODEL_QUALITY | {"waiting_or_post_wait_unvalidated"}):
                    raise AdapterContractError("unrecognized Tenergy quality tag")
                flags = (("assumption",) if quality and quality[0] in _MODEL_QUALITY
                         else ("unpredicted",) if raw.get("predicted_motor_power_kw") is None else ())
                points.append(PowerPoint(
                    segment_id=requested.segment_id, phase=requested.phase,
                    start_offset_s=requested.start_offset_s, duration_s=requested.duration_s,
                    planned_speed_kmh=requested.planned_speed_kmh,
                    predicted_motor_power_kw=raw.get("predicted_motor_power_kw"),
                    motor_energy_kwh=raw.get("motor_energy_kwh"),
                    applicability_flag=raw.get("applicability_flag"), quality_flags=flags,
                    target_speed_kmh=requested.target_speed_kmh,
                    interval_kind=requested.interval_kind))
            if any((raw.get("quality_flags") or [None])[0] in _MODEL_QUALITY for raw in raw_points):
                diagnostics.append(Diagnostic(stage="Tenergy", code="provisional_model_data",
                                              detail="Model training data quality remains provisional; research use only"))
        if entry.get("outcome_code") == "ok" and len(points) != len(candidate.trajectory):
            raise AdapterContractError("successful Tenergy result has no complete power series")
        summaries = []
        for segment_id in dict.fromkeys(point.segment_id for point in points):
            own = [point for point in points if point.segment_id == segment_id]
            summaries.append(self._summary(segment_id, own, grid.distance_km_by_segment.get(segment_id)))
        distance = sum(grid.distance_km_by_segment.get(segment_id, 0.0)
                       for segment_id in dict.fromkeys(point.segment_id for point in points))
        total = self._summary("voyage", points, distance) if points else None
        if entry.get("outcome_code") == "ok":
            if total is None or total.predicted_motor_energy_kwh is None:
                raise AdapterContractError("successful Tenergy result contains unpredicted intervals")
            if not isclose(total.predicted_motor_energy_kwh, entry.get("motor_energy_kwh", float("nan")),
                           rel_tol=1e-8, abs_tol=1e-9):
                raise AdapterContractError("Tenergy total energy differs from verified power series")
        covered = sum(p.duration_s for p in points if p.predicted_motor_power_kw is not None)
        seconds = sum(p.duration_s for p in points)
        coverage = (EnergyCoverage(len(points), sum(p.predicted_motor_power_kw is not None for p in points),
                                   covered / seconds, 0.0, 1.0 - covered / seconds,
                                   covered, seconds) if points else None)
        artifact = ArtifactRef(**ref) if ref is not None else None
        result = EnergyResultEnvelope(candidate_id=candidate.candidate_id,
                                      outcome_code=entry["outcome_code"], power_points=tuple(points),
                                      segment_summaries=tuple(summaries), total_summary=total,
                                      coverage=coverage, artifact_ref=artifact,
                                      diagnostics=tuple(diagnostics), model_version=self.model_version)
        if result.ok and not result.energies_match():
            raise AdapterContractError("Tenergy interval energy differs from P * duration / 3600")
        return result

    @staticmethod
    def _summary(scope: str, points: list[PowerPoint], distance: float | None) -> SegmentSummary:
        complete = all(point.motor_energy_kwh is not None for point in points)
        seconds = sum(point.duration_s for point in points)
        covered = sum(point.duration_s for point in points if point.motor_energy_kwh is not None)
        return SegmentSummary(scope_id=scope, phase=None,
                              moving_duration_s=sum(p.duration_s for p in points if p.phase == "sailing"),
                              waiting_duration_s=sum(p.duration_s for p in points if p.phase != "sailing"),
                              predicted_motor_energy_kwh=(sum(p.motor_energy_kwh for p in points)
                                                          if complete else None),
                              peak_motor_power_kw=max((p.predicted_motor_power_kw for p in points
                                                       if p.predicted_motor_power_kw is not None), default=None),
                              covered_seconds=covered, total_seconds=seconds,
                              coverage_fraction=covered / seconds if seconds else 0.0,
                              distance_km=distance)

    def predict_profile(self, profile: TrajectoryProfile, *, candidate_id: str,
                        grid: EvaluationGrid, keep_power_points: bool = True) -> PredictionOutcome:
        points = tuple(TrajectoryPoint(
            segment_id=profile.segment_ids[i], phase=profile.phases[i],
            start_offset_s=profile.starts[i], duration_s=profile.durations[i],
            planned_speed_kmh=profile.speeds[i], environment_ref=self.environment_id,
            target_speed_kmh=profile.targets[i] if profile.targets else None,
            interval_kind=profile.kinds[i] if profile.kinds else None)
            for i in range(len(profile)))
        request = EnergyBatchRequest(candidates=(CandidateTrajectory(candidate_id, points),),
                                     history_ref=self.history_id,
                                     environment_scenario_ref=self.environment_id,
                                     model_version=self.model_version,
                                     integration_rule=self.integration_rule,
                                     task_id=self._task_id, request_revision=0,
                                     snapshot_id=self._snapshot_id, scenario_id=self._scenario_id)
        envelope = self.predict(request, grid=grid).results[0]
        outcome = PredictionOutcome(candidate_id=candidate_id, outcome_code=envelope.outcome_code,
                                    motor_energy_kwh=envelope.motor_energy_kwh,
                                    peak_power_kw=envelope.peak_motor_power_kw,
                                    power_points=envelope.power_points,
                                    segment_summaries=envelope.segment_summaries,
                                    total_summary=envelope.total_summary, coverage=envelope.coverage,
                                    model_version=envelope.model_version, artifact_ref=envelope.artifact_ref,
                                    diagnostics=envelope.diagnostics)
        return outcome if keep_power_points else outcome.without_power_points()
