"""Standalone prediction runs actual analytic/LSTM tools; only chat is scripted."""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from research.agent.professional import ProfessionalTools
from research.agent.prediction import verify_prediction
from research.agent.runtime import AgentRuntime
from research.agent.store import Store


def setup_tool(tmp_path, request, scenario="route_success"):
    store=Store(tmp_path); store.create("task","local",{})
    tool=ProfessionalTools(store)
    state=dict(task_id="task",scenario_id=scenario,task_type="prediction",revision=1,request=request,refs={})
    state["refs"]["Tdata"]=tool.data(state,"data")["artifact_ref"]
    return store,tool,state


def energy(tool,state):
    result=tool.energy(state,"energy")
    state["refs"]["Tenergy"]=result["artifact_ref"]
    return tool.payload(state,"Tenergy")


def test_default_route_prediction_recomputes_power_distance_and_bus_integrals(tmp_path):
    store,tool,state=setup_tool(tmp_path,{"origin":"漯河船闸","destination":"漯河港"})
    try:
        body=energy(tool,state)
        assert body["outcome_code"]=="ok" and body["backend"]=="analytic"
        summary=verify_prediction(tool,state)
        points=store.read("task",body["power_series_ref"])["payload"]["power_points"]
        coefficient=tool.payload(state,"Tdata")["config"]["coefficient"]
        assert len(points)>1
        for p in points:
            assert p["predicted_motor_power_kw"]==pytest.approx(coefficient*p["planned_speed_kmh"]**3)
            assert p["motor_energy_kwh"]==pytest.approx(p["predicted_motor_power_kw"]*p["duration_s"]/3600)
        assert sum(p["planned_speed_kmh"]*p["duration_s"]/3600 for p in points)==pytest.approx(summary["distance_km"])
        assert summary["bus_energy_kwh"]==pytest.approx(summary["motor_energy_kwh"]/.94+5*summary["duration_s"]/3600)
        assert body["units"]==dict(power="kW",energy="kWh",time="s",speed="km/h")
        assert body["time_range_s"]==[0,summary["duration_s"]]
    finally: store.close()


@pytest.mark.parametrize("field",["power_series_ref","demand_ref","environment_ref","segment_ref"])
def test_prediction_verification_rejects_child_artifact_tampering(tmp_path,field):
    store,tool,state=setup_tool(tmp_path,{"origin":"漯河船闸","destination":"漯河港"})
    try:
        body=energy(tool,state)
        (store.blobs/(body[field]+".json")).write_text("{}",encoding="utf-8")
        with pytest.raises(ValueError,match="hash_mismatch"): verify_prediction(tool,state)
    finally: store.close()


def test_explicit_trajectory_waiting_and_unknown_bus(tmp_path):
    req={"trajectory":[dict(segment_id="a",phase="sailing",start_offset_s=0,duration_s=10,planned_speed_kmh=10),
                       dict(segment_id="a",phase="waiting",start_offset_s=10,duration_s=5,planned_speed_kmh=0)]}
    store,tool,state=setup_tool(tmp_path,req,"missing_conversion")
    try:
        body=energy(tool,state)
        assert body["outcome_code"]=="ok"
        coefficient=tool.payload(state,"Tdata")["config"]["coefficient"]
        assert verify_prediction(tool,state)["motor_energy_kwh"]==pytest.approx(coefficient*10**3*10/3600)
        assert body["summary"]["bus_energy_kwh"] is None
    finally: store.close()


def test_duration_and_user_speed_are_actual_prediction_inputs(tmp_path):
    store,tool,state=setup_tool(tmp_path,{"current_speed_kmh":10,"prediction_duration_s":20})
    try:
        body=energy(tool,state)
        coefficient=tool.payload(state,"Tdata")["config"]["coefficient"]
        assert verify_prediction(tool,state)["motor_energy_kwh"]==pytest.approx(coefficient*10**3*20/3600)
        assert body["summary"]["duration_s"]==20
        assert body["sources"]["trajectory"]=="user"
    finally: store.close()


@pytest.mark.parametrize("change,code",[({"start_offset_s":5},"invalid_input"),
    ({"duration_s":-1},"invalid_input"),({"planned_speed_kmh":None},"needs_input"),
    ({"planned_speed_kmh":100},"invalid_input")])
def test_invalid_or_missing_trajectory_never_produces_energy(tmp_path,change,code):
    point=dict(segment_id="a",phase="sailing",start_offset_s=0,duration_s=5,planned_speed_kmh=10)
    store,tool,state=setup_tool(tmp_path,{"trajectory":[{**point,**change}]})
    try:
        body=energy(tool,state)
        assert body["outcome_code"]==code and body["diagnostics"][0]["field_ref"]
        assert "summary" not in body
    finally: store.close()


@pytest.mark.parametrize("duration,expected",[(20,"ok"),(40,"model_out_of_domain")])
def test_explicit_lstm_from_default_scenario_keeps_backend_and_window(tmp_path,duration,expected):
    store,tool,state=setup_tool(tmp_path,{"prediction_backend":"lstm","prediction_duration_s":duration})
    try:
        body=energy(tool,state)
        assert body["backend"]=="lstm" and body["outcome_code"]==expected
        if expected=="ok":
            assert verify_prediction(tool,state)["motor_energy_kwh"]>0
            assert body["summary"]["tspeed_handoff_verified"]
    finally: store.close()


