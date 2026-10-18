"""Native-call protocol tests with actual professional solvers and durable storage."""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from research.agent.runtime import AgentRuntime
from research.agent.store import Store
from research.agent.professional import ProfessionalTools, configuration
from research.agent.auxiliary import calculate, query_results, search
from research.agent.protocol import render_answer
from core.llm_layer import LLMTransportError


class ScriptedModel:
    """Only automated tests use this model. Every solver below is real."""
    config = SimpleNamespace(enabled=True, model="scripted-test")

    def __init__(self, *, missing=False, malicious=False, invalid_answer=False, objective="min_energy"):
        self.missing = missing
        self.malicious = malicious
        self.invalid_answer = invalid_answer
        self.objective = objective
        self.calls = []

    def complete_chat(self, messages, *, tools, **kwargs):
        names = [x["function"]["name"] for x in tools]
        if names == ["submit_request"]:
            self.calls.append("parse")
            destination = None if self.missing and self.calls.count("parse") == 1 else "漯河港"
            args = {"task_type": "planning", "origin": "漯河船闸", "destination": destination,
                    "objective": self.objective}
            name = "submit_request"
        elif names == ["submit_answer"]:
            name = "submit_answer"
            args = {"answer_markdown": "航程 999 km 已获批准。" if self.invalid_answer else "研究工具已执行，结果请结合仿真假设验收。", "claims": []}
        else:
            context = json.loads(messages[-1]["content"].split("：",1)[1])
            name = next((n for n in ["Tdata","ask_clarification","Tseg","Tspeed","Tmanagement",
                "prepare_final","finish_without_plan"] if n in names), "search_project_docs")
            if self.malicious:
                name = "Tmanagement"
            args = {"revision": context["revision"], "rationale": "test decision"}
            if name == "ask_clarification":
                args["question"] = "请补充目的地。"
            if name == "finish_without_plan":
                args["reason"] = "工具诊断要求停止"
            if name == "search_project_docs":
                args["query"] = "模型限制"
            self.calls.append(name)
        return {"message":{"role":"assistant","content":"","tool_calls":[{
            "id":str(uuid4()),"type":"function","function":{"name":name,"arguments":json.dumps(args)}}]},
            "response_model":"scripted-test","request_id":str(uuid4()),"latency_ms":0}


def test_named_voyage_scenario_matches_economic_power_and_range_anchor():
    cfg = configuration("route_success")
    cruise = cfg["min_speed_kmh"]
    motor_kw = cfg["coefficient"] * cruise**3
    battery = cfg["battery"]
    battery_draw_kw = (motor_kw / cfg["conversion_efficiency"] + cfg["auxiliary_kw"]) / battery["discharge_efficiency"]
    range_to_alarm_km = (1 - battery["soc_min"]) * 2 * battery["capacity_kwh"] / battery_draw_kw * cruise
    assert cruise / 1.852 == pytest.approx(6)
    assert cfg["max_speed_kmh"] / 1.852 == pytest.approx(7.8)
    assert motor_kw == pytest.approx(90)
    assert cfg["coefficient"] * cfg["max_speed_kmh"]**3 <= 200
    assert 130 <= range_to_alarm_km <= 150


def test_named_lock_approach_profile_is_applied_to_solver_input(tmp_path):
    store = Store(tmp_path)
    store.create("approach-profile", "local", {})
    tools = ProfessionalTools(store)
    state = {"task_id": "approach-profile", "scenario_id": "route_success", "revision": 1,
             "request": {"origin": "平顶山港", "destination": "大路李船闸"}, "refs": {}}
    try:
        for name, fn in (("Tdata", tools.data), ("Tseg", tools.segments)):
            result = fn(state, name)
            assert result["outcome_code"] == "ok"
            state["refs"][name] = result["artifact_ref"]
        segments = tools.payload(state, "Tseg")["segments"]
        approach = next(item for item in segments if item["segment_id"].startswith("s04_"))
        assert approach["min_speed_kmh"] == 2.0
        assert approach["max_speed_kmh"] == pytest.approx(11.112)
        assert any("segment_operating_profiles" in ref["source_id"] for ref in approach["source_refs"])
        ordinary = [item for item in segments if item["segment_id"] != approach["segment_id"]]
        assert len({round(item["min_speed_kmh"], 3) for item in ordinary}) == len(ordinary)
        assert all(10 <= item["min_speed_kmh"] <= 14.4456 for item in ordinary)
        assert all(any("speed_perturbation" in ref["source_id"] for ref in item["source_refs"])
                   for item in ordinary)
    finally:
        store.close()


