"""Export real analytic/weight inference evidence without claiming live LLM acceptance."""
import argparse
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from research.agent.professional import ProfessionalTools
from research.agent.prediction import verify_prediction
from research.agent.store import Store, now


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage",type=Path,default=Path("artifacts/standalone-tool-evidence"))
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    report={"started_at":now(),"scope":"actual_tool_calls_only; live DeepSeek acceptance is a separate gate",
            "chat_model":None,"cases":[]}
    store=Store(args.storage);tools=ProfessionalTools(store)
    try:
        cases=[("analytic",{"origin":"漯河船闸","destination":"漯河港"},"ok"),
            ("lstm20",{"prediction_backend":"lstm","prediction_duration_s":20},"ok"),
            ("lstm40",{"prediction_backend":"lstm","prediction_duration_s":40},"model_out_of_domain")]
        for name,request,expected in cases:
            task=str(uuid4());store.create(task,"local",{})
            state=dict(task_id=task,task_type="prediction",scenario_id="route_success",
                       revision=1,request=request,refs={})
            calls=[]
            for tool,fn in (("Tdata",tools.data),("Tenergy",tools.energy)):
                call=str(uuid4())
                result=fn(state,call)
                state["refs"][tool]=result["artifact_ref"]
                calls.append({"tool":tool,"call_id":call,**result})
            assert result["outcome_code"]==expected
            verified=verify_prediction(tools,state) if expected=="ok" else None
            artifacts={}
            for row in store.list(task):
                ref=row["id"];store.read(task,ref)
                raw=(store.blobs/(ref+".json")).read_bytes()
                assert hashlib.sha256(raw).hexdigest()==ref
                (args.output/(ref+".json")).write_bytes(raw)
                artifacts[ref]=row["kind"]
            # Include the original Tenergy v2 history/request/power artifacts too.
            energy_dir=store.directory/"energy"/task
            if energy_dir.exists():
                for path in energy_dir.glob("*.json"):
                    raw=path.read_bytes();assert hashlib.sha256(raw).hexdigest()==path.stem
                    (args.output/path.name).write_bytes(raw)
                    artifacts[path.stem]="TenergyV2:"+json.loads(raw)["kind"]
            body=tools.payload(state,"Tenergy")
            item={"case":name,"task_id":task,"request":request,"tool_calls":calls,
                "backend":body["backend"],"outcome_code":body["outcome_code"],
                "verified_summary":verified,"artifact_refs":state["refs"],"exported_artifacts":artifacts}
            report["cases"].append(item)
            print(json.dumps({k:item[k] for k in ("case","task_id","backend","outcome_code","verified_summary")},ensure_ascii=False))
        report["finished_at"]=now();report["passed"]=True
        (args.output/"tool_calls.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    finally:store.close()


if __name__=="__main__":main()
