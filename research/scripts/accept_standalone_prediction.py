"""Real configured API acceptance through the website endpoints. No scripted LLM."""
import argparse
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from core.llm_layer import LLMClient, LLMConfig
from research.agent.prediction import verify_prediction
from research.agent.store import now
from ui.auth_store import AuthStore
from ui.web_server import create_app


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file",type=Path)
    parser.add_argument("--storage",type=Path,default=Path("artifacts/standalone-real-api"))
    args=parser.parse_args()
    config=LLMConfig.from_env(env_file=args.env_file)
    if not config.enabled:
        raise SystemExit("Configure the real model via --env-file or SHIP_LLM_* first; no fake fallback is provided.")
    if config.provider.lower() != "deepseek":
        raise SystemExit("This acceptance gate requires the configured DeepSeek provider.")
    args.storage.mkdir(parents=True,exist_ok=True)
    auth=AuthStore(backend="sqlite",sqlite_path=args.storage/"auth.sqlite3")
    auth.ensure_schema()
    username="acceptance-"+str(uuid4()); password=str(uuid4())
    auth.create_user(username,"Prediction acceptance","admin",password)
    app=create_app(llm_client=LLMClient(config),checkpoint_backend="memory",
        research_directory=args.storage/"research",auth_store=auth)
    report={"started_at":now(),"provider":config.provider,"requested_model":config.model,"cases":[]}
    runtime=None
    exports=args.storage/"export";exports.mkdir(exist_ok=True)
    def record(client,case,task,expected):
        runtime.futures[task].result(timeout=600)
        result=client.get(f"/api/runs/{task}").json()
        events=runtime.store.events(task)
        models=[]
        for event in events:
            if event["type"]=="model_completed":
                blob=runtime.store.read(task,event["artifact_ref"])
                models.append({"request_id":event["request_id"],"model":event["model"],
                    "artifact_ref":event["artifact_ref"],"response":blob["payload"]["response"]["message"]})
        state=runtime.graph.get_state(runtime._config(task)).values
        verified=verify_prediction(runtime.professional,state) if result.get("outcome_code")=="prediction_completed" else None
        artifact_refs={}
        for row in runtime.store.list(task):
            ref=row["id"]
            payload=runtime.store.read(task,ref)
            raw=(runtime.store.blobs/(ref+".json")).read_bytes()
            assert hashlib.sha256(raw).hexdigest()==ref
            (exports/(ref+".json")).write_bytes(raw)
            artifact_refs[ref]=payload["kind"]
        energy_dir=runtime.store.directory/"energy"/task
        if energy_dir.exists():
            for path in energy_dir.glob("*.json"):
                raw=path.read_bytes();assert hashlib.sha256(raw).hexdigest()==path.stem
                (exports/path.name).write_bytes(raw)
                artifact_refs[path.stem]="TenergyV2:"+json.loads(raw)["kind"]
        prediction=runtime.professional.payload(state,"Tenergy") if state.get("refs",{}).get("Tenergy") else None
        checks={"outcome":result.get("outcome_code")==expected if expected else result["status"]=="waiting_human",
                "real_model_responses":bool(models) and all(m["request_id"] and m["model"] for m in models)}
        if expected:
            checks["answer_verified"]=result.get("answer_status")=="accepted"
            checks["actual_backend"]=bool(prediction) and prediction["backend"]==("lstm" if case.startswith("lstm") else "analytic")
            calls=[e["tool"] for e in events if e["type"]=="tool_started" and not e.get("parent_call_id")]
            checks["tool_order"]=all(tool in calls for tool in ("Tdata","Tenergy")) and calls.index("Tdata")<calls.index("Tenergy")
            checks["no_voyage_planning"]=not any(tool in calls for tool in ("Tspeed","Tmanagement"))
        else:
            checks["missing_fields"]=set((result.get("pending") or {}).get("fields",[]))=={"origin","destination"}
        if case=="resumed":checks["same_task_revision"]=result.get("revision")==2
        item={"case":case,"task_id":task,"status":result["status"],
            "outcome_code":result.get("outcome_code"),"answer_status":result.get("answer_status"),
            "request":result.get("request"),"pending":result.get("pending"),
            "backend":prediction.get("backend") if prediction else None,
            "diagnostics":prediction.get("diagnostics") if prediction else None,
            "metrics":result.get("metrics"),"models":models,"result_refs":result.get("result_refs"),
            "tool_log":[e for e in events if e["type"] in ("action_requested","tool_started","tool_completed","tool_failed","waiting_human")],
            "verified_summary":verified,"exported_artifacts":artifact_refs,
            "checks":checks,"passed":all(checks.values())}
        report["cases"].append(item)
        (exports/"acceptance.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps({k:item[k] for k in ("case","task_id","outcome_code","answer_status","passed")},ensure_ascii=False),flush=True)
        assert item["passed"],item["case"]
        return result
    with TestClient(app) as client:
        runtime=app.state.research_runtime
        login=client.post("/api/auth/login",headers={"Origin":"http://testserver"},json={"username":username,"password":password})
        login.raise_for_status()
        headers={"Origin":"http://testserver","X-CSRF-Token":login.json()["csrf_token"]}
        cases=[("normal","从漯河船闸到漯河港，预测该航段能耗，不做航速优化。","prediction_completed"),
            ("lstm20","明确使用真实 LSTM 预测 20 秒短轨迹，使用项目具名短轨迹及历史样本，不替换后端。","prediction_completed"),
            ("lstm40","明确使用真实 LSTM 预测 40 秒短轨迹，使用项目具名短轨迹及历史样本，不替换后端。","model_out_of_domain"),
            ("missing","请预测航段能耗，起终点待补充。",None)]
        for case,text,expected in cases:
            submitted=client.post("/api/run",headers=headers,json={"task_text":text,
                "workflow_mode":"research_agent","scenario_id":"route_success","task_type":"auto"})
            submitted.raise_for_status()
            task=submitted.json()["task_id"]
            result=record(client,case,task,expected)
            if case=="missing":
                p=result["pending"]
                resumed=client.post("/api/resume",headers=headers,json={"workflow_mode":"research_agent",
                    "thread_id":task,"pending_id":p["pending_id"],"revision":p["revision"],
                    "response_id":str(uuid4()),"reply_text":"从漯河船闸到漯河港，用情景默认航速预测。"})
                resumed.raise_for_status()
                record(client,"resumed",task,"prediction_completed")
    report["finished_at"]=now();report["passed"]=all(c["passed"] for c in report["cases"])
    (exports/"acceptance.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")


if __name__=="__main__":main()
