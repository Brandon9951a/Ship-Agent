"""Short, evidence-bound explanations for voyage operators."""
from __future__ import annotations

import math


def _percent(value):
    return f"{value * 100:.1f}".rstrip("0").rstrip(".") + "%"


def _payload(store, task_id, refs, name):
    try:
        return store.read(task_id, refs[name])["payload"]
    except (KeyError, LookupError, ValueError, OSError):
        return {}


def operator_guidance(store, task_id, outcome, request, refs):
    """Return separate cause and next step; never infer a verified plan."""
    if outcome in (None, "research_feasible", "prediction_completed", "question_answered",
                   "task_cancelled", "task_timeout"):
        return None
    data = _payload(store, task_id, refs, "Tdata")
    speed = _payload(store, task_id, refs, "Tspeed")
    management = _payload(store, task_id, refs, "Tmanagement")
    segment = _payload(store, task_id, refs, "Tseg")
    for body in (data, segment, speed, management):
        for diagnostic in body.get("diagnostics", []):
            if diagnostic.get("code") == "environment_no_operating_speed":
                return {"title": "当前环境下没有可用运行速度",
                    "reason": "所选航段的水流和运行速度范围，与本次仿真的推进功率上限不兼容；未生成可用方案。",
                    "suggestion": "请核对本次环境情景和载况，或选择更短航段试算。若需要调整运行范围，应先确认参数依据；提高 SOC 不会提高推进电机功率上限。"}
            if diagnostic.get("code") == "environment_input_invalid":
                return {"title": "环境条件需要核对",
                    "reason": "本次环境或载况配置不完整，无法完成一致的航时和能耗计算。",
                    "suggestion": "请选用已登记的完整仿真情景；如仍不能计算，请联系技术人员补齐对应航段的环境配置。"}
    batteries = data.get("batteries") or {}
    first, second = batteries.get("battery_1") or {}, batteries.get("battery_2") or {}
    positive_demand = (speed.get("summary") or {}).get("bus_energy_kwh")
    at_floor = all(
        isinstance(b.get("initial_soc"), (int, float))
        and isinstance(b.get("soc_min"), (int, float))
        and math.isfinite(b["initial_soc"]) and math.isfinite(b["soc_min"])
        and b["initial_soc"] <= b["soc_min"] + 1e-9
        for b in (first, second))
    if (outcome in ("solver_unresolved", "infeasible_under_assumptions")
            and at_floor and isinstance(positive_demand, (int, float)) and positive_demand > 0):
        if (abs(first["initial_soc"] - second["initial_soc"]) < 1e-9
                and abs(first["soc_min"] - second["soc_min"]) < 1e-9):
            starting_point = (f"甲、乙组出发电量均为 {_percent(first['initial_soc'])}，"
                              "正好是本次仿真设定的最低允许值。")
        else:
            starting_point = (f"甲组出发电量为 {_percent(first['initial_soc'])}，乙组为 {_percent(second['initial_soc'])}；"
                              f"分别达到本次仿真的最低允许电量 {_percent(first['soc_min'])} 和 {_percent(second['soc_min'])}。")
        return {
            "title": "出发电量已到仿真下限",
            "reason": (starting_point + "本航程需要用电，两组电池在当前约束下都没有可供航行消耗的余量，"
                       "因此未生成通过核验的方案。"),
            "suggestion": ("如仅做仿真，可在快捷输入中分别提高两组电池的出发电量并重新计算。"
                           "实际航次须先补电，再录入两组实测电量重算；若无法补电，请联系调度调整供能安排。"
                           "仅延长到达时间不能解决出发电量不足。"),
        }
    if outcome == "solver_unresolved":
        if management:
            reason = "航速已完成试算，但电池功率分配尚未得到通过核验的结果；现在不能判断该航次一定可行或一定不可行。"
            suggestion = ("请核对甲、乙组出发电量，必要时补电后分别录入新数值并重新计算。"
                          "如仍无法得到方案，请联系调度或技术人员复核航段与供能安排。")
        else:
            reason = "本次航速试算未找到通过核验的候选方案；现在不能据此判断航次一定不可行。"
            suggestion = "请核对航段和到达时限后重新计算；如仍未得到方案，请联系调度或技术人员复核。"
        return {"title": "本次计算未形成可用方案", "reason": reason, "suggestion": suggestion}
    for diagnostic in (speed.get("diagnostics") or []) + (management.get("diagnostics") or []):
        if diagnostic.get("code") == "deadline_below_physical_minimum":
            minimum, limit = diagnostic.get("minimum_duration_s"), diagnostic.get("deadline_s")
            if all(isinstance(x, (int, float)) and math.isfinite(x) for x in (minimum, limit)):
                return {
                    "title": "到达时限短于仿真最快航时",
                    "reason": (f"按本次航段和仿真限速，最快约需 {minimum / 3600:.2f} 小时，"
                               f"当前给定时限为 {limit / 3600:.2f} 小时。"),
                    "suggestion": "请延长到达时限，或改选更短的连续航段后重新计算。",
                }
    if outcome in ("model_unavailable", "tool_error", "verification_failed",
                   "agent_budget_exhausted", "recovery_exhausted"):
        return {"title": "本次计算中断", "reason": "系统尚未完成方案计算或复核，没有可供执行的航速建议。",
                "suggestion": "请重试一次；如果仍未完成，请联系技术人员检查服务，并保留本次任务记录。"}
    if outcome in ("needs_input", "invalid_input", "model_out_of_domain"):
        return {"title": "需要补充或修改输入", "reason": "当前输入尚不足以完成本次仿真计算，没有可供执行的方案。",
                "suggestion": "请核对起终点、到达时限及甲乙两组电池的出发电量，修改后重新计算。"}
    if not (data or speed or management):
        return None
    return {"title": "当前条件下未生成方案", "reason": "本次仿真约束未通过核验，当前不能发布航行方案。",
            "suggestion": "请核对航段、到达时限及两组电池的出发电量，修改后重新计算。"}


def guidance_answer(guidance):
    return guidance["reason"] + "\n\n建议：" + guidance["suggestion"]
