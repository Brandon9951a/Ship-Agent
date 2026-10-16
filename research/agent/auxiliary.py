"""Read-only evidence queries and typed arithmetic; no arbitrary code or SQL."""
from __future__ import annotations

import math
import re
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from research.agent.professional import ROOT, configuration
from research.agent.store import digest

DOC_LOCK = threading.Lock()
INDEX = None
STOP = set("的 了 和 是 在 我 我们 你 为什么 请 一下 帮 查询 这个 什么 如何 能否 吗 多少".split())


def user_numbers(text):
    """Conservative literal extraction, including common Chinese integer/decimal input."""
    values={float(v) for v in re.findall(r"(?<![\d.])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?",text)}
    digits={c:i for i,c in enumerate("零一二三四五六七八九")}
    digits["两"]=2
    def integer(token):
        if not any(c in "十百千万亿" for c in token):
            return int("".join(str(digits[c]) for c in token))
        total=section=number=0
        for c in token:
            if c in digits:number=digits[c]
            else:
                unit={"十":10,"百":100,"千":1000,"万":10000,"亿":100000000}[c]
                if unit<10000:
                    section+=(number or 1)*unit
                else:
                    total+=(section+number)*unit;section=0
                number=0
        return total+section+number
    for token in re.findall(r"[负正]?[零一二三四五六七八九十百千万亿两]+(?:点[零一二三四五六七八九]+)?",text):
        sign=-1 if token.startswith("负") else 1
        token=token.lstrip("负正")
        parts=token.split("点")
        value=integer(parts[0])
        if len(parts)==2:
            value+=float("0."+"".join(str(digits[c]) for c in parts[1]))
        values.add(sign*value)
    return values


def field(value, path):
    if not isinstance(path, str) or len(path) > 200:
        raise ValueError("invalid_field_path")
    for part in path.split("."):
        if isinstance(value, list) and part.isdigit():
            index = int(part)
            if index >= len(value):
                raise ValueError("field_not_found")
            value = value[index]
        elif isinstance(value, dict):
            if part not in value:
                raise ValueError("field_not_found")
            value = value[part]
        else:
            raise ValueError("field_not_found")
    return value


def search(query):
    import jieba
    from rank_bm25 import BM25Okapi
    global INDEX
    if not isinstance(query, str) or not 1 <= len(query) <= 1000:
        raise ValueError("invalid_query")
    jieba.setLogLevel(40)
    def tokens(text):
        return [t.lower() for t in jieba.lcut(text) if t.strip() and t not in STOP and re.search(r"[\w\u4e00-\u9fff]",t)]
    with DOC_LOCK:
        docs=[]
        for name in configuration("route_success")["docs"]:
            text=(ROOT/name).read_text(encoding="utf-8")
            docs.append((name,text,digest(text)))
        fingerprint=digest([(name,sha) for name,_,sha in docs])
        if INDEX is None or INDEX[0]!=fingerprint:
            chunks=[]
            for name,text,sha in docs:
                section="正文"
                for line in text.splitlines():
                    if line.startswith("#"):
                        section=line.lstrip("# ")
                    if not line.strip():
                        continue
                    for start in range(0,len(line),700):
                        chunks.append({"source":name,"section":section,"content":line[start:start+800],
                            "content_hash":sha,"status":"project_document; check date/scope; not adopted parameters"})
            INDEX=(fingerprint,chunks,BM25Okapi([tokens(x["content"]) for x in chunks]))
        scores=INDEX[2].get_scores(tokens(query))
        hits=[{**INDEX[1][i],"score":float(scores[i])} for i in sorted(range(len(scores)),key=lambda i:scores[i],reverse=True)[:5]
              if scores[i]>0]
    return {"outcome_code":"ok" if hits else "not_found","results":hits,"index_version":fingerprint}


