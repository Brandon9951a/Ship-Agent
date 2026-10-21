"""Independent water/ground-speed checks and actual DE/FDP environment integration."""
import copy
import json
from dataclasses import replace

import pytest

from research.agent.environment import (EnvironmentError, RiverPowerPredictor, load_environment,
    resolve_route_environment, validate_snapshot, verify_environment_demand)
from research.agent.professional import ProfessionalTools, configuration
from research.agent.store import Store, digest
from research.demand import AuxiliaryModel, ConversionModel, DemandBuilderSpec, build_power_demand
from research.demand.builder import summarize_analytic_profile
from research.contracts import SourceRef
from research.tspeed.predictor import AnalyticPowerPredictor, EvaluationGrid
from research.tspeed.profile import TrajectoryProfile

LEG = "s01_pingdingshan_gang_to_junli"
SHORT = "s04_luohe_lock_to_luohe_port"
C = .06559425611908812


def profile(segment=LEG, speed=12, seconds=600):
    return TrajectoryProfile((segment,), ("sailing",), (0.,), (seconds,), (speed,), ("cruise",), (speed,))


def predict(env, p):
    predictor = RiverPowerPredictor(coefficient=C, environment=env)
    grid = EvaluationGrid(5, 5, {s: 1 for s in p.segment_ids})
    return predictor, grid, predictor.predict_profile(p, candidate_id="check", grid=grid)


def test_reverse_current_and_same_ground_speed_power_match_hand_calculation():
    env = load_environment("segmented_current_demo", "半载")
    forward = predict(env, profile())[2]
    reverse = predict(env, profile(LEG + "_reverse"))[2]
    assert forward.motor_energy_kwh == pytest.approx(C * (12 - .2 * 3.6)**3 / 6)
    assert reverse.motor_energy_kwh == pytest.approx(C * (12 + .2 * 3.6)**3 / 6)
    assert reverse.motor_energy_kwh > forward.motor_energy_kwh
    assert reverse.power_points[0].current_along_route_mps == -.2


def test_same_water_speed_has_equal_power_and_different_voyage_energy():
    env = load_environment("segmented_current_demo")
    a, b = 12 + .72, 12 - .72
    out_a = predict(env, profile(speed=a, seconds=3600 / a))[2]
    out_b = predict(env, profile(LEG + "_reverse", speed=b, seconds=3600 / b))[2]
    assert out_a.peak_power_kw == pytest.approx(out_b.peak_power_kw)
    assert out_b.motor_energy_kwh > out_a.motor_energy_kwh


def test_zero_current_reference_matches_original_analytic():
    p = profile()
    _, grid, river = predict(load_environment("calm_reference"), p)
    old = AnalyticPowerPredictor(coefficient=C).predict_profile(p, candidate_id="old", grid=grid)
    assert river.motor_energy_kwh == pytest.approx(old.motor_energy_kwh, abs=1e-10)
    assert river.peak_power_kw == pytest.approx(old.peak_power_kw, abs=1e-10)


def test_fast_demand_and_full_export_agree_and_preserve_ground_speeds():
    env = load_environment("segmented_current_demo", "满载")
    p = TrajectoryProfile((LEG, LEG + "_reverse", LEG), ("sailing", "sailing", "waiting"),
        (0, 300, 600), (300, 300, 600), (12, 12, 0), ("cruise", "cruise", "waiting"))
    predictor, grid, full = predict(env, p)
    spec = DemandBuilderSpec("check", conversion=ConversionModel("eff", "constant_efficiency", .94),
        auxiliary=AuxiliaryModel("aux", 5, 5, 5, source_ref=SourceRef("test", "assumption")))
    fast = summarize_analytic_profile(p, power_at_speed=predictor.power_kw,
        power_for_interval=predictor.interval_power_kw, spec=spec)
    demand = build_power_demand(full.to_envelope(), spec=spec, series_id="check")
    assert fast.demand_energy_kwh() == pytest.approx(demand.demand_energy_kwh(), abs=1e-10)
    assert fast.peak_demand_kw() == pytest.approx(demand.peak_demand_kw(), abs=1e-10)
    assert full.power_points[0].planned_speed_kmh == 12
    assert full.power_points[-1].predicted_motor_power_kw == 0
    assert demand.points[-1].demand_bus_kw == 5
    exported = [p.to_dict() for p in full.power_points]
    assert verify_environment_demand(env, env["segments"], exported, demand,
        coefficient=C, efficiency=.94, auxiliary_kw=5)["passed"]
    bad = replace(demand, points=(replace(demand.points[0], motor_power_kw=demand.points[0].motor_power_kw + 1), *demand.points[1:]))
    with pytest.raises(EnvironmentError, match="power_replay_mismatch"):
        verify_environment_demand(env, env["segments"], exported, bad,
            coefficient=C, efficiency=.94, auxiliary_kw=5)


