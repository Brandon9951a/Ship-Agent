import json
from copy import deepcopy
from pathlib import Path

from schemas.types import InfeasibleType, Status
from ui.app import run


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
    assert result["status"] == Status.AWAITING_CHOICE.value
    tspeed = result["tspeed"]
    assert tspeed["infeasible_type"] == InfeasibleType.TIME.value
    options = result["adjustment_options"]
    assert options and options[0]["verified"] is True
    assert options[0]["modification"]["max_duration_h"] > 1
    assert "已计算方案" in result["dashboard"]
    assert "自动写入任务" in result["dashboard"]


def test_soc_infeasible_scenario_does_not_promise_unknown_charging():
    payload = deepcopy(base_payload())
    payload["soc_initial"] = 0.31
    result = run(payload)
    assert result["status"] == Status.AWAITING_CHOICE.value
    tspeed = result["tspeed"]
    assert tspeed["infeasible_type"] == InfeasibleType.SOC.value
    assert [item["direction"] for item in result["adjustment_options"]] == [
        "recharge", "give_up",
    ]
    assert "补能至实测 SOC" in result["dashboard"]
    assert "未显示航速推荐、ETA、最终能耗或 SOC 成功结论。" in result["dashboard"]
