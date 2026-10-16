/* Adapter from the durable research agent to the original voyage dashboard. */
const ResearchBridge = (() => {
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const value = (number, unit) => number == null ? null : { value: number, unit };
  const metric = (task, name) => task.metrics?.[name]?.value ?? null;
  const finite = number => typeof number === "number" && Number.isFinite(number);
  const shanghaiTime = date => new Intl.DateTimeFormat("sv-SE", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(date).replace(" ", "T");

  async function json(path, options) {
    const response = await apiFetch(path, options);
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
    return body;
  }

  function structuredText(request) {
    if (!request) return "";
    const parts = [`从${request.origin}到${request.destination}`];
    if (request.departure_at) parts.push(`${request.departure_at}出发`);
    if (request.soc_initial != null) parts.push(`两组电池 SOC${Number(request.soc_initial * 100).toFixed(1)}%`);
    if (request.load_state) parts.push(request.load_state);
    if (request.max_duration_h != null) parts.push(`${request.max_duration_h}小时内到达`);
    if (request.optimization_goal === "min_time") parts.push("优先最短航时");
    return parts.join("，");
  }

  async function wait(taskId, onProgress = () => {}, signal = null) {
    let task;
    let events = [];
    for (let attempt = 0; attempt < 360; attempt += 1) {
      if (signal?.aborted) {
        const error = new Error("cancelled"); error.name = "AbortError"; throw error;
      }
      task = await json(`/api/runs/${encodeURIComponent(taskId)}`, { signal });
      events = [];
      let afterSeq = 0;
      while (true) {
        const page = await json(`/api/runs/${encodeURIComponent(taskId)}/events?after_seq=${afterSeq}`, { signal });
        const batch = page.events || [];
        events.push(...batch);
        if (batch.length < 200) break;
        afterSeq = batch.at(-1).seq;
      }
      onProgress(task, events);
      if (["completed", "failed", "waiting_human"].includes(task.status)) break;
      await sleep(1000);
    }
    if (!["completed", "failed", "waiting_human"].includes(task?.status)) {
      throw new Error("任务仍在计算，可刷新页面继续查看。");
    }
    return project(task, events);
  }

  async function submit(text, onProgress, signal = null) {
    const body = await json("/api/run", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task_text: text, workflow_mode: "research_agent", scenario_id: "route_success", task_type: "auto" }), signal,
    });
    if (signal?.aborted) {
      const error = new Error("cancelled"); error.name = "AbortError"; throw error;
    }
    userStorageSet(ACTIVE_THREAD_KEY, body.thread_id);
    userStorageSet(ACTIVE_TASK_TEXT_KEY, text);
    return wait(body.thread_id, onProgress, signal);
  }

  async function resume(result, text, onProgress, signal = null) {
    const pending = result.research_pending;
    const body = await json("/api/resume", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workflow_mode: "research_agent", thread_id: result.thread_id,
        pending_id: pending.pending_id, revision: pending.revision,
        response_id: crypto.randomUUID(), reply_text: text }), signal,
    });
    return wait(body.thread_id, onProgress, signal);
  }

  async function load(taskId, onProgress) {
    return wait(taskId, onProgress);
  }

  async function artifact(taskId, reference) {
    if (!reference) return null;
    return (await json(`/api/runs/${encodeURIComponent(taskId)}/artifacts/${encodeURIComponent(reference)}`)).payload;
  }

  async function project(task, events) {
    const refs = task.result_refs || {};
    const [data, seg, speed, management] = await Promise.all([
      artifact(task.thread_id, refs.Tdata), artifact(task.thread_id, refs.Tseg),
      artifact(task.thread_id, refs.Tspeed), artifact(task.thread_id, refs.Tmanagement),
    ]);
    const request = task.request || {};
    const origin = request.origin || "";
    const destination = request.destination || "";
    const duration = metric(task, "duration_s") ?? management?.summary?.duration_s ?? null;
    const departureText = request.departure_at?.replace(" ", "T");
    const depart = departureText ? new Date(/[+-]\d\d:\d\d$|Z$/.test(departureText)
      ? departureText : `${departureText}+08:00`) : null;
    const eta = depart && !Number.isNaN(depart.getTime()) && finite(duration)
      ? shanghaiTime(new Date(depart.getTime() + duration * 1000)) : null;
    const segmentFacts = seg?.segments || [];
    const speedRows = speed?.result?.segment_summary || [];
    const batteryRows = management?.summary?.segment_summary || [];
    const rows = segmentFacts.map((item, index) => {
      const planned = speedRows.find(row => row.segment_id === item.segment_id) || speedRows[index] || {};
      const battery = batteryRows.find(row => row.segment_id === item.segment_id) || {};
      return {
        segment_id: item.segment_id,
        origin: item.origin_entity_id, destination: item.destination_entity_id,
        distance: value(item.distance_km, "km"),
        speed: value(planned.cruise_speed_kmh ?? speed?.summary?.speed_vector_kmh?.[index], "km/h"),
        duration: value(planned.total_duration_s == null ? null : planned.total_duration_s / 3600, "h"),
        propulsion_energy: value(planned.motor_energy_kwh, "kWh"),
        soc_end: value(Math.min(battery.final_soc_1 ?? 1, battery.final_soc_2 ?? 1), "fraction"),
        distance_source: item.source_refs?.[0], model_id: speed?.summary?.backend || "analytic",
      };
    });
    const ok = task.status === "completed" && task.outcome_code === "research_feasible";
    const pending = task.status === "waiting_human" ? task.pending : null;
    const executionFailure = task.status === "failed" || ["not_supported", "tool_error", "model_unavailable",
      "process_interrupted", "agent_budget_exhausted"].includes(task.outcome_code);
    const status = ok ? "ok" : pending ? "need_clarification" : executionFailure ? "failed" : "infeasible";
    const observations = events.filter(event => event.type === "tool_completed" || event.type === "tool_failed");
    const trace = observations.map(event => ({
      node: event.tool, status: event.outcome_code === "ok" || event.type === "tool_completed" ? "ok" : "failed",
    }));
    const energy = metric(task, "bus_energy_kwh") ?? management?.summary?.bus_energy_kwh;
    const soc1 = metric(task, "final_soc_1") ?? management?.summary?.final_soc_1;
    const soc2 = metric(task, "final_soc_2") ?? management?.summary?.final_soc_2;
    const assumptions = data?.assumptions || [];
    const initial1 = request.initial_soc_1 ?? data?.batteries?.battery_1?.initial_soc ?? null;
    const initial2 = request.initial_soc_2 ?? data?.batteries?.battery_2?.initial_soc ?? null;
    const socFloor1 = data?.batteries?.battery_1?.soc_min ?? null;
    const socFloor2 = data?.batteries?.battery_2?.soc_min ?? null;
    const speedLimit = segmentFacts.length ? Math.min(...segmentFacts.map(item => item.max_speed_kmh)) : null;
    const checks = [
      { name: "time", actual: duration, limit: request.remaining_time_s, unit: "s",
        passed: duration == null || request.remaining_time_s == null ? null : duration <= request.remaining_time_s },
      { name: "speed", actual: speed?.summary?.speed_vector_kmh?.length ? Math.max(...speed.summary.speed_vector_kmh) : null,
        limit: speedLimit, unit: "km/h", passed: speed?.result?.constraint_checks?.find(item => item.name === "speed")?.passed ?? null },
      { name: "soc_1", actual: soc1, limit: socFloor1, unit: "fraction",
        passed: soc1 == null || socFloor1 == null ? null : soc1 >= socFloor1 },
      { name: "soc_2", actual: soc2, limit: socFloor2, unit: "fraction",
        passed: soc2 == null || socFloor2 == null ? null : soc2 >= socFloor2 },
    ].filter(item => item.actual != null);
    const legacyJsonFallback = task.final_answer?.startsWith("模型答复未通过或不可用。以下为工具摘要：");
    const verifiedFallback = legacyJsonFallback && task.outcome_code === "research_feasible"
      ? `研究仿真方案已通过工具复核。航程 ${formatNumber(metric(task, "distance_km"), " km", 3)}，历时 ${formatNumber(duration, " s", 1)}，母线能耗 ${formatNumber(energy, " kWh", 3)}。末端电池 SOC：甲组 ${formatNumber((soc1 ?? 0) * 100, "%")}，乙组 ${formatNumber((soc2 ?? 0) * 100, "%")}。模型正文未通过数值核验，原始响应和调用依据可在下方展开查看。`
      : null;
    const finalAnswer = verifiedFallback || task.final_answer || "";
    const reason = pending
      ? `${pending.question || "请补充任务信息。"}\n请在任务描述中补充后点击“计算仿真方案”继续。`
      : finalAnswer || task.error || task.outcome_code || "尚未形成可用方案。";
    const result = {
      workflow_mode: "research_agent", thread_id: task.thread_id, status, outcome_code: task.outcome_code,
      answer_status: task.answer_status, final_answer: finalAnswer || (pending ? reason : ""), research_pending: pending,
      research_events: events, research_metrics: task.metrics || {},
      research_soc: { battery_1: soc1, battery_2: soc2 },
      request: { origin, destination, departure_at: request.departure_at,
        max_duration_h: request.remaining_time_s == null ? null : request.remaining_time_s / 3600,
        soc_initial: initial1,
        load_state: request.load_state || data?.load_state || null,
        optimization_goal: request.objective || "min_energy" },
      tdata: { payload: { route_id: "研究仿真航线" } },
      tseg: { payload: { segments: segmentFacts.map(item => ({
        origin: item.origin_entity_id, destination: item.destination_entity_id,
        distance_km: item.distance_km, segment_id: item.segment_id,
      })) } },
      trace,
      report: { explanation: finalAnswer || reason,
        explanation_mode: task.answer_status === "accepted" ? "llm_qualitative" : "tool_result",
        segments: rows, summary: {
          required_energy: value(energy, "kWh"), total_duration: value(duration == null ? null : duration / 3600, "h"),
          eta: value(eta, "datetime"), soc_final: value(soc1 == null || soc2 == null ? null : Math.min(soc1, soc2), "fraction"),
          soc_initial: value(initial1 == null || initial2 == null ? null : Math.min(initial1, initial2), "fraction"),
          propulsion_energy: value(management?.summary?.propulsion_bus_energy_kwh, "kWh"),
          auxiliary_energy: value(management?.summary?.auxiliary_bus_energy_kwh, "kWh"),
        },
        checks,
        warnings: [],
        management_advice: ok ? [`双电池末端 SOC：甲组 ${formatNumber((soc1 ?? 0) * 100, "%")}，乙组 ${formatNumber((soc2 ?? 0) * 100, "%")}`, "当前是具名情景的软件仿真结果，尚未经实船参数校准。"] : [],
        assumptions: Array.isArray(assumptions) ? assumptions.map(item => typeof item === "string" ? item : JSON.stringify(item)) : [],
      },
      questions: pending ? [pending.question || "请补充缺失任务信息。"] : [],
      missing_fields: task.missing_fields || [],
      final_message: reason,
      failed_tool: observations.findLast(event => event.outcome_code && event.outcome_code !== "ok")?.tool || null,
    };
    if (ok && task.task_type === "planning") {
      const selected = request.objective === "min_time" ? "min_time" : "min_energy";
      result.plan_alternatives = ["min_energy", "min_time"].map(goal => goal === selected ? {
        goal, duration_h: duration / 3600, required_energy_kwh: energy,
        soc_final: Math.min(soc1, soc2), speeds_kmh: speed?.summary?.speed_vector_kmh || [],
      } : { goal, duration_h: null, required_energy_kwh: null, soc_final: null, speeds_kmh: [] });
    }
    return result;
  }

  async function renderDetails(result) {
    const events = result.research_events || [];
    const toolEvents = events.filter(event => ["tool_started", "tool_completed", "tool_failed"].includes(event.type));
    const actions = events.filter(event => event.type === "action_requested");
    const models = events.filter(event => event.type === "model_completed");
    const tools = $("#trace-list");
    tools.innerHTML = toolEvents.map(event => `<li class="trace-item"><span class="trace-index">${event.seq}</span><div><strong>${escapeHtml(event.tool || "工具")} · ${escapeHtml(event.type.replace("tool_", ""))}</strong><p>${escapeHtml(event.outcome_code || event.backend || event.parent_call_id || "")}${event.duration_ms != null ? ` · ${event.duration_ms} ms` : ""}</p>${event.artifact_ref ? `<small>工件：${escapeHtml(event.artifact_ref)}</small>` : ""}</div></li>`).join("");
    $("#trace-meta").textContent = `${toolEvents.filter(event => event.type === "tool_completed").length} 次完成`;
    const decisionList = $("#agent-decision-list");
    if (!decisionList) return;
    const modelViews = await Promise.all(models.map(async event => {
      try {
        const payload = await artifact(result.thread_id, event.artifact_ref);
        return { event, response: payload?.response?.message || {} };
      } catch (_error) {
        return { event, response: {} };
      }
    }));
    if (state.lastResult?.thread_id !== result.thread_id) return;
    const decisionLines = actions.map(event => `<li><strong>${escapeHtml(event.tool || "动作")}</strong><p>${escapeHtml(event.public_rationale || "模型未提供公开依据")}</p><small>调用 ${escapeHtml(event.call_id || "")} · 序号 ${event.seq}</small></li>`).join("");
    const rawLines = modelViews.map(({ event, response }) => {
      let answer = "";
      const call = response.tool_calls?.find(item => item.function?.name === "submit_answer");
      if (call) {
        try { answer = JSON.parse(call.function.arguments).answer_markdown || ""; } catch (_error) { /* Raw JSON is still shown. */ }
      }
      return `<li><strong>${escapeHtml(event.model || "模型")} · ${event.latency_ms ?? "--"} ms</strong>${answer ? `<p class="agent-raw-answer">模型原始答复：${escapeHtml(answer.replaceAll("\\n", "\n"))}</p>` : ""}<pre>${escapeHtml(JSON.stringify(response, null, 2))}</pre><small>响应 ${escapeHtml(event.request_id || "")} · 序号 ${event.seq}</small></li>`;
    }).join("");
    decisionList.innerHTML = `<h4>公开决策依据</h4><ol>${decisionLines || "<li>本次没有决策动作。</li>"}</ol><details><summary>查看模型原始响应</summary><ol>${rawLines || "<li>没有模型响应记录。</li>"}</ol></details>`;
    $("#agent-decision-meta").textContent = `${models.length} 次模型响应 · ${actions.length} 次动作`;
  }

  return { submit, resume, load, wait, renderDetails, structuredText };
})();
