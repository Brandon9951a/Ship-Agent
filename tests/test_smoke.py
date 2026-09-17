from core.agent import AgentV2
from core.pipeline import run_placeholder_pipeline


def test_agent_status_response() -> None:
    response = AgentV2().run("status")

    assert response.ok is True
    assert response.message == "agent_v2 is ready."


def test_placeholder_pipeline_order() -> None:
    results = run_placeholder_pipeline()

    assert [result.name for result in results] == [
        "Tdata",
        "Tseg",
        "Tenergy",
        "Tspeed",
        "Tmanagement",
    ]
    assert all(result.status == "ok" for result in results)
