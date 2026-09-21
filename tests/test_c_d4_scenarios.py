import json
from copy import deepcopy
from pathlib import Path

from schemas.types import InfeasibleType, Status
from ui.app import _adjustment_options, run


ROOT = Path(__file__).resolve().parents[1]


def base_payload():
    return json.loads((ROOT / "configs/examples/voyage_request.json").read_text(encoding="utf-8"))


def test_normal_scenario_has_final_report_and_no_adjustment_prompt():
    result = run(base_payload())
    assert result["status"] == Status.OK.value
    assert result["report"]["scope"] == "synthetic_demo"
    assert "可选调整" not in result["dashboard"]
    assert result["report"]["summary"]["eta"]["unit"] == "ISO 8601"


def test_time_infeasible_scenario_shows_bounded_choices():
    payload = deepcopy(base_payload())
    payload["max_duration_h"] = 1
    result = run(payload)
    assert result["status"] == Status.INFEASIBLE.value
    tspeed = result["tspeed"]
    assert tspeed["infeasible_type"] == InfeasibleType.TIME.value
    options = _adjustment_options(tspeed)
    assert options and "到达截止时间" in options[0]
    assert "可选调整" in result["dashboard"]
    assert "不自动修改硬安全下限" in result["dashboard"]


def test_soc_infeasible_scenario_does_not_promise_unknown_charging():
    payload = deepcopy(base_payload())
    payload["soc_initial"] = 0.31
    result = run(payload)
    assert result["status"] == Status.INFEASIBLE.value
    tspeed = result["tspeed"]
    assert tspeed["infeasible_type"] == InfeasibleType.SOC.value
    assert any("充电地点" in option for option in _adjustment_options(tspeed))
    assert "补充有来源的充电地点" in result["dashboard"]
    assert "未显示航速推荐、ETA、最终能耗或 SOC 成功结论。" in result["dashboard"]
