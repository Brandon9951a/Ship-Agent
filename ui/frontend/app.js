const ROUTES = {
  forward: ["平顶山港", "军李船闸", "马湾船闸", "漯河船闸", "漯河港", "大路李船闸", "葫芦湾船闸", "周口船闸", "周口港"],
  reverse: ["周口港", "周口船闸", "葫芦湾船闸", "大路李船闸", "漯河港", "漯河船闸", "马湾船闸", "军李船闸", "平顶山港"],
};

const SAMPLES = {
  normal: "从平顶山港到军李船闸，2026-09-18 09:00出发，SOC85%，半载，6小时内到达",
  time: "从平顶山港到军李船闸，2026-09-18 09:00出发，SOC85%，半载，1小时内到达",
  soc: "从平顶山港到军李船闸，2026-09-18 09:00出发，SOC31%，半载，6小时内到达",
};

const state = { trace: [], finalAnswer: "", running: false };
const $ = selector => document.querySelector(selector);
const escapeHtml = value => String(value ?? "").replace(/[&<>'"]/g, character => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
}[character]));

function refreshIcons() {
  if (window.lucide) window.lucide.createIcons();
}

function tickClock() {
  $("#clock").textContent = new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(new Date());
}

function formatNumber(value, suffix = "", digits = 1) {
  if (value === undefined || value === null || value === "") return "--";
  const number = Number(value);
  return Number.isFinite(number) ? `${number.toFixed(digits)}${suffix}` : `${value}${suffix}`;
}

function formatEta(value) {
  if (!value) return "--";
  const text = String(value).replace("T", " ");
  return text.length >= 16 ? text.slice(5, 16) : text;
}

function updateStatus(kind, text) {
  const target = $("#decision-status");
  target.className = `decision-status ${kind}`;
  target.innerHTML = `<span></span>${escapeHtml(text)}`;
}

function populateSelect(select, values, value) {
  select.innerHTML = values.map(item => `<option value="${escapeHtml(item)}">${escapeHtml(item)}</option>`).join("");
  if (value && values.includes(value)) select.value = value;
}

function syncRouteNodes() {
  const nodes = ROUTES[$("#route-select").value] || ROUTES.forward;
  populateSelect($("#start-select"), nodes, nodes[0]);
  populateSelect($("#end-select"), nodes, nodes.at(-1));
}

function renderProgress(trace = [], running = false) {
  const names = ["Tdata", "Tseg", "Tenergy", "Tspeed", "Tmanagement"];
  const statusByNode = Object.fromEntries(trace.map(item => [item.node, item.status]));
  $("#progress-labels").querySelectorAll("span").forEach((label, index) => {
    const status = statusByNode[names[index]];
    label.className = status === "ok" ? "done" : status ? "curr" : (running && index === 0 ? "curr" : "");
  });
  $("#progress-steps").querySelectorAll(".progress-step").forEach((step, index) => {
    const status = statusByNode[names[index]];
    step.className = status === "ok"
      ? "progress-step complete"
      : status ? "progress-step error"
        : (running && index === 0 ? "progress-step active" : "progress-step");
  });
}

function rawSegments(result) {
  return result.tseg?.payload?.segments || [];
}

function planSegments(result) {
  if (result.status !== "ok") return [];
  return (result.report?.segments || []).map(segment => ({
    id: segment.segment_id,
    origin: segment.origin,
    destination: segment.destination,
    route: `${segment.origin} → ${segment.destination}`,
    distance_km: segment.distance?.value,
    speed_kmh: segment.speed?.value,
    duration_h: segment.duration?.value,
    energy_kwh: segment.propulsion_energy?.value,
    soc_end: segment.soc_end?.value,
    source: segment.distance_source,
    model_id: segment.model_id,
  }));
}

