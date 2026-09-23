const ROUTES = {
  forward: ["平顶山港", "军李船闸", "马湾船闸", "漯河船闸", "漯河港", "大路李船闸", "葫芦湾船闸", "周口船闸", "周口港"],
  reverse: ["周口港", "周口船闸", "葫芦湾船闸", "大路李船闸", "漯河港", "漯河船闸", "马湾船闸", "军李船闸", "平顶山港"],
};

const SAMPLES = {
  normal: "从平顶山港到军李船闸，2026-09-18 09:00出发，SOC85%，半载，6小时内到达",
  time: "从平顶山港到军李船闸，2026-09-18 09:00出发，SOC85%，半载，1小时内到达",
  soc: "从平顶山港到军李船闸，2026-09-18 09:00出发，SOC31%，半载，6小时内到达",
};

const state = { trace: [], finalAnswer: "", running: false, lastResult: null };
const ALL_ROUTE_NODES = [...new Set(ROUTES.forward)];
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

function localDateTimeValue(value) {
  return value ? String(value).slice(0, 16) : "";
}

function withLocalOffset(value) {
  if (!value) return null;
  const date = new Date(value);
  const offsetMinutes = Number.isNaN(date.getTime()) ? -480 : date.getTimezoneOffset();
  const sign = offsetMinutes <= 0 ? "+" : "-";
  const absolute = Math.abs(offsetMinutes);
  const hours = String(Math.floor(absolute / 60)).padStart(2, "0");
  const minutes = String(absolute % 60).padStart(2, "0");
  return `${value.length === 16 ? `${value}:00` : value}${sign}${hours}:${minutes}`;
}

function fillCorrectionForm(request = {}) {
  const origin = request.origin || $("#start-select").value || ALL_ROUTE_NODES[0];
  const destination = request.destination || $("#end-select").value || ALL_ROUTE_NODES.at(-1);
  populateSelect($("#correction-origin"), ALL_ROUTE_NODES, origin);
  populateSelect($("#correction-destination"), ALL_ROUTE_NODES, destination);
  $("#correction-departure").value = localDateTimeValue(request.departure_at) || $("#departure-input").value;
  $("#correction-duration").value = request.max_duration_h ?? $("#adv-time").value ?? "";
  $("#correction-soc").value = request.soc_initial == null
    ? $("#adv-soc").value
    : Number(request.soc_initial) * 100;
  $("#correction-load").value = request.load_state || $("#adv-load").value || "半载";
}

function readCorrectionPayload() {
  const origin = $("#correction-origin").value;
  const destination = $("#correction-destination").value;
  const departure = $("#correction-departure").value;
  const duration = Number($("#correction-duration").value);
  const socPercent = Number($("#correction-soc").value);
  if (!origin || !destination || origin === destination) throw new Error("起点和终点必须不同。");
  if (!departure) throw new Error("请填写出发时间。");
  if (!Number.isFinite(duration) || duration <= 0) throw new Error("最长航时必须大于 0 小时。");
  if (!Number.isFinite(socPercent) || socPercent < 0 || socPercent > 100) throw new Error("初始 SOC 必须在 0% 到 100% 之间。");
  const prior = state.lastResult?.request || {};
  const loadState = $("#correction-load").value;
  return {
    origin,
    destination,
    departure_at: withLocalOffset(departure),
    arrival_deadline: null,
    max_duration_h: duration,
    soc_initial: socPercent / 100,
    load_state: loadState,
    draft_m: prior.load_state === loadState ? prior.draft_m ?? null : null,
    environment: prior.environment || {},
  };
}