def test_load_and_wind_channel_profiles_change_fixed_trajectory():
    energies = [predict(load_environment("segmented_current_demo", load), profile())[2].motor_energy_kwh
                for load in ("空载", "半载", "满载")]
    assert energies[0] < energies[1] < energies[2]
    env = load_environment("adverse_environment_demo")
    assert env["segments"]["s02_junli_to_mawan"]["channel_factor"] == 1.12
    assert env["segments"][LEG]["wind_factor"] > 1
    assert env["segments"][LEG + "_reverse"]["wind_factor"] == 1


def test_extra_wait_has_only_declared_auxiliary_energy_increment():
    env = load_environment("segmented_current_demo")
    predictor, _, _ = predict(env, profile())
    spec = DemandBuilderSpec("check", conversion=ConversionModel("eff", "constant_efficiency", .94),
        auxiliary=AuxiliaryModel("aux", 5, 5, 5, source_ref=SourceRef("test", "assumption")))
    totals = []
    for wait in (300, 1800):
        p = TrajectoryProfile((LEG, LEG), ("sailing", "waiting"), (0, 600), (600, wait), (12, 0))
        total = summarize_analytic_profile(p, power_at_speed=predictor.power_kw,
            power_for_interval=predictor.interval_power_kw, spec=spec)
        totals.append(total.demand_energy_kwh())
    assert totals[1] - totals[0] == pytest.approx(5 * 1500 / 3600)


def test_missing_physical_environment_is_not_silently_zero(tmp_path):
    from research.agent.professional import ROOT
    raw = json.loads((ROOT / "configs/environment_scenarios.json").read_text(encoding="utf-8"))
    del raw["physical_segments"][LEG]
    file = tmp_path / "missing.json"
    file.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(EnvironmentError, match="missing_segment_environment"):
        load_environment("segmented_current_demo", path=file)


def test_hash_valid_but_wrong_route_projection_is_rejected(tmp_path):
    store = Store(tmp_path)
    store.create("projection", "local", {})
    tool = ProfessionalTools(store)
    state = dict(task_id="projection", scenario_id="route_success", revision=1,
        request={"origin": "漯河船闸", "destination": "漯河港"}, refs={})
    try:
        for name, fn in (("Tdata", tool.data), ("Tseg", tool.segments)):
            out = fn(state, name); state["refs"][name] = out["artifact_ref"]
        body = tool.payload(state, "Tseg")
        body["route_environment"][SHORT]["current_along_route_mps"] = 1
        state["refs"]["Tseg"] = store.put("projection", "Tseg", body)
        with pytest.raises(EnvironmentError, match="projection_mismatch"):
            tool.context(state, "context")
    finally:
        store.close()


def test_fraction_answer_is_readable_percent_without_changing_metric():
    from research.agent.protocol import render_answer
    metric = {"final_soc_1": {"value": .849921, "unit": "fraction"}}
    text = render_answer({"answer_markdown": "甲组到港电量 {{final_soc_1}}。",
        "claims": [{"key": "final_soc_1", "unit": "fraction"}]}, metric)
    assert text == "甲组到港电量 85.0%。"
    assert metric["final_soc_1"]["value"] == .849921


def test_cache_binds_segment_identity_and_environment_content():
    env = load_environment("segmented_current_demo")
    predictor, grid, a = predict(env, profile())
    b = predictor.predict_profile(profile(LEG + "_reverse"), candidate_id="b", grid=grid)
    assert not b.cache_hit
    assert b.motor_energy_kwh != a.motor_energy_kwh
    assert predictor.predict_profile(profile(), candidate_id="again", grid=grid).cache_hit
    changed = copy.deepcopy(env)
    changed["segments"][LEG]["current_along_route_mps"] = .4
    changed["content_hash"] = digest({k: v for k, v in changed.items() if k != "content_hash"})
    other = RiverPowerPredictor(coefficient=C, environment=changed)
    assert other.config_hash != predictor.config_hash


@pytest.mark.parametrize("speed,leg", [(0, LEG), (.5, LEG), (14, LEG + "_reverse")])
def test_drifting_or_overpowered_candidates_are_rejected(speed, leg):
    outcome = predict(load_environment("segmented_current_demo"), profile(leg, speed))[2]
    assert outcome.outcome_code == "model_out_of_domain"
    assert outcome.motor_energy_kwh is None
    assert outcome.diagnostics[0].segment_id == leg


def test_missing_segment_and_snapshot_tampering_fail_closed():
    env = load_environment("segmented_current_demo")
    env["segments"][LEG]["current_along_route_mps"] = 0
    with pytest.raises(EnvironmentError, match="hash_mismatch"):
        validate_snapshot(env)
    outcome = predict(load_environment("segmented_current_demo"), profile("unknown"))[2]
    assert outcome.outcome_code != "ok"