def test_seeded_speed_perturbation_keeps_reverse_lock_handoffs_valid(tmp_path):
    store = Store(tmp_path)
    store.create("reverse-perturbation", "local", {})
    tools = ProfessionalTools(store)
    state = {"task_id": "reverse-perturbation", "scenario_id": "route_success", "revision": 1,
             "request": {"origin": "周口港", "destination": "平顶山港"}, "refs": {}}
    try:
        for name, fn in (("Tdata", tools.data), ("Tseg", tools.segments)):
            result = fn(state, name)
            assert result["outcome_code"] == "ok"
            state["refs"][name] = result["artifact_ref"]
        segments = tools.payload(state, "Tseg")["segments"]
        assert len(segments) == 8
        assert all(current["min_speed_kmh"] <= previous["max_speed_kmh"]
                   for previous, current in zip(segments, segments[1:]))
        assert all(previous["min_speed_kmh"] <= current["max_speed_kmh"]
                   for previous, current in zip(segments, segments[1:]))
        assert segments[5]["min_speed_kmh"] == pytest.approx(11.112)
    finally:
        store.close()


def test_named_multisegment_voyage_has_real_speed_difference_and_verified_soc(tmp_path):
    store = Store(tmp_path)
    store.create("varying-voyage", "local", {})
    tools = ProfessionalTools(store)
    state = {"task_id": "varying-voyage", "scenario_id": "route_success", "revision": 1,
             "request": {"origin": "平顶山港", "destination": "大路李船闸",
                         "remaining_time_s": 104421.6, "initial_soc_1": .85, "initial_soc_2": .85},
             "refs": {}}
    try:
        for name, fn in (("Tdata", tools.data), ("Tseg", tools.segments),
                         ("Tspeed", tools.speed), ("Tmanagement", tools.management)):
            result = fn(state, name)
            assert result["outcome_code"] == "ok"
            state["refs"][name] = result["artifact_ref"]
        speed = tools.payload(state, "Tspeed")["summary"]
        assert speed["speed_vector_kmh"][3] < min(speed["speed_vector_kmh"][:3])
        assert len({round(value, 3) for value in speed["speed_vector_kmh"]}) == 5
        assert all(abs(value - 11.112) <= 3 for i, value in enumerate(speed["speed_vector_kmh"])
                   if i != 3)
        assert speed["distance_km"] == pytest.approx(117.889)
        _, summary = tools.verify(state)
        assert summary["safe"] is True
        assert summary["final_soc_1"] >= .25
        assert summary["final_soc_2"] >= .25
        assert summary["demand_bus_energy_kwh"] == pytest.approx(speed["bus_energy_kwh"])
    finally:
        store.close()


def test_malformed_model_request_arguments_get_one_correction_turn(tmp_path):
    class MalformedOnce:
        config = SimpleNamespace(enabled=True, model="scripted-test")

        def __init__(self):
            self.calls = 0

        def complete_chat(self, messages, *, tools, **kwargs):
            self.calls += 1
            arguments = ('{"task_type":"planning","origin":"漯河船闸",'
                         '"destination":"漯河港","objective": min_time}'
                         if self.calls == 1 else json.dumps({
                             "task_type": "planning", "origin": "漯河船闸",
                             "destination": "漯河港", "objective": "min_time"}))
            return {"message": {"role": "assistant", "tool_calls": [{
                "id": str(uuid4()), "type": "function", "function": {
                    "name": "submit_request", "arguments": arguments}}]},
                "response_model": "scripted-test"}

    client = MalformedOnce()
    runtime = AgentRuntime(tmp_path, client)
    try:
        runtime.store.create("parse-repair", "local", {"status": "running"})
        parsed = runtime._parse({"task_id": "parse-repair", "scenario_id": "route_success",
                                 "task_type": "auto", "revision": 1,
                                 "messages": [{"role": "user", "content": "从漯河船闸到漯河港，优先最短时间"}]})
        assert parsed["request"]["objective"] == "min_time"
        assert client.calls == 2
        assert any(e["type"] == "model_arguments_rejected" for e in runtime.store.events("parse-repair"))
    finally:
        runtime.close()


