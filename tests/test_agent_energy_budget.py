from uuid import uuid4

import pytest

from research.agent.energy_budget import selected_demand_budget
from research.agent.guidance import operator_guidance
from research.agent.professional import ProfessionalTools
from research.agent.store import Store


def batteries():
    return {f"battery_{i}": {"capacity_kwh": 1000, "initial_soc": soc, "soc_min": .25,
            "discharge_efficiency": .9} for i, soc in ((1, .8), (2, .9))}


def test_bus_budget_includes_both_reserves_and_discharge_losses():
    budget = selected_demand_budget(batteries(), 1322.5393029902016)
    assert budget["available_bus_kwh"] == pytest.approx(1080)
    assert budget["shortfall_bus_kwh"] == pytest.approx(242.5393029902016)
    assert budget["insufficient"]
    assert budget["scope"] == "selected_candidate"
    assert not selected_demand_budget(batteries(), 1080)["insufficient"]
    assert not selected_demand_budget(batteries(), 1080 + 1e-8)["insufficient"]


@pytest.mark.parametrize("field,value", [("capacity_kwh", None), ("discharge_efficiency", 0),
    ("initial_soc", float("nan")), ("initial_soc", True), ("soc_min", -1)])
def test_missing_or_invalid_evidence_is_not_zero_available_energy(field, value):
    values = batteries()
    values["battery_1"][field] = value
    assert selected_demand_budget(values, 1200) is None


def test_selected_candidate_energy_failure_stops_before_fdp(tmp_path, monkeypatch):
    def forbidden(_):
        pytest.fail("FDP must not search a demand exceeding the necessary energy bound")
    monkeypatch.setattr("research.agent.professional.run_management_fdp", forbidden)
    store = Store(tmp_path)
    task = "energy-shortfall"
    store.create(task, "local", {})
    tool = ProfessionalTools(store)
    state = dict(task_id=task, scenario_id="route_success", revision=1, refs={}, request={
        "origin": "漯河船闸", "destination": "漯河港", "initial_soc_1": .2501, "initial_soc_2": .2501,
        "remaining_time_s": 7200})
    try:
        for name, fn in (("Tdata", tool.data), ("Tseg", tool.segments), ("Tspeed", tool.speed)):
            result = fn(state, name)
            assert result["outcome_code"] == "ok"
            state["refs"][name] = result["artifact_ref"]
        result = tool.management(state, "management")
        state["refs"]["Tmanagement"] = result["artifact_ref"]
        assert result["outcome_code"] == "infeasible_under_assumptions"
        diagnostic = result["diagnostics"][0]
        assert diagnostic["code"] == "selected_demand_energy_exceeds_available"
        assert diagnostic["available_bus_kwh"] == pytest.approx(.18)
        assert diagnostic["shortfall_bus_kwh"] > 1
        assert diagnostic["failure_scope"] == "selected_candidate_energy"
        guidance = operator_guidance(store, task, result["outcome_code"], state["request"], state["refs"])
        assert "这次结果只排除了当前候选方案" in guidance["suggestion"]
        assert "尚未验证可行" in guidance["suggestion"]
    finally:
        store.close()


def test_grid_budget_without_energy_shortfall_does_not_blame_charge(tmp_path):
    store = Store(tmp_path)
    store.create("grid", "local", {})
    try:
        refs = {name: store.put("grid", name, body) for name, body in {
            "Tdata": {"batteries": batteries()}, "Tspeed": {"summary": {"bus_energy_kwh": 100}},
            "Tmanagement": {"diagnostics": [{"code": "grid_unresolved", "field_ref": "grid.max_transitions"}]},
        }.items()}
        guidance = operator_guidance(store, "grid", "solver_unresolved", {}, refs)
        assert guidance["status_label"] == "计算未完成"
        assert "计算次数" in guidance["reason"]
        assert "补电" not in guidance["suggestion"]
    finally:
        store.close()


def test_saved_opaque_guidance_is_refreshed_on_authenticated_read(tmp_path):
    from tests.test_research_agent import ScriptedModel
    from tests.test_web_frontend import AuthenticatedClient, _store
    from ui.web_server import create_app
    app = create_app(llm_client=ScriptedModel(), checkpoint_backend="memory",
        research_directory=tmp_path / "research", auth_store=_store(tmp_path / "auth.sqlite3"))
    with AuthenticatedClient(app) as client:
        owner = client.get("/api/auth/me").json()["user"]["id"]
        task = str(uuid4())
        store = app.state.research_runtime.store
        original_guidance = {"title": "旧提示", "reason": "未形成可用方案", "suggestion": "请核对"}
        store.create(task, owner, {"status": "completed", "outcome_code": "solver_unresolved",
            "request": {}, "operator_guidance": original_guidance})
        refs = {name: store.put(task, name, body) for name, body in {
            "Tdata": {"batteries": batteries()},
            "Tspeed": {"summary": {"bus_energy_kwh": 1322.5393029902016}},
        }.items()}
        store.update(task, result_refs=refs)
        result = client.get(f"/api/runs/{task}").json()
        guidance = result["operator_guidance"]
        assert guidance["status_label"] == "候选方案电量不足"
        for value in ("80%", "90%", "1322.5", "1080.0", "242.5"):
            assert value in guidance["reason"]
        assert store.get(task, owner)["operator_guidance"] == original_guidance
