"""Reproducible real API acceptance. No fake model is available in this script."""
import argparse
import json
from pathlib import Path
from core.llm_layer import LLMClient, LLMConfig
from research.agent.runtime import AgentRuntime
from research.agent.store import now


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file",type=Path,required=True)
    parser.add_argument("--storage",type=Path,default=Path("var/agent-acceptance-final"))
    args=parser.parse_args()
    client=LLMClient(LLMConfig.from_env(env_file=args.env_file))
    runtime=AgentRuntime(args.storage,client)
    report={"started_at":now(),"requested_model":client.config.model,"cases":[]}
    def record(case,result,expected):
        events=[];after=0
        while True:
            batch=runtime.store.events(result["task_id"],after)
            events.extend(batch)
            if len(batch)<200:break
            after=batch[-1]["seq"]
        item={"case":case,"task_id":result["task_id"],"outcome_code":result.get("outcome_code"),
            "status":result["status"],"answer_status":result.get("answer_status"),
            "models":sorted({e["model"] for e in events if e["type"]=="model_completed"}),
            "actions":[e["tool"] for e in events if e["type"]=="action_requested"],
            "tool_outcomes":[[e["tool"],e.get("outcome_code")] for e in events if e["type"] in ("tool_completed","tool_failed") and not e.get("parent_call_id")],
            "result_refs":result.get("result_refs",{}),"answer_ref":result.get("answer_ref"),
            "metrics":result.get("metrics",{}),
            "passed":result.get("outcome_code") in expected and result.get("answer_status")=="accepted"}
        report["cases"].append(item)
        (args.storage/"acceptance.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps({k:item[k] for k in ("case","task_id","outcome_code","answer_status","passed")},ensure_ascii=False),flush=True)
        return result
    try:
        cases=[
            ("normal","route_success","planning","从漯河船闸到漯河港，按当前仿真情景规划节能航行。",["research_feasible"]),
            ("multi_wait","multi_wait","planning","从仿真起点到仿真终点，包含情景指定的等待，求节能航行方案。",["research_feasible"]),
            ("fdp_reject","fdp_reject","planning","从漯河船闸到漯河港。请处理 FDP 失败反馈；有可核验功率上限时尝试有界重搜，不能改电池边界。",["infeasible_under_assumptions","search_unresolved","search_exhausted","no_feasible_candidate"]),
            ("missing_conversion","missing_conversion","planning","从漯河船闸到漯河港，规划节能航行。",["needs_input"]),
            ("lstm_short","lstm_short","prediction","使用真实 LSTM 验证短轨迹预测及 Tspeed 评价交接。",["prediction_completed"]),
            ("lstm_domain","lstm_domain","prediction","使用选定的真实 LSTM 轨迹进行预测，不替换后端。",["model_out_of_domain"]),
            ("docs","route_success","question","请检索项目资料，解释 LSTM 是否获规划批准及预测窗口限制，说明资料来源。",["question_answered"]),
            ("calculate","route_success","question","请调用单位计算工具，把用户输入的十节转换成公里每小时。",["question_answered"]),
        ]
        results={}
        # One runtime owns one database. Its two workers run independent tasks.
        for i in range(0,len(cases),2):
            pending=[]
            for case,scenario,kind,text,expected in cases[i:i+2]:
                task=runtime.start(text,scenario_id=scenario,task_type=kind)
                pending.append((case,task["task_id"],expected))
            for case,task,expected in pending:
                runtime.futures[task].result()
                results[case]=record(case,runtime.store.get(task),expected)
        a,b=results["normal"],results["multi_wait"]
        text=("请调用结果工具比较两次方案的历时、母线能耗和双电池末端 SOC，明确输入差异，不给优化提升率。"
              f"之前 task_id={a['task_id']} artifact_ref={a['result_refs']['Tmanagement']}；"
              f"之后 task_id={b['task_id']} artifact_ref={b['result_refs']['Tmanagement']}。")
        record("compare",runtime.start(text,task_type="question",background=False),["question_answered"])
        waiting=runtime.start("请从漯河船闸出发规划节能航行，目的地待我补充。",task_type="planning",background=False)
        assert waiting["status"]=="waiting_human",waiting
        runtime.close()
        runtime=AgentRuntime(args.storage,client)
        pending=waiting["pending"]
        resumed=runtime.resume(waiting["task_id"],"local",pending_id=pending["pending_id"],
            revision=pending["revision"],response_id="acceptance-resume",reply_text="目的地是漯河港。",background=False)
        record("clarification_restart",resumed,["research_feasible"])
        report["finished_at"]=now()
        report["passed"]=all(x["passed"] for x in report["cases"])
        (args.storage/"acceptance.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        if not report["passed"]:raise SystemExit(1)
    finally:
        runtime.close()


if __name__=="__main__":
    main()