@pytest.mark.parametrize("model_seconds", [None, 29.006, 3600])
def test_explicit_arrival_limit_survives_model_parse_error(tmp_path, model_seconds):
    class DeadlineModel(ScriptedModel):
        def complete_chat(self, messages, *, tools, **kwargs):
            if [item["function"]["name"] for item in tools] == ["submit_request"]:
                args = {"task_type": "planning", "origin": "军李船闸",
                        "destination": "葫芦湾船闸", "remaining_time_s": model_seconds}
                return {"message": {"role": "assistant", "content": "", "tool_calls": [{
                    "id": str(uuid4()), "type": "function", "function": {
                        "name": "submit_request", "arguments": json.dumps(args, ensure_ascii=False)}}]},
                    "response_model": "scripted-test", "request_id": str(uuid4()), "latency_ms": 0}
            return super().complete_chat(messages, tools=tools, **kwargs)

    runtime = AgentRuntime(tmp_path, DeadlineModel())
    try:
        runtime.store.create("deadline-parse", "local", {"status": "running"})
        parsed = runtime._parse({"task_id": "deadline-parse", "scenario_id": "route_success",
                                 "task_type": "auto", "revision": 1,
                                 "messages": [{"role": "user", "content":
                                               "从军李船闸到葫芦湾船闸，2026-10-19 09:00出发，"
                                               "SOC85%，半载，29.006小时内到达"}]})
        assert parsed["request"]["remaining_time_s"] == pytest.approx(104421.6)
        assert runtime.store.get("deadline-parse")["request"]["remaining_time_s"] == pytest.approx(104421.6)
        if model_seconds != 104421.6:
            assert any(event["type"] == "input_normalized" for event in runtime.store.events("deadline-parse"))
    finally:
        runtime.close()


def test_actual_solver_loop_and_persisted_evidence(tmp_path):
    runtime = AgentRuntime(tmp_path, ScriptedModel())
    try:
        result = runtime.start("从漯河船闸到漯河港", background=False)
        assert result["outcome_code"] == "research_feasible"
        assert result["answer_status"] == "accepted"
        assert result["metrics"]["final_soc_1"]["value"] > .2
        events = runtime.store.events(result["task_id"])
        assert any(e["type"] == "plan_verified" for e in events)
        assert any(e.get("parent_call_id") and e.get("tool") == "Tenergy" for e in events)
    finally:
        runtime.close()


def test_invalid_model_numbers_keep_human_readable_verified_summary(tmp_path):
    runtime = AgentRuntime(tmp_path, ScriptedModel(invalid_answer=True))
    try:
        result = runtime.start("从漯河船闸到漯河港", background=False)
        assert result["outcome_code"] == "research_feasible"
        assert result["answer_status"] == "rejected"
        assert "研究仿真方案已通过工具复核" in result["final_answer"]
        assert "999" not in result["final_answer"]
        assert "source_ref" not in result["final_answer"]
        assert any(event["type"] == "model_completed" for event in runtime.store.events(result["task_id"]))
    finally:
        runtime.close()


def test_min_time_objective_reoptimizes_and_passes_fdp(tmp_path):
    runtime = AgentRuntime(tmp_path, ScriptedModel(objective="min_time"))
    try:
        result = runtime.start("从漯河船闸到漯河港，优先最短航时", background=False)
        assert result["outcome_code"] == "research_feasible"
        assert result["request"]["objective"] == "min_time"
        speed = runtime.store.read(result["task_id"], result["result_refs"]["Tspeed"])["payload"]
        assert speed["result"]["objective_terms"]["total"] == pytest.approx(speed["summary"]["duration_s"])
        assert speed["summary"]["speed_vector_kmh"]
        assert result["metrics"]["final_soc_1"]["value"] > .2
    finally:
        runtime.close()


def test_wait_restart_resume_and_duplicate(tmp_path):
    model = ScriptedModel(missing=True)
    runtime = AgentRuntime(tmp_path, model)
    result = runtime.start("规划航行", background=False)
    assert result["status"] == "waiting_human"
    pending = result["pending"]
    runtime.close()
    runtime = AgentRuntime(tmp_path, model)
    try:
        args = dict(pending_id=pending["pending_id"], revision=1, response_id="reply", reply_text="到漯河港", background=False)
        with pytest.raises(ValueError, match="stale"):
            runtime.resume(result["task_id"],"local", **{**args,"revision":0})
        done = runtime.resume(result["task_id"], "local", **args)
        assert done["outcome_code"] == "research_feasible"
        assert done["revision"] == 2
        duplicate = runtime.resume(result["task_id"], "local", **args)
        assert duplicate["answer_ref"] == done["answer_ref"]
        assert len([e for e in runtime.store.events(result["task_id"]) if e["type"]=="human_response"]) == 1
        with pytest.raises(ValueError, match="conflict"):
            runtime.resume(result["task_id"],"local", **{**args,"reply_text":"另一目的地"})
    finally:
        runtime.close()


