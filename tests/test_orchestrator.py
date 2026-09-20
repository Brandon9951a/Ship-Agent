from schemas.messages import ToolStepResult
from schemas.types import Status, ToolResponse
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
    assert state["status"] == Status.INFEASIBLE.value
    assert state["failed_tool"] == "Tspeed"
    assert "plan" not in state


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
