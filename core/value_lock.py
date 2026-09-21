"""Authoritative numeric lock for the report.

Every numeric value in the report is expected to come from the plan's tool
fields, never from model prose.  This module re-verifies the report against the
plan field-by-field and unit-by-unit; on any mismatch it falls back to a
deterministic template report whose numbers are read directly from the plan.
"""

from __future__ import annotations

from typing import Any

from schemas.types import VoyagePlan


def _close(actual: Any, expected: float | None) -> bool:
    if expected is None:
        return actual is None
    if isinstance(actual, dict):
        actual = actual.get("value")
    if actual is None:
        return False
    try:
        return abs(float(actual) - expected) <= 1e-6
    except (TypeError, ValueError):
        return False


def _template_report(plan: VoyagePlan) -> dict[str, Any]:
    from core.report import build_report

    return build_report(plan, None)


def lock_report_values(report: dict[str, Any], plan: VoyagePlan) -> tuple[dict[str, Any], bool]:
    """Verify report numeric fields against plan fields; fall back on mismatch.

    Returns ``(report, True)`` when every field matches, otherwise
    ``(template_report, False)``.  The template report is built with no LLM and
    reads numbers directly from the plan, so it is correct by construction.
    """
    optimization = plan.optimization
    management = plan.management
    if optimization is None or management is None:
        return _template_report(plan), False

    summary = report.get("summary") or {}
    expected_summary = {
        "total_distance": sum(item.distance_km for item in plan.segments),
        "total_duration": optimization.total_duration_h,
        "propulsion_energy": optimization.total_energy_kwh,
        "auxiliary_energy": management.auxiliary_energy_kwh,
        "required_energy": management.required_energy_kwh,
        "soc_initial": management.soc_initial,
        "soc_final": management.soc_final,
        "soc_planning_min": management.soc_min,
        "charge_required": management.charge_required_kwh,
    }
    for key, expected in expected_summary.items():
        if not _close(summary.get(key), expected):
            return _template_report(plan), False

    energy_by_id = {item.segment_id: item for item in optimization.energy_results}
    soc_by_id = {item.segment_id: item.soc for item in management.soc_trajectory}
    for row in report.get("segments", []):
        segment_id = row.get("segment_id")
        segment = next((s for s in plan.segments if s.segment_id == segment_id), None)
        energy = energy_by_id.get(segment_id)
        if segment is None or energy is None:
            return _template_report(plan), False
        checks = {
            "distance": segment.distance_km,
            "speed": energy.speed_kmh,
            "duration": energy.duration_h,
            "propulsion_energy": energy.propulsion_energy_kwh,
            "soc_end": soc_by_id.get(segment_id),
        }
        for field, expected in checks.items():
            if not _close(row.get(field), expected):
                return _template_report(plan), False

    return report, True
