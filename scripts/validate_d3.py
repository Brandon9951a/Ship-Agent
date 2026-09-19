"""Reproducible synthetic D3 chain; never presented as vessel validation."""

import json

from schemas.types import DataContext, EnergyResult, OptimizationResult, Segment, SourceRef, VesselState, VoyageRequest
from tools.tenergy import EnergyModel, run_tenergy
from tools.tmanagement import run_tmanagement
from tools.tspeed import run_tspeed


def main() -> int:
    test_source = SourceRef("scripts.validate_d3", "assumption", "synthetic fixture",
                            False, "人工算例，不是豫交投001采用参数")
    segments = [
        Segment("demo-1", "甲", "乙", 8, max_speed_kmh=6, waiting_h=0, source=test_source),
        Segment("demo-2", "乙", "丙", 4, max_speed_kmh=6, waiting_h=0, source=test_source),
    ]
    request = VoyageRequest("甲", "丙", max_duration_h=2.5, soc_initial=.8,
                            load_state="synthetic-demo")
    sources = {name: test_source for name in (
        "capacity_kwh", "soc_initial", "soc_min", "max_power_kw", "auxiliary_power_kw")}
    vessel = VesselState("synthetic-vessel", 100, .8, .2, .4, 50, 2,
                         sources=sources, assumptions=["synthetic_demo_only"])
    energy = run_tenergy(segments, [4, 5],
                         EnergyModel("cubic-demo-v1", .125, 2, "total", "synthetic_demo"))
    if energy.status.value != "ok":
        print(json.dumps({"status": "failed", "at": "Tenergy", "response": energy.to_dict()},
                         ensure_ascii=False, indent=2))
        return 1
    candidates = [EnergyResult.from_dict(item) for item in energy.payload["candidate_results"]]
    speed = run_tspeed(request, segments, candidates, vessel)
    if speed.status.value != "ok":
        print(json.dumps({"status": "failed", "at": "Tspeed", "response": speed.to_dict()},
                         ensure_ascii=False, indent=2))
        return 1
    optimization = OptimizationResult.from_dict(speed.payload)
    management = run_tmanagement(request, DataContext(vessel, "synthetic-route"), optimization)
    report = {
        "status": management.status.value,
        "real_ship_validation": False,
        "chain_scope": "Tenergy -> Tspeed -> Tmanagement; parser/Tdata/Tseg/orchestrator not included",
        "units": {"distance": "km", "speed": "km/h", "power": "kW",
                  "energy": "kWh", "duration": "h", "soc": "fraction"},
        "source": test_source.to_dict(),
        "assumptions": ["所有参数为人工测试值", "只证明离散搜索和预算计算可复现"],
        "optimization": speed.payload,
        "management": management.payload,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if management.status.value == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