def test_every_registered_subroute_has_environment_and_unique_intermediate_lock_events():
    from research.agent.professional import ROOT
    raw = json.loads((ROOT / "configs/route_facts.yaml").read_text(encoding="utf-8"))
    by_id = {s["id"]: s for s in raw["segments"]}
    env = load_environment("segmented_current_demo")
    count = 0
    for route in raw["routes"].values():
        ids = route["segment_ids"]
        for start in range(len(ids)):
            for stop in range(start + 1, len(ids) + 1):
                legs = [by_id[s] for s in ids[start:stop]]
                resolved = resolve_route_environment(env, legs)
                expected = sum("船闸" in leg["destination"] for leg in legs[:-1])
                assert sum(x["waiting_duration_s"] for x in resolved) == 300 * expected
                events = [x["lock_event_id"] for x in resolved if x["lock_event_id"]]
                assert len(events) == len(set(events))
                assert resolved[-1]["waiting_duration_s"] == 0
                count += 1
    assert count == 72


def run_voyage(directory, origin, destination, scenario="route_success", remaining=7200):
    store = Store(directory)
    store.create("env-case", "local", {})
    tool = ProfessionalTools(store)
    state = dict(task_id="env-case", scenario_id=scenario, revision=1,
        request=dict(origin=origin, destination=destination, remaining_time_s=remaining,
                     initial_soc_1=.85, initial_soc_2=.85, load_state="半载"), refs={})
    try:
        for name, fn in (("Tdata", tool.data), ("Tseg", tool.segments), ("Tspeed", tool.speed), ("Tmanagement", tool.management)):
            out = fn(state, name)
            assert out["outcome_code"] == "ok", (name, out)
            state["refs"][name] = out["artifact_ref"]
        ref, summary = tool.verify(state)
        replay = store.read("env-case", ref)["payload"]["environment_replay"]
        assert replay["passed"]
        return tool.payload(state, "Tspeed"), summary, tool.payload(state, "Tseg")
    finally:
        store.close()


@pytest.mark.parametrize("scenario", ["calm_reference", "segmented_current_demo", "adverse_current_demo", "adverse_environment_demo"])
def test_actual_de_fdp_and_independent_replay(tmp_path, scenario):
    speed, summary, seg = run_voyage(tmp_path, "漯河船闸", "漯河港", scenario)
    assert summary["safe"]
    assert .25 <= summary["final_soc_1"] <= .85
    assert .25 <= summary["final_soc_2"] <= .85
    assert speed["summary"]["bus_energy_kwh"] == pytest.approx(summary["demand_bus_energy_kwh"])
    assert speed["summary"]["distance_km"] == pytest.approx(.689)


def test_reverse_real_solvers_and_waiting_stop_start(tmp_path):
    run_voyage(tmp_path / "reverse", "漯河港", "漯河船闸")
    speed, _, segments = run_voyage(tmp_path / "wait", "马湾船闸", "漯河港", remaining=6*3600)
    rows = speed["result"]["segment_summary"]
    assert rows[0]["waiting_duration_s"] == 300
    assert rows[0]["exit_speed_kmh"] == 0
    assert rows[1]["entry_speed_kmh"] == 0
    assert segments["segments"][0]["stop_for_wait"]


def test_environment_aware_deadline_precheck_reports_computed_lower_bound(tmp_path):
    store = Store(tmp_path)
    store.create("tight", "local", {})
    tool = ProfessionalTools(store)
    state = dict(task_id="tight", scenario_id="adverse_current_demo", revision=1,
        request={"origin": "马湾船闸", "destination": "军李船闸", "remaining_time_s": 60}, refs={})
    try:
        for name, fn in (("Tdata", tool.data), ("Tseg", tool.segments)):
            out = fn(state, name); state["refs"][name] = out["artifact_ref"]
        out = tool.speed(state, "speed")
        assert out["outcome_code"] == "infeasible_under_assumptions"
        diag = out["diagnostics"][0]
        leg = tool.payload(state, "Tseg")["segments"][0]
        assert diag["minimum_duration_s"] == pytest.approx(3600 * 25 / leg["max_speed_kmh"])
    finally:
        store.close()