function renderRoute(segments, routeId = "航线待识别") {
  if (!segments.length) {
    $("#route-track").innerHTML = '<div class="empty-inline"><i data-lucide="route"></i><span>航段识别完成后显示路径与速度建议</span></div>';
    $("#segment-summary").innerHTML = "";
    $("#route-chip").textContent = routeId;
    refreshIcons();
    return;
  }
  const nodes = segments.map((segment, index) => ({
    name: segment.origin,
    distance: index ? segments[index - 1].distance_km : null,
  }));
  nodes.push({ name: segments.at(-1).destination, distance: segments.at(-1).distance_km });
  $("#route-chip").textContent = routeId;
  $("#route-track").innerHTML = `<div class="metro-route" style="--stops:${Math.max(nodes.length, 2)}; --ship-from:0%; --ship-to:100%">
    <span class="metro-ship" aria-hidden="true"><i data-lucide="ship"></i></span>
    ${nodes.map((node, index) => {
      const kind = index === 0 ? "metro-origin" : index === nodes.length - 1 ? "metro-destination" : "metro-waypoint";
      return `<div class="metro-stop ${kind}">${node.distance != null ? `<span class="metro-distance">${formatNumber(node.distance, " km")}</span>` : ""}<span class="metro-dot"></span><span class="metro-port">${escapeHtml(node.name)}</span></div>`;
    }).join("")}
  </div>`;
  $("#segment-summary").innerHTML = `<span class="route-key origin-key">起点</span><span class="route-key waypoint-key">途经港口</span><span class="route-key destination-key">终点</span><span class="route-key">${escapeHtml(nodes[0].name)} → ${escapeHtml(nodes.at(-1).name)}</span>`;
  refreshIcons();
}

function renderEnergyChart(segments) {
  if (!segments.length) {
    $("#energy-chart").innerHTML = '<div class="empty-inline"><i data-lucide="chart-no-axes-combined"></i><span>优化完成后显示分段对比</span></div>';
    refreshIcons();
    return;
  }
  const maxEnergy = Math.max(...segments.map(segment => Number(segment.energy_kwh) || 0), 1);
  $(".energy-surface .legend").innerHTML = '<span class="legend-energy"></span> 推荐能耗 <span class="legend-speed"></span> 对地航速';
  $("#energy-chart").innerHTML = `<div class="bar-chart" style="--count:${segments.length}">${segments.map(segment => {
    const height = Math.max(8, ((Number(segment.energy_kwh) || 0) / maxEnergy) * 100);
    return `<div class="bar-group"><span class="bar-metrics"><span class="bar-speed-value">${formatNumber(segment.speed_kmh, " km/h", 2)}</span><span class="bar-value">${formatNumber(segment.energy_kwh, " kWh")}</span></span><div class="bar" style="height:${height}%"></div><span class="bar-label">${escapeHtml(segment.route)}</span></div>`;
  }).join("")}</div>`;
}

function sourceLabel(segment) {
  const source = segment.source;
  if (!source) return "未知来源";
  return `${source.source_id || "未知来源"} · ${source.confirmed ? "已核对" : "待核实"} · ${segment.model_id || "模型未标识"}`;
}

function renderTable(segments) {
  $("#result-count").textContent = `${segments.length} 个航段`;
  $("#segment-table").innerHTML = segments.length ? segments.map(segment => `<tr>
    <td>${escapeHtml(segment.route)}</td>
    <td>${escapeHtml(formatNumber(segment.distance_km, " km"))}</td>
    <td>${escapeHtml(formatNumber(segment.speed_kmh, " km/h", 3))}</td>
    <td>${escapeHtml(formatNumber(segment.duration_h, " h", 2))}</td>
    <td>${escapeHtml(formatNumber(segment.energy_kwh, " kWh"))}</td>
    <td>${escapeHtml(sourceLabel(segment))}</td>
  </tr>`).join("") : '<tr class="placeholder-row"><td colspan="6">等待航速优化结果</td></tr>';
}

function renderSafety(checks, warnings = []) {
  const items = checks.map(check => {
    const actual = check.actual == null ? "--" : `${check.actual} ${check.unit || ""}`;
    const limit = check.limit == null ? "未设置" : `${check.limit} ${check.unit || ""}`;
    return `<div class="safety-item ${check.passed ? "ok" : "error"} state"><span class="safety-label">${escapeHtml(check.name)}</span><strong class="safety-content">${check.passed ? "通过" : "未通过"} · 实际 ${escapeHtml(actual)} · 边界 ${escapeHtml(limit)}</strong></div>`;
  });
  warnings.forEach(warning => items.push(`<div class="safety-item warning state"><span class="safety-label">风险</span><strong class="safety-content">${escapeHtml(warning)}</strong></div>`));
  $("#safety-list").innerHTML = items.length ? items.join("") : '<div class="empty-inline">暂无校验结果</div>';
}