def test_forbidden_skip_fdp_never_executes(tmp_path):
    runtime = AgentRuntime(tmp_path, ScriptedModel(malicious=True))
    try:
        result=runtime.start("规划", background=False)
        events=runtime.store.events(result["task_id"])
        assert result["outcome_code"]=="agent_budget_exhausted"
        assert len([e for e in events if e["type"]=="action_rejected"])==3
        assert not any(e["type"]=="tool_started" for e in events)
    finally:
        runtime.close()


@pytest.mark.parametrize("scenario,expected", [("fdp_reject","solver_unresolved"),("missing_conversion","needs_input")])
def test_failures_never_publish_plan(tmp_path,scenario,expected):
    runtime=AgentRuntime(tmp_path,ScriptedModel())
    try:
        result=runtime.start("到漯河港",scenario_id=scenario,background=False)
        assert result["outcome_code"] == expected
        assert result["status"]=="completed"
        assert not any(e["type"]=="plan_verified" for e in runtime.store.events(result["task_id"]))
        if scenario=="fdp_reject":
            assert runtime.client.calls.count("Tspeed")==2
    finally:
        runtime.close()


def test_fdp_grid_budget_ends_as_solver_unresolved_without_model_budget_exhaustion(tmp_path):
    runtime = AgentRuntime(tmp_path, ScriptedModel())
    try:
        runtime.store.create("task-grid-unresolved", "local", {"status": "running"})
        state = {
            "task_id": "task-grid-unresolved",
            "scenario_id": "route_success",
            "outcomes": {"Tdata": "ok", "Tseg": "ok", "Tspeed": "ok",
                         "Tmanagement": "grid_unresolved"},
            "steps": 0, "invalid_actions": 0, "task_type": "planning",
            "request": {"origin": "漯河船闸", "destination": "漯河港"},
            "observations": [],
        }
        decision = runtime._reconcile(state)
        assert decision == {"route": "finish", "outcome_code": "solver_unresolved"}
        events = runtime.store.events("task-grid-unresolved")
        assert events[-1]["type"] == "solver_unresolved"
    finally:
        runtime.close()


def test_registered_long_route_reports_deadline_bound_and_change_option(tmp_path):
    store = Store(tmp_path)
    store.create("long-route", "local", {})
    tools = ProfessionalTools(store)
    state = {"task_id": "long-route", "scenario_id": "route_success", "revision": 1,
             "request": {"origin": "平顶山港", "destination": "军李船闸"}, "refs": {}}
    try:
        for name, fn in (("Tdata", tools.data), ("Tseg", tools.segments)):
            result = fn(state, name)
            assert result["outcome_code"] == "ok"
            state["refs"][name] = result["artifact_ref"]
        result = tools.speed(state, "speed")
        assert result["outcome_code"] == "infeasible_under_assumptions"
        diagnostic = result["diagnostics"][0]
        assert diagnostic["code"] == "deadline_below_physical_minimum"
        assert diagnostic["minimum_duration_s"] > diagnostic["deadline_s"]
        assert diagnostic["deadline_source"] == "named_simulation"
        assert "延长到达期限" in diagnostic["detail"]
    finally:
        store.close()


