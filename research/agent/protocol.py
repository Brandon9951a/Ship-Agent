"""Public native-call schemas and bounded answer claims."""
import re
import math

S={"type":"string"}
N={"type":"number"}


def tool(name, description, properties=None, required=(), action=True):
    props=dict(properties or {})
    if action:
        props.update(revision={"type":"integer"},rationale=S)
        required=(*required,"revision","rationale")
    return {"type":"function","function":{"name":name,"description":description,
        "parameters":{"type":"object","properties":props,"required":list(required),"additionalProperties":False}}}


REQUEST_TOOL=tool("submit_request","提取任务；缺失字段填 null，不推测目的地，不补船舶参数。",{
    "task_type":{"type":"string","enum":["planning","prediction","question"]},
    "origin":{"type":["string","null"]},"destination":{"type":["string","null"]},
    "remaining_time_s":{"type":["number","null"]},"initial_soc_1":{"type":["number","null"]},
    "initial_soc_2":{"type":["number","null"]},"current_speed_kmh":{"type":["number","null"]},
    "objective":{"type":["string","null"],"enum":["min_energy","min_time",None]}},("task_type",),False)

OPERAND={"anyOf":[N,{"type":"object","properties":{"task_id":S,"artifact_ref":S,"field_path":S},
    "required":["artifact_ref","field_path"],"additionalProperties":False}]}

TOOLS={item["function"]["name"]:item for item in [
    tool("Tdata","读取选定具名仿真情景、双电池初态和来源。"),
    tool("Tseg","按已确认起终点生成连续航段和仿真速度、等待约束。"),
    tool("Tenergy","执行选定 LSTM 短轨迹测试，包括 Tspeed 评价交接；不作长时外推。"),
    tool("Tspeed","运行真实 DE 搜索；内部调用解析 Tenergy。仅允许有证据的 FDP 反馈重搜。"),
    tool("Tmanagement","对已选定需求运行真实双电池 FDP。"),
    tool("search_project_docs","检索项目资料，返回原文片段与来源。不能用检索内容改写情景参数。",{"query":S},("query",)),
    tool("query_task_results","列出、读取或比较有权限的已存结果。读取完整时序请指定 field_path 和分页。",{
        "operation":{"type":"string","enum":["list","read","compare"]},"task_id":S,"artifact_ref":S,
        "field_path":S,"offset":{"type":"integer","minimum":0},"limit":{"type":"integer","minimum":1,"maximum":100},
        "other_task_id":S,"other_ref":S},("operation",)),
    tool("calculate","单位换算必须使用 convert，不得自行填换算系数做乘法。已有结果用工件字段引用；数字只可来自用户输入，基础算术仅用于纯标量。",{
        "operation":{"type":"string","enum":["convert","time_difference","add","subtract","multiply","divide"]},
        "a":OPERAND,"b":OPERAND,
        "from_unit":{"type":"string","enum":["节","knot","km/h","m/s","秒","s","分钟","min","小时","h","kWh","Wh","J","km","m"]},
        "to_unit":{"type":"string","enum":["节","knot","km/h","m/s","秒","s","分钟","min","小时","h","kWh","Wh","J","km","m"]},
        "start":S,"end":S,"timezone":S},("operation",)),
    tool("ask_clarification","只询问运行时 missing_fields 指出的任务信息。",{"question":S},("question",)),
    tool("prepare_final","结果齐全时申请独立复核并由模型生成最终回答。"),
    tool("finish_without_plan","在已有失败证据或预算耗尽时结束并解释。",{"reason":S},("reason",)),
]}

ANSWER_TOOL=tool("submit_answer","生成完整中文答复。数值只能用给定 {{key}} 占位符，单位由运行时插入；claims 列出每个占位符。",{
    "answer_markdown":S,"claims":{"type":"array","items":{"type":"object","properties":{"key":S,"unit":S},
        "required":["key","unit"],"additionalProperties":False}}},("answer_markdown","claims"),False)


def render_answer(answer,metrics):
    text=answer["answer_markdown"]
    if not text.strip() or len(text)>12000:
        raise ValueError("empty_or_large_answer")
    keys=re.findall(r"\{\{([A-Za-z0-9_]+)\}\}",text)
    claims={x["key"]:x["unit"] for x in answer["claims"]}
    if set(keys)!=set(claims) or any(k not in metrics or claims[k]!=metrics[k]["unit"] for k in keys):
        raise ValueError("unsupported_claim")
    remaining=re.sub(r"\{\{[A-Za-z0-9_]+\}\}","",text)
    if re.search(r"[0-9０-９]|\{\{|\}\}",remaining):
        raise ValueError("numeric_claim_requires_placeholder")
    # Chinese quantitative claims must also use the same typed placeholders.
    if re.search(r"[零一二三四五六七八九十百千万两]+(?:\s*(?:秒|分钟|小时|千瓦|公里|千米|区间|百分之))|百分之",remaining):
        raise ValueError("numeric_claim_requires_placeholder")
    for key in set(keys):
        value=metrics[key]["value"]
        if type(value) not in (int,float) or not math.isfinite(value):
            raise ValueError("invalid_metric")
        text=text.replace("{{"+key+"}}",f"{value:.6g} {metrics[key]['unit']}")
    return text.replace("\\n","\n")