function renderRecommendations(result) {
  const options = result.adjustment_options || [];
  if (options.length) {
    $("#recommendation-list").innerHTML = options.map(option => `<div class="recommendation info">${escapeHtml(option.label || option)}</div>`).join("");
    return;
  }
  const management = result.tmanagement?.payload || {};
  const messages = [];
  if (management.safe) {
    messages.push("电池组1推进优先；电池组2日常负载优先，必要时辅助推进。系统仅给出能量管理建议，不下发接触器控制命令。");
    messages.push(`当前工具计算结束 SOC：${formatNumber((management.soc_final ?? 0) * 100, "%")}；规划下限：${formatNumber((management.soc_min ?? 0) * 100, "%")}。`);
  }
  (management.warnings || []).forEach(warning => messages.push(warning));
  $("#recommendation-list").innerHTML = messages.length
    ? messages.map(message => `<div class="recommendation info">${escapeHtml(message)}</div>`).join("")
    : '<div class="empty-inline">等待 EMS 仿真结果</div>';
}

function renderTrace(trace) {
  state.trace = trace || [];
  $("#trace-list").innerHTML = state.trace.map((item, index) => `<li class="trace-item"><span class="trace-index">${index + 1}</span><div><strong>${escapeHtml(item.node || "流程")}</strong><p>${escapeHtml(item.status || "完成")}</p></div></li>`).join("");
  $("#trace-meta").textContent = state.trace.length ? `${state.trace.length} 个步骤` : "等待任务";
}

function renderModel(result) {
  const reportMode = result.report?.explanation_mode;
  const understandingMode = result.task_understanding?.mode;
  const llmUsed = reportMode === "llm_qualitative" || understandingMode === "llm_qualitative";
  const explanation = result.report?.explanation || result.task_understanding?.text || result.final_message || "暂无模型解释。";
  $("#model-mode").textContent = llmUsed ? "DeepSeek 已真实调用" : "模板回退";
  $("#reply-answer").textContent = explanation;
  $("#reply-panel").hidden = false;
  $("#model-label").textContent = llmUsed ? "DeepSeek 实际调用完成" : "DeepSeek 回退模式";
  $("#model-dot").className = llmUsed ? "status-dot" : "status-dot offline";
}

function showInfeasible(result) {
  const speed = result.tspeed?.payload || result.tspeed || {};
  $("#infeasible-reason").textContent = speed.reason || result.final_message || "当前航次不满足执行条件。";
  const options = result.adjustment_options || [];
  $("#infeasible-suggestion").textContent = options.length
    ? options.map((option, index) => `${index + 1}. ${option.label || option}`).join("\n")
    : (result.questions || []).join("\n") || "请补充所需参数后重新计算。";
  $("#infeasible-alert").hidden = false;
}

function resetResult() {
  state.trace = [];
  state.finalAnswer = "";
  $("#mission-title").textContent = "正在准备决策任务";
  $("#mission-subtitle").textContent = "正在连接 LangGraph 五工具链并调用 DeepSeek。";
  ["#metric-energy", "#metric-time", "#metric-eta", "#metric-soc"].forEach(id => { $(id).textContent = "--"; });
  $("#route-chip").textContent = "推理中";
  renderRoute([]);
  renderEnergyChart([]);
  renderTable([]);
  renderSafety([]);
  renderRecommendations({});
  renderTrace([]);
  renderProgress([], true);
  $("#reply-panel").hidden = true;
  $("#infeasible-alert").hidden = true;
  $("#final-response").hidden = true;
  refreshIcons();
}