def test_adaptive_fdp_result_replays_with_effective_grid(tmp_path, monkeypatch):
    from dataclasses import replace
    import research.agent.professional as professional_module
    from schemas.research_management import BatteryManagementRequest
    from tools.tmanagement_fdp import run_management_fdp

    store = Store(tmp_path)
    store.create("adaptive-grid", "local", {})
    tools = ProfessionalTools(store)
    state = {"task_id": "adaptive-grid", "scenario_id": "route_success", "revision": 1,
             "request": {"origin": "漯河船闸", "destination": "漯河港"}, "refs": {}}
    try:
        for name, fn in (("Tdata", tools.data), ("Tseg", tools.segments),
                         ("Tspeed", tools.speed)):
            result = fn(state, name)
            assert result["outcome_code"] == "ok"
            state["refs"][name] = result["artifact_ref"]
        speed = tools.payload(state, "Tspeed")
        original = BatteryManagementRequest.from_dict(speed["fdp_request"])
        effective = replace(original, grid=replace(original.grid, soc_step=0.1,
            algorithm_version=original.grid.algorithm_version + "+bounded-adaptive-retry"))
        retry_result = run_management_fdp(effective)
        assert retry_result["outcome_code"] == "ok"
        monkeypatch.setattr(professional_module, "run_management_fdp", lambda _request: retry_result)
        result = tools.management(state, "management")
        assert result["outcome_code"] == "ok"
        state["refs"]["Tmanagement"] = result["artifact_ref"]
        saved = tools.payload(state, "Tmanagement")
        assert saved["original_request"]["grid"]["soc_step"] == 0.05
        assert saved["request"]["grid"]["soc_step"] == 0.1
        assert tools.verify(state)[1]["safe"] is True
    finally:
        store.close()


def test_deadline_failure_fallback_keeps_specific_reason_and_adjustment(tmp_path):
    runtime = AgentRuntime(tmp_path, ScriptedModel(invalid_answer=True))
    try:
        runtime.store.create("deadline-final", "local", {"status": "running"})
        runtime._finish({"task_id": "deadline-final", "scenario_id": "route_success", "task_type": "planning",
                         "outcome_code": "infeasible_under_assumptions", "metrics": {},
                         "request": {"origin": "平顶山港", "destination": "军李船闸"},
                         "messages": [{"role": "user", "content": "1 小时内到达"}], "refs": {},
                         "observations": [{"tool": "Tspeed", "outcome_code": "infeasible_under_assumptions",
                                           "diagnostics": [{"code": "deadline_below_physical_minimum",
                                                            "minimum_duration_s": 7200,
                                                            "deadline_s": 3600,
                                                            "deadline_source": "user"}]}]})
        answer = runtime.store.get("deadline-final")["final_answer"]
        assert "最短需 2.00 小时" in answer
        assert "用户给定期限为 1.00 小时" in answer
        assert "延长到达期限" in answer
    finally:
        runtime.close()


def test_tspeed_search_unresolved_ends_as_solver_unresolved(tmp_path):
    runtime = AgentRuntime(tmp_path, ScriptedModel())
    try:
        runtime.store.create("case-search-unresolved", "local", {"status": "running"})
        state = {
            "task_id": "case-search-unresolved",
            "scenario_id": "route_success",
            "outcomes": {"Tdata": "ok", "Tseg": "ok", "Tspeed": "search_unresolved"},
            "steps": 0, "invalid_actions": 0, "task_type": "planning",
            "request": {"origin": "漯河船闸", "destination": "漯河港"},
            "observations": [],
        }
        decision = runtime._reconcile(state)
        assert decision == {"route": "finish", "outcome_code": "solver_unresolved"}
        assert runtime.store.events("case-search-unresolved")[-1]["source_outcome"] == "search_unresolved"
    finally:
        runtime.close()


def test_artifacts_scope_hash_and_revision(tmp_path):
    store=Store(tmp_path)
    store.create("a","alice",{});store.create("b","bob",{})
    tools=ProfessionalTools(store)
    state=dict(task_id="a",owner="alice",scenario_id="route_success",revision=1,request={},refs={})
    ref=tools.data(state,"call")["artifact_ref"];state["refs"]["Tdata"]=ref
    with pytest.raises(LookupError):
        query_results(store,state,{"operation":"list","task_id":"b"})
    with pytest.raises(LookupError):
        store.read("b",ref)
    with pytest.raises(ValueError,match="version"):
        tools.payload({**state,"revision":2},"Tdata")
    (store.blobs/(ref+".json")).write_text("{}",encoding="utf-8")
    with pytest.raises(ValueError,match="hash"):
        tools.payload(state,"Tdata")
    store.close()


def test_query_missing_field_reports_available_fields(tmp_path):
    store=Store(tmp_path);store.create("task","local",{})
    try:
        ref=store.put("task","Tdata",{"snapshot_id":"sample","batteries":{}})
        result=query_results(store,{"task_id":"task","owner":"local"},
            {"operation":"read","artifact_ref":ref,"field_path":"trajectory"})
        assert result["outcome_code"]=="not_found"
        assert result["diagnostics"][0]["code"]=="field_not_found"
        assert result["available_fields"]==["batteries","snapshot_id"]
    finally:
        store.close()


