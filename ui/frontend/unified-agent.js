/* The original decision page now drives the durable LangGraph tool loop. */
(() => {
  const $ = id => document.getElementById(id);
  const state = { user: null, csrf: "", task: null, generation: 0, seq: 0, events: [], timer: null,
    polling: false, modelConfigured: false, pendingId: null, responseId: null };
  const toolNames = ["Tdata", "Tseg", "Tenergy", "Tspeed", "Tmanagement"];
  const auxiliaryNames = ["search_project_docs", "query_task_results", "calculate"];
  const toolLabels = { Tdata: "船态与电池", Tseg: "航段与约束", Tenergy: "功率预测",
    Tspeed: "航速搜索", Tmanagement: "双电池管理", search_project_docs: "资料检索",
    query_task_results: "结果查询", calculate: "计算" };
  const statusLabels = { queued: "排队中", running: "工具运行中", waiting_human: "等待补充",
    completed: "已完成", failed: "执行中断" };
  const outcomeLabels = { research_feasible: "仿真方案通过复核", prediction_completed: "短时预测完成",
    question_answered: "问答完成", infeasible_under_assumptions: "当前条件下未找到可行方案",
    model_out_of_domain: "超出预测窗口", needs_input: "需要补充信息",
    not_supported: "所选情景不支持这项计算", no_usable_plan: "未形成可用方案",
    agent_budget_exhausted: "本次调用次数已用尽", model_unavailable: "模型连接失败",
    tool_error: "工具执行出错", verification_failed: "复核未通过", search_unresolved: "重搜未解决",
    process_interrupted: "服务重启中断了计算", invalid_input: "输入值需修正" };
  const eventLabels = { model_started: "请求模型", model_completed: "模型响应", model_failed: "模型请求失败",
    action_requested: "模型请求动作", action_rejected: "动作检查拒绝", tool_started: "开始工具",
    tool_completed: "工具完成", tool_failed: "工具失败", plan_verified: "独立复核通过",
    verification_failed: "独立复核失败", answer_completed: "答复完成", run_failed: "运行中断",
    waiting_human: "等待补充信息", human_response: "收到补充信息", input_normalized: "输入归一" };
  const metricLabels = { distance_km: ["航程", "km"], duration_s: ["航行历时", "s"],
    bus_energy_kwh: ["母线能耗", "kWh"], motor_energy_kwh: ["电机能耗", "kWh"],
    final_soc_1: ["甲组末端 SOC", "fraction"], final_soc_2: ["乙组末端 SOC", "fraction"],
    calculated_value: ["计算结果", ""] };
  function status(kind, label) {
    $("decision-status").className = "decision-status " + kind;
    $("decision-status").replaceChildren();
    $("decision-status").append(document.createElement("span"), document.createTextNode(label));
  }
  function historyKey() { return "ship-agent.unified-history." + state.user.id; }
  function history() {
    try { return JSON.parse(localStorage.getItem(historyKey()) || "[]"); } catch (_) { return []; }
  }
  function remember(task) {
    const rows = history().filter(item => item.id !== task.task_id);
    rows.unshift({ id: task.task_id, text: task.task_text, scenario: task.scenario_id,
      type: task.task_type, created: task.created_at });
    try { localStorage.setItem(historyKey(), JSON.stringify(rows.slice(0, 25))); } catch (_) { /* Optional history. */ }
    renderHistory();
  }
  function renderHistory() {
    const rows = history();
    $("history-count").textContent = rows.length;
    $("history-list").replaceChildren();
    if (!rows.length) {
      const empty = document.createElement("p"); empty.className = "history-empty";
      empty.textContent = "当前浏览器还没有任务。"; $("history-list").append(empty); return;
    }
    rows.forEach(item => {
      const button = document.createElement("button"); button.type = "button";
      button.className = "history-entry" + (item.id === state.task?.task_id ? " active" : "");
      const title = document.createElement("strong"); title.textContent = item.text || item.id;
      const meta = document.createElement("small"); meta.textContent = item.created?.slice(0, 16).replace("T", " ") || item.id.slice(0, 8);
      button.append(title, meta);
      button.addEventListener("click", () => openTask(item.id));
      $("history-list").append(button);
    });
  }
  async function api(path, options = {}) {
    const method = options.method || "GET";
    const headers = { ...(options.headers || {}) };
    if (method !== "GET" && path !== "/api/auth/login") headers["X-CSRF-Token"] = state.csrf;
    const response = await fetch(path, { ...options, method, headers, credentials: "same-origin", cache: "no-store" });
    const body = response.status === 204 ? {} : await response.json().catch(() => ({}));
    if (response.status === 401 && path !== "/api/auth/login") {
      showLogin("登录已过期，请重新登录。");
      throw new Error("登录已过期");
    }
    if (!response.ok) throw new Error(body.message || body.error || `HTTP ${response.status}`);
    return body;
  }
  function showLogin(message = "") {
    clearInterval(state.timer); state.timer = null; state.generation += 1;
    state.user = null; state.csrf = ""; state.task = null; state.pendingId = null; state.responseId = null;
    clearTaskView(); $("history-list").replaceChildren();
    $("app-shell").hidden = true; $("login-screen").hidden = false;
    $("login-feedback").textContent = message;
  }
  async function authenticated(session) {
    state.user = session.user; state.csrf = session.csrf_token;
    $("session-name").textContent = session.user.display_name || session.user.username;
    $("login-screen").hidden = true; $("app-shell").hidden = false;
    const config = await api("/api/research/config");
    state.modelConfigured = config.model_configured === true;
    $("model-availability").textContent = state.modelConfigured
      ? `真实模型已配置：${config.requested_model}。实际响应模型见右侧记录。`
      : "真实模型尚未启用；需以 --llm 和 --env-file 启动服务。";
    $("model-availability").classList.toggle("warning", !state.modelConfigured);
    $("run-button").disabled = !state.modelConfigured;
    $("scenario-select").replaceChildren(...Object.entries(config.scenarios).map(([id, item]) => new Option(item.label, id)));
    renderHistory();
    const latest = history()[0];
    if (latest) await openTask(latest.id).catch(() => {});
    const health = await fetch("/healthz", { cache: "no-store" }).then(r => r.ok).catch(() => false);
    $("service-label").textContent = health ? "规划服务在线" : "规划服务异常";
    $("service-dot").classList.toggle("offline", !health);
  }
  async function login(event) {
    event.preventDefault();
    $("login-submit").disabled = true; $("login-feedback").textContent = "";
    try {
      const session = await api("/api/auth/login", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: $("login-username").value.trim(), password: $("login-password").value }) });
      $("login-password").value = "";
      await authenticated(session);
    } catch (error) { $("login-feedback").textContent = error.message; }
    finally { $("login-submit").disabled = false; }
  }
  function clearTaskView() {
    state.seq = 0; state.events = [];
    state.pendingId = null; state.responseId = null;
    $("event-list").replaceChildren(); $("raw-list").replaceChildren();
    $("metrics-section").replaceChildren(); $("metrics-section").hidden = true;
    $("source-list").replaceChildren(); $("source-details").hidden = true;
    $("event-count").textContent = "0 条"; $("model-count").textContent = "0 次";
    $("response-model").textContent = "等待模型";
    $("answer-text").textContent = "模型正在选择动作，工具执行记录会持续更新…";
    $("answer-note").textContent = ""; $("clarification-section").hidden = true;
    renderChain();
  }
  function renderMetrics(metrics) {
    const entries = Object.entries(metrics || {});
    $("metrics-section").hidden = entries.length === 0;
    $("metrics-section").replaceChildren(...entries.map(([key, value]) => {
      const card = document.createElement("article"); card.className = "agent-metric surface";
      const label = document.createElement("span"), number = document.createElement("strong"), source = document.createElement("small");
      const [name, fallbackUnit] = metricLabels[key] || [key.replaceAll("_", " "), value.unit || ""];
      label.textContent = name;
      const numeric = Number(value.value);
      number.textContent = Number.isFinite(numeric)
        ? (value.unit === "fraction" ? (numeric * 100).toFixed(1) + "%" :
          value.unit === "s" && key === "duration_s" ? (numeric / 60).toFixed(1) + " min" :
          Number(numeric.toPrecision(6)) + (value.unit || fallbackUnit ? " " + (value.unit || fallbackUnit) : ""))
        : String(value.value ?? "--");
      source.textContent = value.source_ref ? "工具结果 · " + value.source_ref.slice(0, 12) : "工具计算";
      card.append(label, number, source); return card;
    }));
  }
  function renderAnswer(markdown) {
    const target = $("answer-text");
    target.replaceChildren();
    let list = null;
    const inline = (parent, value) => {
      const chunks = value.split(/(\*\*[^*\n]+\*\*)/g);
      chunks.forEach(chunk => {
        if (chunk.startsWith("**") && chunk.endsWith("**")) {
          const strong = document.createElement("strong"); strong.textContent = chunk.slice(2, -2); parent.append(strong);
        } else parent.append(document.createTextNode(chunk));
      });
    };
    String(markdown).split("\n").forEach(line => {
      if (!line.trim()) { list = null; return; }
      const bullet = /^\s*[-*]\s+(.+)$/.exec(line);
      if (bullet) {
        if (!list) { list = document.createElement("ul"); target.append(list); }
        const item = document.createElement("li"); inline(item, bullet[1]); list.append(item);
      } else {
        list = null;
        const paragraph = document.createElement("p"); inline(paragraph, line); target.append(paragraph);
      }
    });
  }
  function renderChain() {
    const taskType = state.task?.task_type;
    const names = taskType === "question" ? auxiliaryNames : taskType === "prediction"
      ? ["Tdata", "Tenergy"] : toolNames;
    $("chain-heading").textContent = taskType === "question" ? "辅助工具执行状态" :
      taskType === "prediction" ? "短时预测工具状态" : "五工具执行状态";
    const states = Object.fromEntries(names.map(name => [name, "idle"]));
    state.events.forEach(event => {
      if (!states.hasOwnProperty(event.tool)) return;
      if (event.type === "tool_started") states[event.tool] = "running";
      if (event.type === "tool_completed") states[event.tool] = "ok";
      if (event.type === "tool_failed") states[event.tool] = "failed";
    });
    $("chain-steps").replaceChildren(...names.map(name => {
      const step = document.createElement("div"); step.className = "chain-step " + states[name];
      const icon = document.createElement("span"); icon.textContent = states[name] === "ok" ? "✓" : states[name] === "failed" ? "!" : "·";
      const text = document.createElement("div");
      const strong = document.createElement("strong"); strong.textContent = name;
      const small = document.createElement("small"); small.textContent = toolLabels[name];
      text.append(strong, small); step.append(icon, text); return step;
    }));
    $("chain-count").textContent = Object.values(states).filter(value => value === "ok").length + " / " + names.length;
  }
  function showTask(task) {
    if (task.pending?.pending_id !== state.pendingId) {
      state.pendingId = task.pending?.pending_id || null;
      state.responseId = null;
    }
    state.task = task;
    const outcome = task.outcome_code;
    const terminal = ["completed", "failed"].includes(task.status);
    const success = ["research_feasible", "prediction_completed", "question_answered"].includes(outcome);
    $("mission-title").textContent = terminal ? (outcomeLabels[outcome] || statusLabels[task.status]) : statusLabels[task.status] || task.status;
    $("mission-subtitle").textContent = terminal
      ? `任务 ${task.task_id.slice(0, 8)} · ${task.scenario_id} · ${task.task_type}`
      : "模型正在调用工具；可以展开下方过程查看实时记录。";
    status(task.status === "failed" ? "error" : task.status === "waiting_human" ? "warning" :
      task.status === "completed" ? (success ? "success" : "warning") : "running", statusLabels[task.status] || task.status);
    $("task-id").textContent = task.task_id ? "任务 ID：" + task.task_id : "";
    if (task.final_answer) renderAnswer(task.final_answer);
    else if (task.status === "failed") $("answer-text").textContent = "执行中断。请展开调用过程和模型原始响应查看最后一步；可修改条件后重新运行。";
    else if (task.status === "waiting_human") $("answer-text").textContent = "请补充下方信息，智能体会从当前任务继续。";
    $("answer-note").textContent = task.answer_status === "accepted"
      ? "模型依据工具证据生成；数值由后端引用与校验。"
      : task.answer_status ? "模型答复未通过校验或不可用。上方为系统保存的答复，下方仍可查看原始响应。" : "";
    $("clarification-section").hidden = task.status !== "waiting_human";
    $("clarification-question").textContent = task.pending?.question || "";
    renderMetrics(task.metrics);
    const sources = task.answer_sources || [];
    $("source-details").hidden = sources.length === 0;
    $("source-list").replaceChildren(...sources.map(item => {
      const line = document.createElement("p");
      line.textContent = `${item.source} · ${item.section} · SHA256 ${item.content_hash}`;
      return line;
    }));
    $("scenario-select").value = task.scenario_id;
    $("task-type-select").value = task.task_type;
    renderChain();
    renderHistory();
  }
  function artifactButton(event, container) {
    if (!event.artifact_ref || !state.task) return;
    const button = document.createElement("button"); button.type = "button"; button.textContent = "查看校验工件";
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const artifact = await api(`/api/runs/${state.task.task_id}/artifacts/${event.artifact_ref}`);
        let pre = container.querySelector("pre");
        if (!pre) { pre = document.createElement("pre"); container.append(pre); }
        pre.textContent = JSON.stringify(artifact.payload, null, 2);
      } catch (error) { button.textContent = "读取失败：" + error.message; }
      finally { button.disabled = false; }
    });
    container.append(button);
  }
  function appendEvent(event) {
    state.events.push(event);
    const row = document.createElement("details"), summary = document.createElement("summary");
    const name = event.tool ? " · " + (toolLabels[event.tool] || event.tool) : event.model ? " · " + event.model : "";
    summary.textContent = `${event.seq} · ${eventLabels[event.type] || event.type}${name}${event.outcome_code ? " · " + event.outcome_code : ""}`;
    const body = document.createElement("div"); body.className = "event-body";
    if (event.public_rationale) {
      const reason = document.createElement("p"); reason.textContent = "公开依据：" + event.public_rationale; body.append(reason);
    }
    const pre = document.createElement("pre"); pre.textContent = JSON.stringify(event, null, 2);
    body.append(pre); artifactButton(event, body); row.append(summary, body); $("event-list").append(row);
    $("event-count").textContent = state.events.length + " 条";
    if (event.type === "model_completed") appendModel(event);
    if (event.type === "model_completed" && event.model) $("response-model").textContent = event.model;
    renderChain();
  }
  function appendModel(event) {
    const row = document.createElement("details"), summary = document.createElement("summary"), pre = document.createElement("pre");
    summary.textContent = `${event.model || "模型"} · 请求 ${event.request_id || "无标识"} · ${event.latency_ms ?? "?"} ms`;
    pre.textContent = "展开后读取模型的原始 API 响应…";
    row.append(summary, pre); $("raw-list").append(row);
    $("model-count").textContent = $("raw-list").childElementCount + " 次";
    row.addEventListener("toggle", async () => {
      if (!row.open || row.dataset.loaded || !state.task) return;
      row.dataset.loaded = "1";
      try {
        const artifact = await api(`/api/runs/${state.task.task_id}/artifacts/${event.artifact_ref}`);
        pre.textContent = JSON.stringify(artifact.payload?.response?.message || artifact.payload?.response || {}, null, 2);
      } catch (error) { pre.textContent = "读取失败：" + error.message; row.dataset.loaded = ""; }
    });
  }
  async function poll(generation) {
    if (state.polling || !state.task || generation !== state.generation) return;
    state.polling = true;
    try {
      const id = state.task.task_id;
      const [task, page] = await Promise.all([
        api(`/api/runs/${id}`), api(`/api/runs/${id}/events?after_seq=${state.seq}`)]);
      if (generation !== state.generation || state.task?.task_id !== id) return;
      page.events.forEach(event => { appendEvent(event); state.seq = event.seq; });
      showTask(task);
      if (["completed", "failed", "waiting_human"].includes(task.status) && page.events.length < 200) {
        clearInterval(state.timer); state.timer = null;
      }
    } catch (error) {
      if (generation === state.generation) $("mission-subtitle").textContent = "读取任务状态失败：" + error.message + "。正在重试。";
    } finally { state.polling = false; }
  }
  async function openTask(id, initial = null) {
    const generation = ++state.generation;
    clearInterval(state.timer); state.timer = null;
    clearTaskView();
    const task = initial || await api(`/api/runs/${id}`);
    if (generation !== state.generation) return;
    state.task = task;
    $("mission-input").value = task.task_text || $("mission-input").value;
    showTask(task); remember(task);
    state.timer = setInterval(() => poll(generation), 1000);
    await poll(generation);
  }
  async function runTask() {
    const text = $("mission-input").value.trim();
    if (!text) { $("mission-input").focus(); return; }
    if (!state.modelConfigured) { $("model-availability").textContent = "真实模型未启用，请检查服务配置。"; return; }
    $("run-button").disabled = true;
    try {
      const task = await api("/api/run", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workflow_mode: "research_agent", task_text: text,
          scenario_id: $("scenario-select").value, task_type: $("task-type-select").value }) });
      await openTask(task.task_id, task);
    } catch (error) {
      $("mission-title").textContent = "任务未启动";
      $("mission-subtitle").textContent = error.message;
      status("error", "请求失败");
    } finally { $("run-button").disabled = !state.modelConfigured; }
  }
  async function resumeTask() {
    if (!state.task?.pending) return;
    const reply = $("clarification-reply").value.trim();
    if (!reply) { $("clarification-reply").focus(); return; }
    $("clarification-submit").disabled = true;
    try {
      const task = await api("/api/resume", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workflow_mode: "research_agent", thread_id: state.task.task_id,
          pending_id: state.task.pending.pending_id, revision: state.task.pending.revision,
          response_id: state.responseId ||= crypto.randomUUID(), reply_text: reply }) });
      $("clarification-reply").value = "";
      showTask(task);
      const generation = state.generation;
      state.timer ||= setInterval(() => poll(generation), 1000);
      await poll(generation);
    } catch (error) { $("mission-subtitle").textContent = "恢复失败：" + error.message; }
    finally { $("clarification-submit").disabled = false; }
  }
  function applyQuick() {
    const origin = $("quick-origin").value.trim(), destination = $("quick-destination").value.trim();
    const hours = Number($("quick-hours").value), soc = Number($("quick-soc").value);
    if (!origin || !destination || origin === destination || $("quick-hours").value === "" ||
        $("quick-soc").value === "" || !Number.isFinite(hours) || hours <= 0 ||
        !Number.isFinite(soc) || soc < 0 || soc > 100) {
      $("mission-subtitle").textContent = "请检查起终点、航时和 SOC。"; return;
    }
    $("mission-input").value = `从${origin}到${destination}，两组电池初始 SOC${soc}%，${hours}小时内到达` +
      ($("quick-load").value ? `，${$("quick-load").value}` : "") + "，按具名仿真情景规划节能航行。";
    $("task-type-select").value = "planning";
  }
  const examples = {
    route: ["route_success", "planning", "从漯河船闸到漯河港，按当前仿真情景规划节能航行。"],
    wait: ["multi_wait", "planning", "从仿真起点到仿真终点，考虑中间等待，规划节能航行。"],
    prediction: ["lstm_short", "prediction", "使用已登记的真实 LSTM 权重，对约 20 秒固定短轨迹进行功率预测并核验 Tspeed 评价器交接。"],
    docs: ["route_success", "question", "项目资料中，当前 LSTM 模型的适用范围和正式规划限制是什么？请检索并给出来源。"]
  };
  document.addEventListener("DOMContentLoaded", async () => {
    $("login-form").addEventListener("submit", login);
    $("logout-button").addEventListener("click", async () => {
      try { await api("/api/auth/logout", { method: "POST" }); } catch (_) { /* Clear local view. */ }
      showLogin("已退出登录。");
    });
    $("run-button").addEventListener("click", runTask);
    $("mission-input").addEventListener("keydown", event => {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") runTask();
    });
    $("clarification-submit").addEventListener("click", resumeTask);
    $("quick-apply").addEventListener("click", applyQuick);
    document.querySelectorAll("[data-example]").forEach(button => button.addEventListener("click", () => {
      const [scenario, type, prompt] = examples[button.dataset.example];
      $("scenario-select").value = scenario; $("task-type-select").value = type;
      $("mission-input").value = prompt;
    }));
    renderChain();
    try { await authenticated(await api("/api/auth/me")); }
    catch (error) { showLogin(error.message === "登录已过期" ? "" : error.message); }
  });
})();
