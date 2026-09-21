"""Tests for the authoritative numeric lock on the report (D4 task 16)."""

import json
from copy import deepcopy
from pathlib import Path

from core.orchestrator import run_structured_workflow
from core.value_lock import lock_report_values
from schemas.types import VoyagePlan

ROOT = Path(__file__).resolve().parents[1]


def _normal_result():
    payload = json.loads(
        (ROOT / "configs/examples/voyage_request.json").read_text(encoding="utf-8")
    )
    state = run_structured_workflow(payload)
    plan = VoyagePlan.from_dict(state["plan"])
    return state["report"], plan


def test_valid_report_passes_the_lock():
    report, plan = _normal_result()
    locked, passed = lock_report_values(report, plan)
    assert passed is True
    assert locked == report


def test_tampered_summary_number_falls_back_to_template():
    report, plan = _normal_result()
    tampered = deepcopy(report)
    tampered["summary"]["total_distance"]["value"] = 999.0
    locked, passed = lock_report_values(tampered, plan)
    assert passed is False
    assert locked != tampered
    # 模板报告的数字必然来自 plan 字段,不可被篡改值污染
    assert locked["summary"]["total_distance"]["value"] != 999.0


def test_tampered_segment_number_falls_back_to_template():
    report, plan = _normal_result()
    tampered = deepcopy(report)
    tampered["segments"][0]["speed"]["value"] = 999.0
    locked, passed = lock_report_values(tampered, plan)
    assert passed is False
    assert locked["segments"][0]["speed"]["value"] != 999.0