def query_results(store, state, args):
    task=args.get("task_id") or state["task_id"]
    # An ID supplied by the model never grants access to another user's task.
    store.get(task,state["owner"])
    operation=args["operation"]
    if operation=="list":
        return {"outcome_code":"ok","task_id":task,"results":store.list(task)}
    if operation=="read":
        doc=store.read(task,args["artifact_ref"])
        try:
            value=field(doc["payload"],args["field_path"]) if args.get("field_path") else doc["payload"].get("summary",{})
        except ValueError as exc:
            if str(exc)!="field_not_found":
                raise
            return {"outcome_code":"not_found","artifact_ref":args["artifact_ref"],
                    "field_path":args.get("field_path"),"available_fields":list(doc["payload"])[:30],
                    "diagnostics":[{"code":"field_not_found","detail":"该工件没有请求字段，请从现有字段选择；预测轨迹由 Tenergy 工具读取。"}]}
        if isinstance(value,list):
            offset=args.get("offset",0);limit=args.get("limit",20)
            if type(offset)!=int or type(limit)!=int or offset<0 or not 1<=limit<=100:
                raise ValueError("invalid_page")
            return {"outcome_code":"ok","artifact_ref":args["artifact_ref"],"total":len(value),"value":value[offset:offset+limit]}
        return {"outcome_code":"ok","artifact_ref":args["artifact_ref"],"value":value}
    if operation=="compare":
        other=args.get("other_task_id") or task
        store.get(other,state["owner"])
        a=store.read(task,args["artifact_ref"])["payload"]
        b=store.read(other,args["other_ref"])["payload"]
        sa,sb=a.get("summary",{}),b.get("summary",{})
        delta={}
        for key in ("duration_s","bus_energy_kwh","final_soc_1","final_soc_2"):
            if type(sa.get(key)) in (int,float) and type(sb.get(key)) in (int,float):
                delta[key]=sb[key]-sa[key]
        context_changes={k:{"before":a.get(k),"after":b.get(k)}
            for k in ("request_revision","scenario_id","snapshot_id","request") if a.get(k)!=b.get(k)}
        return {"outcome_code":"ok","before_ref":args["artifact_ref"],"after_ref":args["other_ref"],
            "difference_after_minus_before":delta,"input_changes":context_changes,
            "comparison_scope":"描述差值；不同输入不宣称优化提升率"}
    raise ValueError("unknown_query_operation")


UNITS={"节":"knot","km/h":"kilometer/hour","m/s":"meter/second","秒":"second",
       "分钟":"minute","小时":"hour","kWh":"kilowatt_hour","Wh":"watt_hour","J":"joule",
       "knot":"knot","s":"second","min":"minute","h":"hour","km":"kilometer","m":"meter"}


def calculate(store,state,args):
    def number(name):
        spec=args[name]
        if isinstance(spec,dict):
            task=spec.get("task_id") or state["task_id"]
            store.get(task,state["owner"])
            value=field(store.read(task,spec["artifact_ref"])["payload"],spec["field_path"])
            origin={"artifact_ref":spec["artifact_ref"],"field_path":spec["field_path"]}
        else:
            value=spec;origin={"source":"user_input"}
        if type(value) not in (int,float) or not math.isfinite(value):
            raise ValueError("finite_number_required")
        return value,origin
    operation=args["operation"]
    if operation=="time_difference":
        zone=ZoneInfo(args.get("timezone") or "Asia/Shanghai")
        def date(key):
            d=datetime.fromisoformat(args[key])
            return d.replace(tzinfo=zone) if d.tzinfo is None else d
        value=(date("end")-date("start")).total_seconds()
        return {"outcome_code":"ok","value":value,"unit":"s","formula":"end-start",
                "source":"user_input","timezone":str(zone)}
    a,source_a=number("a")
    if operation=="convert":
        from pint import UnitRegistry
        ureg=UnitRegistry()
        before=UNITS[args["from_unit"]];after=UNITS[args["to_unit"]]
        value=float((a*ureg(before)).to(after).magnitude)
        result={"unit":args["to_unit"],"formula":f"{args['from_unit']} -> {args['to_unit']}","sources":[source_a]}
    else:
        b,source_b=number("b")
        operations={"add":lambda:a+b,"subtract":lambda:a-b,"multiply":lambda:a*b,"divide":lambda:a/b}
        if operation not in operations:
            raise ValueError("unsupported_operation")
        value=operations[operation]()
        result={"unit":"scalar","formula":operation,"sources":[source_a,source_b]}
    if not math.isfinite(value):
        raise ValueError("nonfinite_result")
    return {"outcome_code":"ok","value":value,**result}
