from schemas.types import SourceRef, Status, ToolResponse
from ui.app import render_dashboard, run


SOURCE = SourceRef("route-ppt", "document", "slide 1", confirmed=True)


def test_dashboard_shows_partial_segment_and_stops_success_claim():
    rendered = render_dashboard([
        ToolResponse("Tdata", Status.NEED_CLARIFICATION,
                     payload={"route_id": "route-demo", "parameters": {}},
                     missing_fields=["route.max_speed_kmh"], questions=["请确认限速。"]).to_dict(),
        ToolResponse("Tseg", Status.NEED_CLARIFICATION,
                     payload={"segments": [{
                         "segment_id": "s01", "origin": "甲港", "destination": "乙闸",
                         "distance_km": 1.0, "max_speed_kmh": None, "min_speed_kmh": None,
                         "waiting_h": 0.0, "source": SOURCE.to_dict(), "assumptions": [],
                     }]},
                     missing_fields=["segments.s01.max_speed_kmh"], questions=["请确认限速。"]).to_dict(),
    ])
    assert "甲港 -> 乙闸" in rendered
    assert "route-ppt（已核对；slide 1）" in rendered
    assert "未显示航速推荐、ETA、最终能耗或 SOC 成功结论。" in rendered


def test_dashboard_labels_energy_candidates_not_final_result():
    rendered = render_dashboard([
        ToolResponse("Tenergy", Status.OK, payload={"candidate_results": [{
            "segment_id": "s01", "speed_kmh": 8.0, "power_kw": 10.0,
            "duration_h": 2.0, "energy_kwh": 20.0, "energy_scope": "total",
            "model_id": "synthetic", "peak_power_kw": 12.0, "assumptions": [],
            "propulsion_energy_kwh": 15.0, "auxiliary_energy_kwh": 5.0,
            "source": SOURCE.to_dict(), "model_approval_ref": None,
        }]}).to_dict()
    ])
    assert "能耗候选（非最终优化/安全结论）" in rendered
    assert "仍需 Tspeed、Tmanagement 和最终方案校验" in rendered


def test_run_includes_final_dashboard_from_real_tool_outputs():
    result = run({
        "origin": "平顶山港", "destination": "军李船闸",
        "departure_at": "2026-09-21T08:00:00+08:00",
        "arrival_deadline": "2026-09-21T11:00:00+08:00",
        "soc_initial": 0.85, "load_state": "半载", "environment": {},
    })
    assert "dashboard" in result
    assert "[Tdata]" in result["dashboard"]
    assert "最终航行方案（synthetic_demo，仅软件仿真）" in result["dashboard"]
    assert "ETA:" in result["dashboard"]
