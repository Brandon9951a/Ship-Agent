from core import build_workflow, run_workflow


NORMAL_TASK = (
    "从平顶山港到军李船闸，2026-09-18 09:00出发，"
    "SOC85%，半载，6小时内到达"
)


def test_public_graph_factory_builds() -> None:
    graph = build_workflow()

    assert graph is not None


def test_public_workflow_runs_real_five_tool_chain() -> None:
    state = run_workflow(NORMAL_TASK)

    assert state["status"] == "ok"
    assert [item["tool"] for item in state["tool_results"]] == [
        "Tdata",
        "Tseg",
        "Tenergy",
        "Tspeed",
        "Tmanagement",
    ]
    assert all(item["status"] == "ok" for item in state["tool_results"])