def test_calculations_refs_and_units(tmp_path):
    store=Store(tmp_path);store.create("a","alice",{})
    state={"task_id":"a","owner":"alice"}
    ref=store.put("a","test",{"speed":10})
    result=calculate(store,state,{"operation":"convert","a":{"artifact_ref":ref,"field_path":"speed"},"from_unit":"节","to_unit":"km/h"})
    assert result["value"]==pytest.approx(18.52)
    assert result["sources"][0]["artifact_ref"]==ref
    with pytest.raises(Exception):
        calculate(store,state,{"operation":"convert","a":1,"from_unit":"kWh","to_unit":"km"})
    result=calculate(store,state,{"operation":"time_difference","start":"2026-10-16T23:30:00","end":"2026-10-17T00:30:00"})
    assert result["value"]==3600 and result["timezone"]=="Asia/Shanghai"
    store.close()


def test_numeric_inputs_cannot_invent_conversion_constants(tmp_path):
    from research.agent.auxiliary import user_numbers
    assert {10,12.5,-3}.issubset(user_numbers("速度十节，数值12.5，负三"))
    runtime=AgentRuntime(tmp_path,ScriptedModel())
    try:
        runtime.store.create("guard","local",{})
        message={"role":"user","content":"把十节转换成公里每小时"}
        action={"tool_calls":[{"id":"bad","type":"function","function":{"name":"calculate",
            "arguments":json.dumps({"revision":1,"rationale":"conversion","operation":"multiply","a":10,"b":1.852})}}]}
        result=runtime._guard({"task_id":"guard","allowed":["calculate"],"action":action,
            "revision":1,"messages":[message],"invalid_actions":0,"missing":[]})
        assert result["route"]=="loop"
        assert "literal_not_in_user_input" in result["messages"][-1]["content"]
    finally:
        runtime.close()


def test_docs_and_answer_validation():
    result=search("planner_approved LSTM 短时预测")
    assert result["outcome_code"]=="ok"
    assert 0<len(result["results"])<=5
    assert all(len(r["content"])<=800 and len(r["content_hash"])==64 for r in result["results"])
    assert search("zzqxvnonexistentword")["outcome_code"]=="not_found"
    assert render_answer({"answer_markdown":"历时 {{duration_s}}。","claims":[{"key":"duration_s","unit":"s"}]},
        {"duration_s":{"value":20,"unit":"s"}})=="历时 20 s。"
    with pytest.raises(ValueError):
        render_answer({"answer_markdown":"能耗 99 kWh。","claims":[]},{})
    assert render_answer({"answer_markdown":"用户输入 SOC85%，限时6小时。距离 {{distance_km}}。",
        "claims":[{"key":"distance_km","unit":"km"}]},
        {"distance_km":{"value":25.5,"unit":"km"}},
        "SOC85%，6小时内到达")=="用户输入 SOC85%，限时6小时。距离 25.5 km。"
    with pytest.raises(ValueError):
        render_answer({"answer_markdown":"预测能耗 99 kWh。","claims":[]},{},"SOC85%，6小时内到达")


def test_api_unavailable_does_not_fake_completion(tmp_path):
    runtime=AgentRuntime(tmp_path,None)
    try:
        result=runtime.start("规划",background=False)
        assert result["status"]=="failed" and result["outcome_code"]=="model_unavailable"
    finally:
        runtime.close()