function syncCorrectionToTask(payload) {
  const departure = localDateTimeValue(payload.departure_at).replace("T", " ");
  const soc = Number(payload.soc_initial) * 100;
  const socText = String(Number(soc.toFixed(1)));
  const durationText = String(Math.ceil(Number(payload.max_duration_h) * 1000) / 1000);
  $("#mission-input").value = `从${payload.origin}到${payload.destination}，${departure}出发，SOC${socText}%，${payload.load_state}，${durationText}小时内到达`;
  $("#departure-input").value = localDateTimeValue(payload.departure_at);
  $("#adv-time").value = payload.max_duration_h;
  $("#time-input").value = payload.max_duration_h;
  $("#adv-soc").value = Number(soc.toFixed(1));
  $("#adv-load").value = payload.load_state;
  const originIndex = ALL_ROUTE_NODES.indexOf(payload.origin);
  const destinationIndex = ALL_ROUTE_NODES.indexOf(payload.destination);
  $("#route-select").value = originIndex <= destinationIndex ? "forward" : "reverse";
  syncRouteNodes();
  if ([...$("#start-select").options].some(option => option.value === payload.origin)) $("#start-select").value = payload.origin;
  if ([...$("#end-select").options].some(option => option.value === payload.destination)) $("#end-select").value = payload.destination;
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
  $("#route-chip").textContent = `${segments[0].origin}—${segments.at(-1).destination}`;
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

function renderTable(segments) {
  $("#result-count").textContent = `${segments.length} 个航段`;
  $("#segment-table").innerHTML = segments.length ? segments.map(segment => `<tr>
    <td>${escapeHtml(segment.route)}</td>
    <td>${escapeHtml(formatNumber(segment.distance_km, " km"))}</td>
    <td>${escapeHtml(formatNumber(segment.speed_kmh, " km/h", 3))}</td>
    <td>${escapeHtml(formatNumber(segment.duration_h, " h", 2))}</td>
    <td>${escapeHtml(formatNumber(segment.energy_kwh, " kWh"))}</td>
  </tr>`).join("") : '<tr class="placeholder-row"><td colspan="5">等待航速优化结果</td></tr>';
}

const CHECK_LABELS = { time: "航时约束", speed: "航速范围", power: "推进功率", soc: "电量余度" };

function checkValue(value, unit) {
  if (value == null) return "--";
  if (unit === "fraction") return formatNumber(Number(value) * 100, "%");
  return `${formatNumber(value, "", 2)}${unit ? ` ${unit}` : ""}`;
}

function operatorWarning(warning) {
  if (warning.includes("临界线")) return "预计到港电量过低，请重新规划航次并按船舶应急规程处置。";
  if (warning.includes("警告线")) return "预计到港电量不足，不建议执行当前任务；请先补能或缩短航程。";
  if (warning.includes("关注线")) return "预计到港电量偏低，建议出发前补能或缩短航程，并提高电量检查频次。";
  return "检测到能量风险，请根据能量管理建议调整任务。";
}

function renderSafety(checks, warnings = []) {
  const uniqueChecks = [...new Map(checks.map(check => [check.name, check])).values()];
  const items = uniqueChecks.map(check => {
    const label = CHECK_LABELS[check.name] || "约束检查";
    const actual = checkValue(check.actual, check.unit);
    const limit = check.limit == null ? "未设置" : checkValue(check.limit, check.unit);
    return `<div class="safety-item ${check.passed ? "ok" : "error"} state"><span class="safety-label">${escapeHtml(label)}</span><strong class="safety-content">${check.passed ? "通过" : "未通过"} · ${escapeHtml(actual)} / ${escapeHtml(limit)}</strong></div>`;
  });
  warnings.forEach(warning => items.push(`<div class="safety-item warning state"><span class="safety-label">电量提示</span><strong class="safety-content">${escapeHtml(operatorWarning(warning))}</strong></div>`));
  $("#safety-list").innerHTML = items.length ? items.join("") : '<div class="empty-inline">暂无校验结果</div>';
}

function managementAdvice(result) {
  const advice = result.report?.management_advice;
  return Array.isArray(advice) ? advice : [];
}

function renderRecommendations(result) {
  const options = result.adjustment_options || [];
  if (options.length) {
    $("#recommendation-list").innerHTML = options.map(option => `<div class="recommendation info">${escapeHtml(option.label || option)}</div>`).join("");
    return;
  }
  const messages = managementAdvice(result);
  $("#recommendation-list").innerHTML = messages.length
    ? messages.map(message => `<div class="recommendation info">${escapeHtml(message)}</div>`).join("")
    : '<div class="empty-inline">等待能量管理结果</div>';
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
  const explanation = result.status === "ok"
    ? (result.report?.explanation || "航行方案已生成，请查看下方执行建议。")
    : result.status === "infeasible"
      ? "当前任务不可执行，请根据可选调整修改航时、电量或航线后重新计算。"
      : "请补充或修正任务信息后重新计算。";
  $("#model-mode").textContent = llmUsed ? "AI 提示已更新" : "系统提示";
  $("#reply-answer").textContent = explanation;
  $("#reply-panel").hidden = false;
  $("#model-label").textContent = llmUsed ? "DeepSeek 实际调用完成" : "DeepSeek 回退模式";
  $("#model-dot").className = llmUsed ? "status-dot" : "status-dot offline";
}

function optionAction(option) {
  if (option.direction === "accept_late" && option.modification?.max_duration_h) return "采用并重算";
  if (option.direction === "give_up") return "保留结论";
  if (option.direction === "recharge") return "填写实测 SOC";
  if (option.direction === "slow_down") return "修改航时";
  if (option.direction === "adjust_departure") return "修改出发时间";
  if (option.direction === "shorten_route") return "修改航线";
  return "选择调整";
}

function renderAdjustmentOptions(options) {
  if (!options.length) {
    $("#adjustment-options").innerHTML = '<div class="adjustment-option"><span>请直接修改下方任务参数<small>补齐或更正输入后，重新经过五工具链校验。</small></span><b>人工修正</b></div>';
    return;
  }
  $("#adjustment-options").innerHTML = options.map((option, index) => {
    const note = option.requires_input || "采用后仍会重新执行全部约束检查。";
    return `<button class="adjustment-option" type="button" data-option-index="${index}"><span>${escapeHtml(option.label || option)}<small>${escapeHtml(note)}</small></span><b>${escapeHtml(optionAction(option))}</b></button>`;
  }).join("");
}

function clearCorrectionAttention() {
  document.querySelectorAll(".correction-grid .field-attention").forEach(item => item.classList.remove("field-attention"));
  document.querySelectorAll(".adjustment-option.selected").forEach(item => item.classList.remove("selected"));
}

async function applyAdjustmentOption(index) {
  const option = state.lastResult?.adjustment_options?.[index];
  if (!option || state.running) return;
  clearCorrectionAttention();
  const button = $(`[data-option-index="${index}"]`);
  if (button) button.classList.add("selected");
  const focusField = selector => {
    const field = $(selector);
    field.closest("label")?.classList.add("field-attention");
    field.focus();
  };
  if (option.direction === "accept_late" && option.modification?.max_duration_h) {
    $("#correction-duration").value = String(Number(option.modification.max_duration_h));
    $("#correction-feedback").textContent = "已采用工具给出的最短放宽值，正在重新计算。";
    await applyCorrection();
    return;
  }
  if (option.direction === "give_up") {
    $("#correction-feedback").textContent = "已保留本次不可行结论；未修改任务，也未生成航行方案。";
    return;
  }
  if (option.direction === "recharge") {
    focusField("#correction-soc");
    $("#correction-feedback").textContent = "请填写补能完成后的实测初始 SOC；系统不会假定充电站功率或可用性。";
    return;
  }
  if (option.direction === "adjust_departure") {
    focusField("#correction-departure");
    $("#correction-feedback").textContent = "请填写新的出发时间，再点击“确认修改并重新计算”。";
    return;
  }
  if (option.direction === "slow_down") {
    focusField("#correction-duration");
    $("#correction-feedback").textContent = "请放宽最长航时，工具链会重新选择可行候选航速。";
    return;
  }
  if (option.direction === "shorten_route") {
    $("#correction-origin").closest("label")?.classList.add("field-attention");
    focusField("#correction-destination");
    $("#correction-feedback").textContent = "请选择新的起点或终点，再点击“确认修改并重新计算”。";
    return;
  }
  $("#correction-feedback").textContent = option.requires_input || "请修改相关任务参数后重新计算。";
}

function syncCorrectionOnly() {
  try {
    const payload = readCorrectionPayload();
    syncCorrectionToTask(payload);
    $("#correction-feedback").textContent = "已更新左侧任务描述；尚未重新计算。";
  } catch (error) {
    $("#correction-feedback").textContent = error.message;
  }
}

async function applyCorrection() {
  try {
    const payload = readCorrectionPayload();
    syncCorrectionToTask(payload);
    await runInference(payload);
  } catch (error) {
    $("#correction-feedback").textContent = error.message;
  }
}

function showInfeasible(result) {
  const failedRecord = result[result.failed_tool?.toLowerCase()] || result.tspeed || {};
  const failedPayload = failedRecord.payload || {};
  const infeasible = result.status === "infeasible";
  $("#infeasible-title").textContent = infeasible ? "航次不可行" : "任务需要修正";
  $("#infeasible-reason").textContent = failedRecord.reason || failedPayload.reason || (result.questions || []).join("；") || result.final_message || "当前任务未形成可执行方案。";
  const options = result.adjustment_options || [];
  $("#infeasible-suggestion").textContent = options.length
    ? options.map((option, index) => `${index + 1}. ${option.label || option}`).join("\n")
    : (result.questions || []).join("\n") || "请补充所需参数后重新计算。";
  fillCorrectionForm(result.request || {});
  renderAdjustmentOptions(options);
  $("#correction-feedback").textContent = "";
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

function operatorSummary(result, route, summary) {
  if (result.status === "ok") {
    const origin = route[0]?.origin || result.request?.origin || "起点";
    const destination = route.at(-1)?.destination || result.request?.destination || "终点";
    const lines = [
      `${origin} → ${destination}`,
      `计划耗时：${formatNumber(summary.total_duration?.value, " h", 2)}`,
      `预计到达：${formatEta(summary.eta?.value)}`,
      `预计总能耗：${formatNumber(summary.required_energy?.value, " kWh")}`,
      `预计到港电量：${formatNumber((summary.soc_final?.value ?? 0) * 100, "%")}`,
    ];
    managementAdvice(result).forEach(item => lines.push(`建议：${item}`));
    return lines.join("\n");
  }
  const failedRecord = result[result.failed_tool?.toLowerCase()] || result.tspeed || {};
  const reason = failedRecord.reason || failedRecord.payload?.reason || (result.questions || []).join("；") || "当前任务信息不完整。";
  const options = (result.adjustment_options || []).map((item, index) => `${index + 1}. ${item.label || item}`);
  return [`任务未生成航行方案。`, `原因：${reason}`, ...options].join("\n");
}

function renderResult(result) {
  state.lastResult = result;
  const okay = result.status === "ok";
  const infeasible = result.status === "infeasible";
  const route = rawSegments(result);
  const segments = planSegments(result);
  const summary = result.report?.summary || {};
  const checks = result.report?.checks || result.tspeed?.payload?.checks || [];
  const warnings = result.report?.warnings || result.tmanagement?.payload?.warnings || [];

  updateStatus(okay ? "success" : "error", okay ? "方案已生成" : infeasible ? "航次不可行" : "需要补充");
  $("#mission-title").textContent = route.length ? `${route[0].origin} → ${route.at(-1).destination}` : (okay ? "方案已生成" : "任务未完成");
  $("#mission-subtitle").textContent = okay
    ? "方案已生成，请查看推荐航速、能耗与电量安排。"
    : infeasible
      ? "当前任务不满足执行条件，请选择下方调整方案。"
      : "请补充或修正任务信息后重新计算。";
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

  state.finalAnswer = operatorSummary(result, route, summary);
  $("#final-answer").textContent = state.finalAnswer;
  $("#final-response").hidden = !state.finalAnswer;
  refreshIcons();
}

async function runInference(payloadOverride = null) {
  const message = $("#mission-input").value.trim();
  if ((!message && !payloadOverride) || state.running) {
    if (!message && !payloadOverride) $("#mission-input").focus();
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
      body: JSON.stringify(payloadOverride || { task_text: message }),
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
  $("#run-button").addEventListener("click", () => runInference());
  $("#mission-input").addEventListener("keydown", event => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") runInference();
  });
  $("#clear-button").addEventListener("click", () => {
    $("#mission-input").value = "";
    state.lastResult = null;
    resetResult();
    updateStatus("idle", "待命");
    $("#mission-title").textContent = "等待航行任务";
    $("#mission-subtitle").textContent = "输入自然语言需求，或使用左侧快捷航线开始。";
  });
  document.querySelectorAll("[data-sample]").forEach(button => button.addEventListener("click", () => {
    $("#mission-input").value = SAMPLES[button.dataset.sample];
  }));
  $("#adjustment-options").addEventListener("click", event => {
    const button = event.target.closest("[data-option-index]");
    if (button) applyAdjustmentOption(Number(button.dataset.optionIndex));
  });
  $("#sync-correction").addEventListener("click", syncCorrectionOnly);
  $("#apply-correction").addEventListener("click", applyCorrection);
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