def test_environment_artifacts_and_verification_are_available_through_authenticated_api(tmp_path):
    from tests.test_research_agent import ScriptedModel
    from tests.test_web_frontend import AuthenticatedClient, _store
    from ui.web_server import create_app
    app = create_app(llm_client=ScriptedModel(), checkpoint_backend="memory",
        research_directory=tmp_path / "research", auth_store=_store(tmp_path / "auth.sqlite3"))
    with AuthenticatedClient(app) as client:
        response = client.post("/api/run", json={"workflow_mode": "research_agent",
            "scenario_id": "adverse_environment_demo", "task_text": "从漯河船闸到漯河港，甲组SOC85%，乙组SOC85%，半载，2小时内到达"})
        assert response.status_code == 202
        task = response.json()["thread_id"]
        app.state.research_runtime.futures[task].result(timeout=25)
        body = client.get(f"/api/runs/{task}").json()
        assert body["outcome_code"] == "research_feasible"
        refs = body["result_refs"]
        data = client.get(f"/api/runs/{task}/artifacts/{refs['Tdata']}").json()["payload"]
        assert data["config"]["environment"]["scenario_id"] == "adverse_environment_demo"
        events = client.get(f"/api/runs/{task}/events").json()["events"]
        verification = next(e for e in events if e["type"] == "plan_verified")
        check = client.get(f"/api/runs/{task}/artifacts/{verification['artifact_ref']}").json()["payload"]
        assert check["environment_replay"]["passed"]


def test_deadline_precheck_does_not_overstate_fast_boundary_search_failure(tmp_path):
    store = Store(tmp_path)
    store.create("boundary", "local", {})
    tool = ProfessionalTools(store)
    state = dict(task_id="boundary", scenario_id="route_success", revision=1,
        request={"origin": "漯河船闸", "destination": "漯河港", "current_speed_kmh": 14,
                 "remaining_time_s": 223, "objective": "min_time"}, refs={})
    try:
        for name, fn in (("Tdata", tool.data), ("Tseg", tool.segments)):
            out = fn(state, name)
            assert out["outcome_code"] == "ok", (name, out)
            state["refs"][name] = out["artifact_ref"]
        out = tool.speed(state, "speed")
        # Initial speed exceeds the local limit. D/local_limit alone is not a
        # proved lower bound; a failed search must retain its actual meaning.
        assert out["outcome_code"] in ("ok", "search_unresolved")
        assert not any(d["code"] == "deadline_below_physical_minimum" for d in out["diagnostics"])
    finally:
        store.close()


@pytest.mark.parametrize("prediction_only", [False, True])
def test_explicit_voyage_deadline_gets_full_plan_unless_prediction_only(tmp_path, prediction_only):
    from tests.test_research_agent import ScriptedModel
    from research.agent.runtime import AgentRuntime
    class PredictionModel(ScriptedModel):
        def complete_chat(self, messages, *, tools, **kwargs):
            if [t["function"]["name"] for t in tools] == ["Tenergy"]:
                return {"message": {"role": "assistant", "content": "", "tool_calls": [{
                    "id": "energy-test", "type": "function", "function": {"name": "Tenergy",
                    "arguments": json.dumps({"revision": 1, "rationale": "explicit prediction"})}}]},
                    "response_model": "scripted-test", "latency_ms": 0}
            result = super().complete_chat(messages, tools=tools, **kwargs)
            if [t["function"]["name"] for t in tools] == ["submit_request"]:
                call = result["message"]["tool_calls"][0]
                args = json.loads(call["function"]["arguments"])
                args["task_type"] = "prediction"
                call["function"]["arguments"] = json.dumps(args)
            return result
    runtime = AgentRuntime(tmp_path, PredictionModel())
    try:
        text = "从漯河港到漯河船闸，甲组电池SOC85%，乙组电池SOC65%，半载，2小时内到达"
        if prediction_only:
            text += "，只预测能耗，不做航速优化"
        result = runtime.start(text, task_type="auto", background=False)
        assert result["request"]["origin"] == "漯河港"
        assert result["request"]["destination"] == "漯河船闸"
        assert result["outcome_code"] == ("prediction_completed" if prediction_only else "research_feasible")
        assert ("Tmanagement" in result["result_refs"]) != prediction_only
    finally:
        runtime.close()


@pytest.mark.parametrize("text,expected", [
    ("从漯河港到马湾船闸，空载", {"origin": "漯河港", "destination": "马湾船闸"}),
    ("军李船闸 → 平顶山港，半载", {"origin": "军李船闸", "destination": "平顶山港"}),
    ("从漯河港到未知码头，半载", None),
    ("从漯河港到马湾船闸附近未知码头，半载", None),
    ("从漯河港到马湾船闸。改成从平顶山港到军李船闸，半载", {"origin": "平顶山港", "destination": "军李船闸"}),
    ("从漯河港到马湾船闸。改成从漯河港到未知码头，半载", None),
])
def test_registered_route_normalization_keeps_ports_and_locks_distinct(text, expected):
    from research.agent.runtime import explicit_registered_route
    assert explicit_registered_route(text) == expected