def test_percent_soc_and_half_load_run_with_explicit_simulation_scope(tmp_path):
    class PercentModel(ScriptedModel):
        def complete_chat(self,messages,*,tools,**kwargs):
            if [x["function"]["name"] for x in tools]==["submit_request"]:
                args={"task_type":"planning","origin":"平顶山港","destination":"军李船闸",
                    "initial_soc_1":85,"remaining_time_s":21600,"load_state":"半载",
                    "departure_at":"2026-10-17 09:00"}
                return {"message":{"role":"assistant","content":"","tool_calls":[{
                    "id":str(uuid4()),"type":"function",
                    "function":{"name":"submit_request","arguments":json.dumps(args,ensure_ascii=False)}}]},
                    "response_model":"scripted-test","request_id":str(uuid4()),"latency_ms":0}
            return super().complete_chat(messages,tools=tools,**kwargs)
    runtime=AgentRuntime(tmp_path,PercentModel())
    try:
        result=runtime.start("从平顶山港到军李船闸，2026-10-17 09:00出发，SOC85%，半载，6小时内到达",
            task_type="planning",background=False)
        assert result["request"]["initial_soc_1"]==.85
        assert result["request"]["initial_soc_2"]==.85
        assert result["request"]["soc_applied_to_both"]
        assert result["outcome_code"]=="research_feasible"
        assert result["metrics"]["distance_km"]["value"]==pytest.approx(25.5)
        assert all(result["outcomes"][name]=="ok" for name in ("Tdata","Tseg","Tspeed","Tmanagement"))
        snapshot=runtime.store.read(result["task_id"],result["result_refs"]["Tdata"])["payload"]
        assert snapshot["load_state"]=="半载"
        assert any("不按载况区分" in note for note in snapshot["assumptions"])
    finally:
        runtime.close()


def test_invalid_percent_is_field_diagnostic(tmp_path):
    store=Store(tmp_path);store.create("task","local",{})
    try:
        result=ProfessionalTools(store).data({"task_id":"task","scenario_id":"route_success",
            "revision":1,"request":{"initial_soc_1":140}},"call")
        assert result["outcome_code"]=="invalid_input"
        assert result["diagnostics"][0]["field_ref"]=="initial_soc_1"
    finally:
        store.close()


@pytest.mark.parametrize("scenario,expected",[("lstm_short","ok"),("lstm_domain","model_out_of_domain")])
def test_actual_lstm_prediction_and_handoff(tmp_path,scenario,expected):
    store=Store(tmp_path);store.create("test","local",{})
    professional=ProfessionalTools(store)
    state=dict(task_id="test",owner="local",scenario_id=scenario,revision=1,request={},refs={})
    state["refs"]["Tdata"]=professional.data(state,"data")["artifact_ref"]
    result=professional.energy(state,"energy")
    assert result["outcome_code"]==expected
    if expected=="ok":
        assert result["summary"]["tspeed_handoff_verified"]
    store.close()


def test_native_schema_hard_constraint_injection_is_rejected(tmp_path):
    runtime=AgentRuntime(tmp_path, ScriptedModel())
    try:
        runtime.store.create("guard","local",{})
        action={"tool_calls":[{"id":"bad","type":"function","function":{"name":"Tdata",
            "arguments":json.dumps({"revision":1,"rationale":"change power","max_power_kw":9999})}}]}
        result=runtime._guard({"task_id":"guard","allowed":["Tdata"],"action":action,
            "revision":1,"messages":[],"invalid_actions":0,"missing":[]})
        assert result["route"]=="loop" and result["invalid_actions"]==1
    finally:
        runtime.close()


def test_web_research_owner_event_and_artifact_api(tmp_path):
    from tests.test_web_frontend import AuthenticatedClient, _store
    from ui.web_server import create_app
    app=create_app(llm_client=ScriptedModel(),checkpoint_backend="memory",
        research_directory=tmp_path/"research",auth_store=_store(tmp_path/"auth.sqlite3"))
    with AuthenticatedClient(app) as client:
        assert client.get("/api/research/config").json()["model_configured"]
        response=client.post("/api/run",json={"workflow_mode":"research_agent","task_text":"到漯河港"})
        assert response.status_code==202
        task=response.json()["task_id"]
        app.state.research_runtime.futures[task].result(timeout=20)
        result=client.get("/api/runs/"+task).json()
        assert result["outcome_code"]=="research_feasible"
        events=client.get("/api/runs/"+task+"/events").json()["events"]
        assert events and client.get("/api/runs/"+task+"/events?after_seq="+str(events[-1]["seq"])).json()["events"]==[]
        ref=result["result_refs"]["Tmanagement"]
        assert client.get(f"/api/runs/{task}/artifacts/{ref}").status_code==200
        app.state.research_runtime.store.create("foreign","another-user",{})
        assert client.get("/api/runs/foreign/events").status_code==404
        assert client.get(f"/api/runs/foreign/artifacts/{ref}").status_code==404
        path=app.state.research_runtime.store.blobs/(ref+".json")
        path.write_text("{}",encoding="utf-8")
        assert client.get(f"/api/runs/{task}/artifacts/{ref}").status_code==409