def test_explicit_lstm_trajectory_and_history_integrity(tmp_path):
    store,tool,state=setup_tool(tmp_path,{"prediction_backend":"lstm","trajectory":[
        dict(segment_id="custom",phase="sailing",start_offset_s=i*5,duration_s=5,planned_speed_kmh=10)
        for i in range(4)]})
    try:
        body=energy(tool,state)
        assert verify_prediction(tool,state)["motor_energy_kwh"]>0
        assert body["sources"]["trajectory"]=="user"
        history_ref=body["request"]["history_ref"]
        path=store.directory/"energy"/"task"/(history_ref["sha256"]+".json")
        path.write_text("{}",encoding="utf-8")
        with pytest.raises(ValueError,match="hash mismatch"): verify_prediction(tool,state)
    finally: store.close()


class PredictionModel:
    config=SimpleNamespace(enabled=True,model="scripted-test")
    def __init__(self,missing=False,skip=False): self.parses=0; self.missing=missing; self.skip=skip
    def complete_chat(self,messages,*,tools,**kwargs):
        names=[t["function"]["name"] for t in tools]
        if names==["submit_request"]:
            self.parses+=1
            name="submit_request"; args=dict(task_type="prediction",origin="漯河船闸",
                destination=None if self.missing and self.parses==1 else "漯河港")
        elif names==["submit_answer"]:
            name="submit_answer";args=dict(answer_markdown="预测来自工具，仅为研究仿真。",claims=[])
        else:
            ctx=json.loads(messages[-1]["content"].split("：",1)[1])
            name="prepare_final" if self.skip else next(n for n in ("Tdata","ask_clarification","Tenergy","prepare_final","finish_without_plan") if n in names)
            args=dict(revision=ctx["revision"],rationale="test")
            if name=="ask_clarification":args["question"]="请补充预测航段目的地。"
            if name=="finish_without_plan":args["reason"]="诊断终止"
        return {"message":{"role":"assistant","tool_calls":[{"id":str(uuid4()),"type":"function",
            "function":{"name":name,"arguments":json.dumps(args)}}]},"response_model":"scripted-test"}


def test_prediction_clarification_resumes_same_task_and_cannot_skip_tool(tmp_path):
    runtime=AgentRuntime(tmp_path,PredictionModel(missing=True))
    try:
        waiting=runtime.start("预测该航段能耗",background=False)
        assert waiting["status"]=="waiting_human" and waiting["missing_fields"]==["destination"]
        p=waiting["pending"]
        done=runtime.resume(waiting["task_id"],"local",pending_id=p["pending_id"],revision=p["revision"],
            response_id="resume",reply_text="到漯河港",background=False)
        assert done["task_id"]==waiting["task_id"] and done["revision"]==2
        assert done["outcome_code"]=="prediction_completed"
        assert set(done["result_refs"])=={"Tdata","Tenergy"}
        assert done["metrics"]["motor_energy_kwh"]["source_ref"]==done["result_refs"]["Tenergy"]
    finally: runtime.close()


def test_model_cannot_skip_tenergy(tmp_path):
    runtime=AgentRuntime(tmp_path,PredictionModel(skip=True))
    try:
        result=runtime.start("预测能耗",background=False)
        assert result["outcome_code"]=="agent_budget_exhausted"
        assert not result.get("metrics")
        assert not any(e["type"]=="tool_started" for e in runtime.store.events(result["task_id"]))
    finally: runtime.close()


def test_completed_prediction_answer_is_not_overwritten_by_replayed_finish(tmp_path):
    runtime=AgentRuntime(tmp_path,PredictionModel())
    try:
        done=runtime.start("预测能耗",background=False)
        state=dict(runtime.graph.get_state(runtime._config(done["task_id"])).values)
        ref=state["refs"]["Tenergy"]
        (runtime.store.blobs/(ref+".json")).write_text("{}",encoding="utf-8")
        state.update(outcome_code="verification_failed",metrics={})
        runtime._finish(state)
        result=runtime.store.get(done["task_id"])
        assert result["outcome_code"]=="prediction_completed"
        assert result["answer_ref"]==done["answer_ref"]
        assert runtime.store.events(done["task_id"])[-1]["type"]=="answer_reused"
    finally: runtime.close()


def test_original_website_auto_request_executes_standalone_tenergy(tmp_path):
    from tests.test_web_frontend import AuthenticatedClient, _store
    from ui.web_server import create_app
    app=create_app(llm_client=PredictionModel(),checkpoint_backend="memory",
        research_directory=tmp_path/"research",auth_store=_store(tmp_path/"auth.sqlite3"))
    with AuthenticatedClient(app) as client:
        response=client.post("/api/run",json={"task_text":"从漯河船闸到漯河港，预测该航段能耗",
            "workflow_mode":"research_agent","scenario_id":"route_success","task_type":"auto"})
        assert response.status_code==202
        task=response.json()["task_id"]
        app.state.research_runtime.futures[task].result(timeout=30)
        result=client.get(f"/api/runs/{task}").json()
        assert result["outcome_code"]=="prediction_completed"
        events=client.get(f"/api/runs/{task}/events").json()["events"]
        assert [e["tool"] for e in events if e["type"]=="tool_started"]==["Tdata","Tenergy"]
        completed=next(e for e in events if e["type"]=="tool_completed" and e["tool"]=="Tenergy")
        assert completed["backend"]=="analytic"
        payload=client.get(f"/api/runs/{task}/artifacts/{result['result_refs']['Tenergy']}").json()["payload"]
        power=client.get(f"/api/runs/{task}/artifacts/{payload['power_series_ref']}").json()["payload"]
        assert sum(p["motor_energy_kwh"] for p in power["power_points"])==pytest.approx(result["metrics"]["motor_energy_kwh"]["value"])
