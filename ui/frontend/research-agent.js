/* Shares the existing authenticated fetch/CSRF client; all evidence is rendered as text. */
(() => {
  const panel = document.createElement("section");
  panel.className = "research-panel";
  panel.innerHTML = '<h2>研究智能体 · 真实模型调用</h2><p>具名仿真情景，真实 DE 与双电池 FDP 求解。与下方 v1 演示流程独立。</p><p id="research-model">登录后加载模型配置</p><div class="research-controls"><label>仿真情景<select id="research-scenario"></select></label><label>任务类型<select id="research-kind"><option value="auto">自动识别</option><option value="planning">航行规划</option><option value="prediction">LSTM 短轨迹</option><option value="question">资料 / 结果 / 换算问答</option></select></label></div><label for="research-text">任务或结果追问</label><textarea id="research-text">从漯河船闸到漯河港，按当前仿真情景规划节能航行。</textarea><button id="research-run" type="button">运行研究任务</button><p class="research-status" id="research-status" role="status"></p><div id="research-pending" hidden><p id="research-question"></p><label for="research-reply">补充任务信息</label><textarea id="research-reply"></textarea><button id="research-resume" type="button">提交并继续</button></div><div id="research-answer"></div><pre id="research-metrics" hidden></pre><details><summary>工具调用、模型响应与来源证据</summary><div id="research-events"></div></details>';
  document.querySelector(".workspace").prepend(panel);
  const metricsBox = document.createElement("div");
  metricsBox.id = "research-metrics"; metricsBox.hidden = true;
  panel.querySelector("#research-metrics").replaceWith(metricsBox);
  const el = id => document.getElementById("research-" + id);
  let task = null, seq = 0, timer = null, loadedUser = null, busy = false, responseId = null;
  const showError = e => { el("status").textContent = "操作失败：" + e.message; };
  async function json(path, options) {
    const response = await apiFetch(path, options);
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || response.status);
    return body;
  }
  function saveTask() { localStorage.setItem("ship-research-" + loadedUser, task.task_id); }
  function render(result) {
    if (result.pending?.pending_id !== task?.pending?.pending_id) responseId = null;
    task = result;
    el("status").textContent = result.status + " · " + (result.outcome_code || result.phase || "") + " · 任务 " + result.task_id;
    el("answer").textContent = result.final_answer || "";
    el("pending").hidden = result.status !== "waiting_human";
    el("question").textContent = result.pending?.question || "";
    el("metrics").hidden = !result.metrics;
    const labels = {duration_s:"航行历时",distance_km:"航程",bus_energy_kwh:"母线能耗",motor_energy_kwh:"电机能耗",
      final_soc_1:"甲组末端 SOC",final_soc_2:"乙组末端 SOC",calculated_value:"计算结果"};
    el("metrics").replaceChildren(...Object.entries(result.metrics || {}).map(([key,item]) => {
      const line = document.createElement("p");
      line.textContent = (labels[key] || key) + "：" + Number(item.value).toPrecision(6) + " " + item.unit;
      return line;
    }));
    el("run").disabled = ["queued", "running"].includes(result.status);
  }
  function eventRow(event) {
    const row = document.createElement("details"), label = document.createElement("summary"), body = document.createElement("pre");
    label.textContent = (event.parent_call_id ? "↳ " : "") + event.seq + " · " + event.type + " " + (event.tool || event.model || "") + (event.outcome_code ? " · " + event.outcome_code : "");
    body.textContent = JSON.stringify(event, null, 2);
    row.append(label, body);
    if (event.artifact_ref) {
      const button = document.createElement("button");
      button.type = "button"; button.textContent = "查看已校验工件";
      button.onclick = async () => {
        try { body.textContent = JSON.stringify(await json("/api/runs/" + task.task_id + "/artifacts/" + event.artifact_ref), null, 2); }
        catch (e) { showError(e); }
      };
      row.append(button);
    }
    el("events").append(row);
    if (event.type === "model_completed") el("model").textContent = "真实 API 已响应 · " + event.model + " · 请求 " + event.request_id;
  }
  async function poll() {
    if (busy || !task || !state.authUser) return;
    busy = true;
    try {
      const id = task.task_id;
      const result = await json("/api/runs/" + id);
      const events = await json("/api/runs/" + id + "/events?after_seq=" + seq);
      if (!state.authUser || task?.task_id !== id) return;
      events.events.forEach(event => { eventRow(event); seq = event.seq; });
      render(result);
      if (["completed", "failed", "waiting_human"].includes(result.status) && events.events.length < 200) { clearInterval(timer); timer = null; }
    } catch (e) { showError(e); clearInterval(timer); timer = null; }
    finally { busy = false; }
  }
  function begin(result) {
    clearInterval(timer); seq = 0; el("events").replaceChildren(); responseId = null;
    el("text").value = result.task_text;
    el("scenario").value = result.scenario_id;
    el("kind").value = result.task_type;
    render(result); saveTask(); timer = setInterval(poll, 1000); poll();
  }
  el("run").onclick = async () => {
    el("run").disabled = true;
    try { begin(await json("/api/run", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({
      workflow_mode:"research_agent", task_text:el("text").value, scenario_id:el("scenario").value, task_type:el("kind").value})})); }
    catch(e) { showError(e); el("run").disabled = false; }
  };
  el("resume").onclick = async () => {
    el("resume").disabled = true;
    responseId ||= crypto.randomUUID();
    try {
      const result = await json("/api/resume", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({
        workflow_mode:"research_agent", thread_id:task.task_id, ...task.pending, response_id:responseId, reply_text:el("reply").value})});
      render(result); clearInterval(timer); timer = setInterval(poll,1000); poll();
    } catch(e) { showError(e); }
    finally { el("resume").disabled = false; }
  };
  setInterval(async () => {
    const owner = state.authUser?.id;
    if (owner === loadedUser) return;
    clearInterval(timer); task = null; seq = 0; loadedUser = owner;
    el("events").replaceChildren(); el("answer").textContent = ""; el("metrics").hidden = true; el("pending").hidden = true;
    if (!owner) return;
    try {
      const config = await json("/api/research/config");
      el("model").textContent = config.model_configured ? "已配置 " + config.requested_model + "，等待本次真实响应" : "模型未启用；请使用 --llm 和 --env-file 启动";
      el("scenario").replaceChildren(...Object.entries(config.scenarios).map(([value, item]) => new Option(item.label, value)));
      const saved = localStorage.getItem("ship-research-" + owner);
      if (saved) begin(await json("/api/runs/" + saved));
    } catch(e) { showError(e); }
  },1000);
})();
