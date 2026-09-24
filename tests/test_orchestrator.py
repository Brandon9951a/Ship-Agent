from schemas.messages import ToolStepResult
from schemas.types import InfeasibleType, Status, ToolResponse
from tools.tdata import load_config


def complete_request():
    return "从平顶山港到周口港，SOC80%，半载，20小时内到达"


def tool_map(calls, failure=None):
    names = ("Tdata", "Tseg", "Tenergy", "Tspeed", "Tmanagement")

    def make(name):
        def run(state):
            calls.append(name)
            if name == failure:
                return ToolStepResult(name, "failed", "test failure")
            return ToolStepResult(name, "ok", "test success")
        return run

    return {name: make(name) for name in names}


def test_real_langgraph_runs_five_tools_in_order():
    from core.orchestrator import run_workflow

    calls = []
    state = run_workflow(complete_request(), tool_map(calls), tool_mode="test")
    assert calls == ["Tdata", "Tseg", "Tenergy", "Tspeed", "Tmanagement"]
    assert state["status"] == "ok"
    assert [item["node"] for item in state["trace"]] == [
        "parse", "Tdata", "Tseg", "Tenergy", "Tspeed", "Tmanagement", "finalize"
    ]


def test_missing_request_fields_stop_before_tools():
    from core.orchestrator import run_workflow

    calls = []
    state = run_workflow("从平顶山港到周口港", tool_map(calls), tool_mode="test")
    assert calls == []
    assert state["status"] == Status.NEED_CLARIFICATION.value
    assert state["questions"]
    assert "需要补充信息" in state["final_message"]


def test_tool_failure_stops_following_tools():
    from core.orchestrator import run_workflow

    calls = []
    state = run_workflow(complete_request(), tool_map(calls, failure="Tseg"), tool_mode="test")
    assert calls == ["Tdata", "Tseg"]
    assert state["status"] == Status.FAILED.value
    assert state["failed_tool"] == "Tseg"
    assert "未生成航行方案" in state["final_message"]


def test_tool_clarification_is_propagated_and_stops():
    from core.orchestrator import run_workflow

    calls = []
    tools = tool_map(calls)

    def clarify(state):
        calls.append("Tenergy")
        return ToolResponse(
            "Tenergy", Status.NEED_CLARIFICATION,
            missing_fields=["model"], questions=["请提供已批准的能耗模型。"],
        )

    tools["Tenergy"] = clarify
    state = run_workflow(complete_request(), tools, tool_mode="test")
    assert calls == ["Tdata", "Tseg", "Tenergy"]
    assert state["missing_fields"] == ["model"]
    assert state["questions"] == ["请提供已批准的能耗模型。"]


def test_default_graph_runs_real_synthetic_demo_chain_and_builds_plan():
    from core.orchestrator import run_workflow

    state = run_workflow(
        "从平顶山港到军李船闸，2026-09-21 08:00出发，"
        "2026-09-21 11:00到达，SOC85%，半载"
    )
    assert state["status"] == "ok"
    assert state["plan"]["management"]["safe"] is True
    assert state["plan"]["optimization"]["eta"].startswith("2026-09-21T10:45")
    assert "synthetic_demo" in state["final_message"]


def test_default_graph_stops_on_tight_time_constraint():
    from core.orchestrator import run_workflow

    state = run_workflow("从平顶山港到军李船闸，SOC85%，半载，2小时内到达")
    assert state["status"] == Status.AWAITING_CHOICE.value
    assert state["failed_tool"] == "Tspeed"
    assert "plan" not in state
    assert state["decision"]["type"] == "adjustment_choice"
    assert state["pending_decision_id"].endswith(":0")
    assert all("option_id" in item for item in state["adjustment_options"])


def test_demo_model_is_explicit_single_point_synthetic_anchor():
    from pathlib import Path

    config = load_config(Path(__file__).resolve().parents[1] / "configs/demo_policy.yaml")
    model = config["voyage_demo"]["energy_model"]
    assert model["usage"] == "synthetic_demo"
    assert model["energy_scope"] == "propulsion"
    assert model["historical_data_role"] == "coverage_check_only_not_calibration"
    modeled = model["coefficient_kw_per_kmh3"] * model["anchor_speed_kmh"] ** 3
    assert abs(modeled - model["anchor_propulsion_power_kw"]) < 1e-9


def test_adapter_names_must_match_contract():
    from core.orchestrator import build_workflow

    try:
        build_workflow({})
    except ValueError as error:
        assert "missing" in str(error)
    else:
        raise AssertionError("Incomplete adapters must be rejected")


def test_give_up_resumes_to_terminal_infeasible_without_recalculation():
    from uuid import uuid4

    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    from core.orchestrator import build_workflow

    thread_id = str(uuid4())
    graph = build_workflow(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": thread_id}}
    blocked = graph.invoke({
        "user_input": "从平顶山港到军李船闸，SOC85%，半载，2小时内到达",
        "trace": [], "tool_results": [], "decision_history": [],
        "tool_mode": "synthetic_demo", "thread_id": thread_id,
        "replan_count": 0, "max_replans": 2,
    }, config)
    before = len(blocked["tool_results"])
    finished = graph.invoke(Command(resume={
        "decision_id": blocked["decision"]["decision_id"],
        "option_id": "give_up",
        "selected_at": "2026-09-24T00:00:00+00:00",
    }), config)
    assert finished["status"] == Status.INFEASIBLE.value
    assert len(finished["tool_results"]) == before
    assert finished["decision_history"][-1]["option_id"] == "give_up"
    assert finished["replan_count"] == 0
    assert "船员选择保留" in finished["final_message"]


def test_replanning_stops_after_two_failed_adjustments():
    from uuid import uuid4

    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    from core.orchestrator import _default_tools, build_workflow

    tools = _default_tools()
    original_speed = tools["Tspeed"]

    def stubborn_speed(state):
        result = original_speed(state)
        if not state.get("thread_id"):
            return result
        payload = dict(result.payload or {})
        payload.update(
            feasible=False,
            infeasible_type=InfeasibleType.TIME,
            reason="测试：外部条件变化后仍不可行。",
        )
        return ToolResponse(
            "Tspeed", Status.INFEASIBLE, payload=payload,
            reason="测试：外部条件变化后仍不可行。",
            infeasible_type=InfeasibleType.TIME,
        )

    tools["Tspeed"] = stubborn_speed
    thread_id = str(uuid4())
    graph = build_workflow(tools, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": thread_id}}
    state = graph.invoke({
        "user_input": "从平顶山港到周口港，SOC85%，半载，20小时内到达",
        "trace": [], "tool_results": [], "decision_history": [],
        "tool_mode": "synthetic_demo", "thread_id": thread_id,
        "replan_count": 0, "max_replans": 2,
    }, config)
    for expected_round in (1, 2):
        assert state["status"] == Status.AWAITING_CHOICE.value
        assert state["decision"]["round"] == expected_round
        option = next(
            item for item in state["adjustment_options"]
            if item["option_id"] != "give_up"
        )
        state = graph.invoke(Command(resume={
            "decision_id": state["decision"]["decision_id"],
            "option_id": option["option_id"],
            "selected_at": "2026-09-24T00:00:00+00:00",
        }), config)
    assert state["status"] == Status.INFEASIBLE.value
    assert state["replan_count"] == 2
    assert state["replan_limit_reached"] is True
    assert state["decision"] == {}
    assert "两轮调整上限" in state["final_message"]