function renderResult(result) {
  const okay = result.status === "ok";
  const infeasible = result.status === "infeasible";
  const route = rawSegments(result);
  const segments = planSegments(result);
  const summary = result.report?.summary || {};
  const checks = result.report?.checks || result.tspeed?.payload?.checks || [];
  const warnings = result.report?.warnings || result.tmanagement?.payload?.warnings || [];

  updateStatus(okay ? "success" : "error", okay ? "方案已生成" : infeasible ? "航次不可行" : "需要补充");
  $("#mission-title").textContent = route.length ? `${route[0].origin} → ${route.at(-1).destination}` : (okay ? "方案已生成" : "任务未完成");
  $("#mission-subtitle").textContent = result.final_message || "流程已结束。";
  renderProgress(result.trace || []);
  renderRoute(route, result.tdata?.payload?.route_id || "航线待识别");
  renderModel(result);
  renderTrace(result.trace || []);
  renderRecommendations(result);

  if (okay) {
    $("#metric-energy").textContent = formatNumber(summary.required_energy?.value, " kWh");
    $("#metric-time").textContent = formatNumber(summary.total_duration?.value, " h", 2);
    $("#metric-eta").textContent = formatEta(summary.eta?.value);
    $("#metric-soc").textContent = formatNumber((summary.soc_final?.value ?? 0) * 100, "%");
    renderEnergyChart(segments);
    renderTable(segments);
    renderSafety(checks, warnings);
  } else {
    renderEnergyChart([]);
    renderTable([]);
    renderSafety(checks, warnings);
    showInfeasible(result);
  }

  state.finalAnswer = result.dashboard || result.final_message || "";
  $("#final-answer").textContent = state.finalAnswer;
  $("#final-response").hidden = !state.finalAnswer;
  refreshIcons();
}

async function runInference() {
  const message = $("#mission-input").value.trim();
  if (!message || state.running) {
    if (!message) $("#mission-input").focus();
    return;
  }
  state.running = true;
  $("#run-button").disabled = true;
  updateStatus("running", "推理中");
  resetResult();
  try {
    const response = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task_text: message }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "请求失败");
    renderResult(result);
  } catch (error) {
    updateStatus("error", "计算失败");
    $("#mission-title").textContent = "任务执行失败";
    $("#mission-subtitle").textContent = error.message;
    $("#model-mode").textContent = "调用失败";
    $("#reply-answer").textContent = "工具链或模型调用失败，请检查本地服务状态后重试。";
    $("#reply-panel").hidden = false;
  } finally {
    state.running = false;
    $("#run-button").disabled = false;
  }
}

function writeQuickTask() {
  const departure = $("#departure-input").value.replace("T", " ");
  const hours = Number($("#adv-time").value || $("#time-input").value || 6);
  const soc = Number($("#adv-soc").value || 85);
  const load = $("#adv-load").value;
  $("#time-input").value = hours;
  $("#mission-input").value = `从${$("#start-select").value}到${$("#end-select").value}，${departure}出发，SOC${soc}%，${load}，${hours}小时内到达`;
}

async function loadHealth() {
  try {
    const health = await (await fetch("/healthz")).json();
    const enabled = health.llm_mode === "enabled";
    $("#model-label").textContent = enabled ? "DeepSeek API 已启用" : "DeepSeek 模板回退";
    $("#model-copy").textContent = enabled ? "DeepSeek 将真实参与任务理解与解释" : "模型不可用，当前使用模板回退";
    $("#model-dot").className = enabled ? "status-dot" : "status-dot offline";
  } catch (_error) {
    $("#model-label").textContent = "本地决策引擎未连接";
    $("#model-dot").className = "status-dot offline";
  }
}

function bindControls() {
  $("#route-select").addEventListener("change", syncRouteNodes);
  $("#time-minus").addEventListener("click", () => {
    $("#time-input").value = Math.max(0.5, Number($("#time-input").value || 6) - 0.5);
    $("#adv-time").value = $("#time-input").value;
  });
  $("#time-plus").addEventListener("click", () => {
    $("#time-input").value = Math.min(72, Number($("#time-input").value || 6) + 0.5);
    $("#adv-time").value = $("#time-input").value;
  });
  $("#quick-fill").addEventListener("click", writeQuickTask);
  $("#run-button").addEventListener("click", runInference);
  $("#mission-input").addEventListener("keydown", event => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") runInference();
  });
  $("#clear-button").addEventListener("click", () => {
    $("#mission-input").value = "";
    resetResult();
    updateStatus("idle", "待命");
    $("#mission-title").textContent = "等待航行任务";
    $("#mission-subtitle").textContent = "输入自然语言需求，或使用左侧快捷航线开始。";
  });
  document.querySelectorAll("[data-sample]").forEach(button => button.addEventListener("click", () => {
    $("#mission-input").value = SAMPLES[button.dataset.sample];
  }));
  $("#copy-final").addEventListener("click", async () => {
    if (state.finalAnswer) await navigator.clipboard.writeText(state.finalAnswer);
  });
}

window.addEventListener("DOMContentLoaded", async () => {
  tickClock();
  setInterval(tickClock, 1000);
  syncRouteNodes();
  bindControls();
  refreshIcons();
  await loadHealth();
});
