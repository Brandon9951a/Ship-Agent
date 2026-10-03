const ROUTES = {
  forward: ["平顶山港", "军李船闸", "马湾船闸", "漯河船闸", "漯河港", "大路李船闸", "葫芦湾船闸", "周口船闸", "周口港"],
  reverse: ["周口港", "周口船闸", "葫芦湾船闸", "大路李船闸", "漯河港", "漯河船闸", "马湾船闸", "军李船闸", "平顶山港"],
};

const state = {
  trace: [], finalAnswer: "", running: false, lastResult: null,
  voice: null, recording: false, speechAvailable: false, resultStale: false,
  inputRevision: 0, quickDirty: false, requestSequence: 0, activeRequest: null,
};

const OPTION_LABELS = {
  accept_late: "接受延时",
  adjust_departure: "提前出发",
  recharge: "补能后出发",
  recharge_and_extend: "补能并延长航时",
  shorten_route: "缩短航线",
  give_up: "放弃任务",
};
const ACTIVE_THREAD_KEY = "ship-agent.active-thread";
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
  return text.length >= 16 ? text.slice(0, 16) : text;
}

function localDateTime(daysAhead = 1) {
  const date = new Date();
  date.setDate(date.getDate() + daysAhead);
  date.setHours(9, 0, 0, 0);
  const pad = value => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T09:00`;
}

function sampleTask(kind) {
  const departure = localDateTime().replace("T", " ");
  const timeLimit = kind === "time" ? 1 : 6;
  const soc = kind === "soc" ? 31 : 85;
  return `从平顶山港到军李船闸，${departure}出发，SOC${soc}%，半载，${timeLimit}小时内到达`;
}

function updateStatus(kind, text) {
  const target = $("#decision-status");
  target.className = `decision-status ${kind}`;
  target.innerHTML = `<span></span>${escapeHtml(text)}`;
}

function populateSelect(select, values, value, emptyLabel = "") {
  const emptyOption = emptyLabel ? `<option value="">${escapeHtml(emptyLabel)}</option>` : "";
  select.innerHTML = emptyOption + values.map(item => `<option value="${escapeHtml(item)}">${escapeHtml(item)}</option>`).join("");
  select.value = value && values.includes(value) ? value : "";
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
  populateSelect($("#correction-origin"), ALL_ROUTE_NODES, request.origin || "", "请选择起点");
  populateSelect($("#correction-destination"), ALL_ROUTE_NODES, request.destination || "", "请选择终点");
  $("#correction-departure").value = localDateTimeValue(request.departure_at);
  $("#correction-duration").value = request.max_duration_h ?? "";
  $("#correction-soc").value = request.soc_initial == null ? "" : Number(request.soc_initial) * 100;
  $("#correction-load").value = request.load_state || "";
}

function readCorrectionPayload() {
  const origin = $("#correction-origin").value;
  const destination = $("#correction-destination").value;
  const departure = $("#correction-departure").value;
  if (!origin || !destination) throw new Error("请选择起点和终点。");
  if (!departure) throw new Error("请填写出发时间。");
  if (!$("#correction-duration").value) throw new Error("请填写最长航时。");
  if (!$("#correction-soc").value) throw new Error("请填写初始 SOC。");
  if (!$("#correction-load").value) throw new Error("请填写载况。");
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
  state.quickDirty = false;
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

function updateVesselView(status, { segments = [], summary = {}, message = "" } = {}) {
  const detail = { status, segments, summary, message };
  window.ship3dPendingUpdate = detail;
  document.dispatchEvent(new CustomEvent("ship3d:update", {
    detail,
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
  if (warning.includes("临界线")) return "软件仿真估算触及电量临界线；实船 SOC 尚未核实，不构成应急判断。";
  if (warning.includes("警告线")) return "软件仿真估算低于电量警告线；实船电量与可航条件尚未核实。";
  if (warning.includes("关注线")) return "软件仿真估算接近电量关注线；实船电量和补能条件待核实。";
  return "软件仿真提示存在能量风险；实船状态与运行约束尚未核实。";
}

function handleTableScrollKeydown(event) {
  const container = event.currentTarget;
  const page = Math.max(container.clientWidth * 0.7, 120);
  if (event.key === "ArrowLeft") container.scrollBy({ left: -48, behavior: "smooth" });
  else if (event.key === "ArrowRight") container.scrollBy({ left: 48, behavior: "smooth" });
  else if (event.key === "Home") container.scrollTo({ left: 0, behavior: "smooth" });
  else if (event.key === "End") container.scrollTo({ left: container.scrollWidth, behavior: "smooth" });
  else if (event.key === "PageUp") container.scrollBy({ left: -page, behavior: "smooth" });
  else if (event.key === "PageDown") container.scrollBy({ left: page, behavior: "smooth" });
  else return;
  event.preventDefault();
}

function renderSafety(checks, warnings = []) {
  const uniqueChecks = [...new Map(checks.map(check => [check.name, check])).values()];
  const items = uniqueChecks.map(check => {
    const label = CHECK_LABELS[check.name] || "约束检查";
    const actual = checkValue(check.actual, check.unit);
    const limit = check.limit == null ? "未设置" : checkValue(check.limit, check.unit);
    const stateLabel = check.passed === false ? "未通过" : check.limit == null || check.passed == null ? "待核验" : "通过";
    const stateClass = check.passed === false ? "error" : check.limit == null || check.passed == null ? "warn" : "ok";
    return `<div class="safety-item ${stateClass} state"><span class="safety-label">${escapeHtml(label)}</span><strong class="safety-content">${stateLabel} · ${escapeHtml(actual)} / ${escapeHtml(limit)}</strong></div>`;
  });
  warnings.forEach(warning => items.push(`<div class="safety-item warning state"><span class="safety-label">电量提示</span><strong class="safety-content">${escapeHtml(operatorWarning(warning))}</strong></div>`));
  $("#safety-list").innerHTML = items.length ? items.join("") : '<div class="empty-inline">暂无校验结果</div>';
}

function managementAdvice(result) {
  const advice = result.report?.management_advice;
  return Array.isArray(advice) ? advice : [];
}

function renderRecommendations(result) {
  const options = result.status === "awaiting_choice" ? (result.adjustment_options || []) : [];
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
  $("#trace-list").innerHTML = state.trace.map((item, index) => {
    const iteration = Number(item.iteration || 0);
    const round = iteration ? `第${iteration}次调整` : "初始计算";
    const option = item.option_id ? ` · ${OPTION_LABELS[item.option_id] || item.option_id}` : "";
    const label = item.node === "prepare_adjustment" && item.status === "awaiting_choice"
      ? "等待用户选择"
      : item.node === "await_choice" ? "用户已选择" : (item.node || "流程");
    return `<li class="trace-item"><span class="trace-index">${index + 1}</span><div><strong>${escapeHtml(label)}</strong><p>${escapeHtml(`${round} · ${item.status || "完成"}${option}`)}</p></div></li>`;
  }).join("");
  $("#trace-meta").textContent = state.trace.length ? `${state.trace.length} 个步骤` : "等待任务";
}

function renderModel(result) {
  const reportMode = result.report?.explanation_mode;
  const understandingMode = result.task_understanding?.mode;
  const llmUsed = reportMode === "llm_qualitative" || understandingMode === "llm_qualitative";
  const explanation = result.status === "ok"
    ? (result.report?.explanation || "仿真方案已生成。请结合现场条件核对后再作判断。")
    : (result.status === "infeasible" || result.status === "awaiting_choice")
      ? "当前输入条件下未生成仿真航速方案；下方展示系统内备选计算结果。"
      : "请补充或修正任务信息后重新计算。";
  $("#model-mode").textContent = llmUsed ? "AI 提示已更新" : "系统提示";
  $("#reply-answer").textContent = explanation;
  $("#reply-panel").hidden = false;
  $("#model-label").textContent = llmUsed ? "DeepSeek 实际调用完成" : "DeepSeek 回退模式";
  $("#model-dot").className = llmUsed ? "status-dot" : "status-dot offline";
}

function optionAction(option) {
  if (option.direction === "give_up") return "保留结论";
  if (option.requires_input) return "需填写实测值";
  if (option.verified && option.modification) return "采用并重算";
  return "不可直接采用";
}

function renderAdjustmentOptions(options) {
  if (!options.length) {
    $("#adjustment-options").innerHTML = '<div class="adjustment-option"><span>请直接修改下方任务参数<small>补齐或更正输入后，重新经过五工具链校验。</small></span><b>人工修正</b></div>';
    return;
  }
  $("#adjustment-options").innerHTML = options.map((option, index) => {
    const note = option.requires_input
      ? "须先完成现场补能并录入实测 SOC，再通过下方表单重新计算；系统不会代替确认补能完成。"
      : option.direction === "give_up"
        ? "保留本次不可行结论，不修改任务。"
        : "已通过 Tdata→Tseg→Tenergy→Tspeed→Tmanagement 完整复算。";
    const disabled = option.requires_input || (option.direction !== "give_up" && (!option.verified || !option.modification));
    return `<button class="adjustment-option" type="button" data-option-index="${index}"${disabled ? " disabled" : ""}><span>${escapeHtml(option.label || option)}<small>${escapeHtml(note)}</small></span><b>${escapeHtml(optionAction(option))}</b></button>`;
  }).join("");
}

function clearCorrectionAttention() {
  document.querySelectorAll(".correction-grid .field-attention").forEach(item => item.classList.remove("field-attention"));
  document.querySelectorAll(".adjustment-option.selected").forEach(item => item.classList.remove("selected"));
}

async function applyAdjustmentOption(index) {
  const option = state.lastResult?.adjustment_options?.[index];
  if (!option || state.running || state.resultStale || option.requires_input) return;
  clearCorrectionAttention();
  const button = $(`[data-option-index="${index}"]`);
  if (button) button.classList.add("selected");
  if (option.direction !== "give_up" && (!option.verified || !option.modification)) {
    $("#correction-feedback").textContent = "该选项没有通过完整工具链验证，不能采用。";
    return;
  }
  const decision = state.lastResult?.decision || {};
  if (!state.lastResult?.thread_id || !decision.decision_id || !option.option_id) {
    $("#correction-feedback").textContent = "当前任务没有可恢复的决策状态，请重新发起任务。";
    return;
  }
  const threadId = state.lastResult.thread_id;
  const requestId = ++state.requestSequence;
  const submittedRevision = state.inputRevision;
  const controller = typeof AbortController === "function"
    ? new AbortController()
    : { signal: undefined, abort() {} };
  state.activeRequest = { id: requestId, controller };
  const isCurrentRequest = () => state.activeRequest?.id === requestId;
  state.running = true;
  $("#run-button").disabled = true;
  $("#voice-button").disabled = true;
  document.querySelectorAll(".adjustment-option").forEach(item => { item.disabled = true; });
  if (button) button.querySelector("b").textContent = "正在恢复并重算";
  $("#correction-feedback").classList.remove("error");
  $("#correction-feedback").textContent = option.direction === "give_up"
    ? "正在保存船员决定并结束本次任务。"
    : "正在从已保存的 LangGraph 状态恢复并重新执行五工具链。";
  try {
    const response = await fetch("/api/resume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        thread_id: threadId,
        decision_id: decision.decision_id,
        option_id: option.option_id,
      }),
      signal: controller.signal,
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "恢复失败");
    if (!isCurrentRequest()) return;
    if (submittedRevision !== state.inputRevision) {
      discardStaleResponse();
      return;
    }
    // Mirror the server-owned, verified modification into every task input.
    if (result.request) syncCorrectionToTask(result.request);
    renderResult(result);
  } catch (error) {
    if (!isCurrentRequest() || error.name === "AbortError") return;
    $("#correction-feedback").classList.add("error");
    $("#correction-feedback").textContent = error.message === "workflow_not_awaiting_choice"
      ? "该决策已经处理或已过期，请刷新任务状态。"
      : `恢复失败：${error.message}`;
    renderAdjustmentOptions(state.lastResult?.adjustment_options || []);
  } finally {
    if (isCurrentRequest()) {
      state.activeRequest = null;
      state.running = false;
      $("#run-button").disabled = false;
      $("#voice-button").disabled = !state.speechAvailable;
    }
  }
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
  const awaiting = result.status === "awaiting_choice";
  $("#infeasible-title").textContent = awaiting ? "当前输入条件不满足，等待选择" : infeasible ? "当前输入条件不可行" : "任务需要补充或修正";
  $("#infeasible-reason").textContent = failedRecord.reason || failedPayload.reason || (result.questions || []).join("；") || result.final_message || "当前任务未形成仿真方案。";
  const options = awaiting ? (result.adjustment_options || []) : [];
  $("#infeasible-suggestion").textContent = options.length
    ? options.map((option, index) => `${index + 1}. ${option.label || option}`).join("\n")
    : result.replan_limit_reached
      ? "已达到两轮调整上限，请重新发起任务。"
      : (result.questions || []).join("\n") || result.final_message || "请补充所需参数后重新计算。";
  fillCorrectionForm(result.request || {});
  renderAdjustmentOptions(options);
  const round = result.decision?.round || Math.min((result.replan_count || 0) + 1, result.max_replans || 2);
  $("#correction-title").textContent = awaiting
    ? `等待选择 · 第 ${round}/${result.max_replans || 2} 轮`
    : result.replan_limit_reached ? "已达到调整上限" : "选用已复算方案或补充任务字段";
  $("#correction-lock").innerHTML = awaiting
    ? '<i data-lucide="database-zap"></i> 演示任务状态已保存'
    : '<i data-lucide="shield-check"></i> 模型规划线固定，非实船安全核验';
  $("#correction-note").textContent = result.replan_limit_reached
    ? "已达到两轮调整上限，请重新发起任务并修改初始条件。"
    : awaiting
      ? "已复算选项仅代表本系统内计算完成。补能选项须先在现场充电，再录入实测 SOC 重新计算；也可手动改写已知任务字段。"
      : "请只填写本次任务已知的信息；缺失字段保持空白。提交后将按演示模型重新计算。";
  $(".correction-grid").hidden = false;
  $(".correction-actions").hidden = false;
  $("#correction-form-heading").hidden = false;
  $("#correction-feedback").classList.remove("error");
  $("#correction-feedback").textContent = awaiting ? "任务状态已保存，可关闭页面后继续。" : "";
  $("#infeasible-alert").hidden = false;
  $("#infeasible-alert").setAttribute("aria-hidden", "false");
}

function resetResult() {
  state.lastResult = null;
  state.resultStale = false;
  $("#result-stale").hidden = true;
  $("#result-scope-banner").hidden = true;
  $("#evidence-panel").hidden = true;
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
  $("#infeasible-alert").setAttribute("aria-hidden", "true");
  $("#final-response").hidden = true;
  refreshIcons();
}

function operatorSummary(result, route, summary) {
  if (result.status === "ok") {
    const origin = route[0]?.origin || result.request?.origin || "起点";
    const destination = route.at(-1)?.destination || result.request?.destination || "终点";
    const lines = [
      `软件仿真：${origin} → ${destination}`,
      `计划耗时：${formatNumber(summary.total_duration?.value, " h", 2)}`,
      `预计到达：${formatEta(summary.eta?.value)}`,
      `预计总能耗：${formatNumber(summary.required_energy?.value, " kWh")}`,
      `预计到港电量：${formatNumber((summary.soc_final?.value ?? 0) * 100, "%")}`,
    ];
    managementAdvice(result).forEach(item => lines.push(`提示：${item}`));
    return lines.join("\n");
  }
  const failedRecord = result[result.failed_tool?.toLowerCase()] || result.tspeed || {};
  const reason = failedRecord.reason || failedRecord.payload?.reason || (result.questions || []).join("；") || "当前任务信息不完整。";
  const options = (result.adjustment_options || []).map((item, index) => `${index + 1}. ${item.label || item}`);
  return [`任务未生成航行方案。`, `原因：${reason}`, ...options].join("\n");
}

function renderEvidence(result) {
  const report = result.report || {};
  const evidenceSourceLabels = { capacity: "电池容量", power_limit: "功率边界", soc_min: "SOC 规划下限" };
  const sourceLine = (label, source) => source
    ? `<li><strong>${escapeHtml(label)}</strong>：${escapeHtml(source.source_id || "未知来源")}${source.locator ? ` · ${escapeHtml(source.locator)}` : ""}${source.confirmed ? "（资料已核对）" : "（演示假设/待确认）"}</li>`
    : "";
  const sources = report.sources || {};
  const distances = (report.segments || []).map(segment => {
    const source = segment.distance_source;
    const location = source?.locator ? ` · ${source.locator}` : "";
    return `<li>${escapeHtml(segment.origin)} → ${escapeHtml(segment.destination)}：${escapeHtml(source?.source_id || "未知距离来源")}${escapeHtml(location)}</li>`;
  }).join("");
  const modelIds = [...new Set((report.segments || []).map(segment => segment.model_id).filter(Boolean))];
  const assumptions = report.assumptions || [];
  $("#evidence-content").innerHTML = `
    <div class="evidence-grid">
      <section><h4>模型与范围</h4><p>模型：${escapeHtml(modelIds.join("、") || "未知")}</p><p>当前结果为 synthetic_demo 软件仿真；推进能耗系数由调研工况单点锚定，历史数据只用于覆盖范围检查，未用于实船标定。</p></section>
      <section><h4>采用参数来源</h4><ul>${Object.entries(evidenceSourceLabels).map(([key, label]) => sourceLine(label, sources[key])).join("")}</ul></section>
      <section><h4>航段距离来源</h4><ul>${distances || "<li>未提供航段来源。</li>"}</ul></section>
      <section><h4>模型假设</h4><ul>${assumptions.length ? assumptions.map(item => `<li>${escapeHtml(item)}</li>`).join("") : "<li>报告未提供额外假设。</li>"}</ul></section>
    </div>
    <p class="evidence-limit">未核实的逐段真实限速、船闸等待、充电可用性与实船 BMS 参数不构成已通过的约束。</p>`;
  $("#evidence-panel").hidden = false;
}

function markResultStale() {
  state.inputRevision += 1;
  if (state.running) {
    state.resultStale = true;
    $("#result-stale").hidden = false;
    $("#result-stale").textContent = "输入已修改；当前计算结果不会覆盖新任务，请重新发送。";
    return;
  }
  if (!state.lastResult) return;
  state.resultStale = true;
  $("#result-stale").hidden = false;
  $("#result-stale").textContent = state.lastResult.status === "awaiting_choice"
    ? "输入已修改；下方待确认选项仍属于上一次任务。请重新计算后再选择。"
    : "输入已修改；当前结果对应之前的任务，请重新计算以更新方案。";
  document.querySelectorAll(".adjustment-option[data-option-index]").forEach(button => { button.disabled = true; });
}

function discardStaleResponse() {
  state.resultStale = true;
  updateStatus("idle", "输入已修改");
  $("#mission-title").textContent = "当前计算结果已忽略";
  $("#mission-subtitle").textContent = "任务条件在计算期间发生变化，请重新发送当前任务。";
  $("#result-stale").hidden = false;
  $("#result-stale").textContent = "已忽略旧任务返回的结果；当前输入尚未计算。";
  updateVesselView("idle", { message: "输入已修改，旧结果未应用" });
}

function initializeTaskDefaults() {
  $("#departure-input").value = localDateTime();
  $("#start-select").value = "平顶山港";
  $("#end-select").value = "军李船闸";
  writeQuickTask({ markChanged: false });
}

function renderResult(result) {
  state.lastResult = result;
  state.resultStale = false;
  $("#result-stale").hidden = true;
  const okay = result.status === "ok";
  const infeasible = result.status === "infeasible";
  const awaiting = result.status === "awaiting_choice";
  if (okay) {
    // Clear the previous decision panel before rendering any new result data.
    $("#infeasible-alert").hidden = true;
    $("#infeasible-alert").setAttribute("aria-hidden", "true");
    $("#correction-feedback").classList.remove("error");
    $("#correction-feedback").textContent = "";
  }
  const route = rawSegments(result);
  const segments = planSegments(result);
  const summary = result.report?.summary || {};
  const checks = result.report?.checks || result.tspeed?.payload?.checks || [];
  const warnings = result.report?.warnings || result.tmanagement?.payload?.warnings || [];

  updateStatus(okay ? "success" : awaiting ? "running" : "error", okay ? "仿真方案已生成" : awaiting ? "等待确认" : infeasible ? "航次不可行" : "需要补充");
  $("#mission-title").textContent = route.length ? `${route[0].origin} → ${route.at(-1).destination}` : (okay ? "仿真方案已生成" : "任务未完成");
  $("#mission-subtitle").textContent = okay
    ? "以下为软件仿真结果，仅供演示和方法验证，不构成实船航行指令。"
    : awaiting
      ? "当前任务不满足执行条件，请选择下方调整方案。"
      : infeasible
        ? "本次任务保留不可行结论，未生成航行方案。"
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
    updateVesselView("success", { segments, summary });
    renderEvidence(result);
    const departure = result.request?.departure_at ? new Date(result.request.departure_at) : null;
    const isPast = departure && departure.getTime() < Date.now();
    $("#result-scope-banner").textContent = isPast
      ? "历史时点仿真：出发时间已过。该结果用于软件演示，不代表历史实船记录或当前航行建议。"
      : "软件仿真结果：模型参数和约束采用演示口径，不代表实船安全或运营批准。";
    $("#result-scope-banner").hidden = false;
  } else {
    $("#evidence-panel").hidden = true;
    $("#result-scope-banner").hidden = true;
    renderEnergyChart([]);
    renderTable([]);
    renderSafety(checks, warnings);
    showInfeasible(result);
    updateVesselView((infeasible || awaiting) ? "infeasible" : "incomplete", {
      message: (infeasible || awaiting) ? "当前航次不可行，回放已停止" : "任务信息需要补充，回放已停止",
    });
  }

  if (awaiting && result.thread_id) {
    localStorage.setItem(ACTIVE_THREAD_KEY, result.thread_id);
  } else if (result.thread_id === localStorage.getItem(ACTIVE_THREAD_KEY)) {
    localStorage.removeItem(ACTIVE_THREAD_KEY);
  }

  state.finalAnswer = operatorSummary(result, route, summary);
  $("#final-answer").textContent = state.finalAnswer;
  $("#final-response").hidden = !state.finalAnswer;
  refreshIcons();
}

function pcm16Base64(input, sourceRate) {
  const ratio = sourceRate / 16000;
  const length = Math.max(1, Math.round(input.length / ratio));
  const output = new Int16Array(length);
  for (let index = 0; index < length; index += 1) {
    const start = Math.floor(index * ratio);
    const end = Math.min(input.length, Math.floor((index + 1) * ratio));
    let sum = 0;
    for (let cursor = start; cursor < Math.max(start + 1, end); cursor += 1) {
      sum += input[cursor] || 0;
    }
    const sample = Math.max(-1, Math.min(1, sum / Math.max(1, end - start)));
    output[index] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
  }
  const bytes = new Uint8Array(output.buffer);
  let binary = "";
  for (let index = 0; index < bytes.length; index += 1) {
    binary += String.fromCharCode(bytes[index]);
  }
  return btoa(binary);
}

async function voiceRequest(path, payload = {}) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || "语音服务请求失败");
  return body;
}

function updateVoiceText(voice, text) {
  voice.transcript = text || voice.transcript || "";
  const mission = $("#mission-input");
  mission.value = `${voice.prefix}${voice.transcript}${voice.suffix}`;
  mission.dispatchEvent(new Event("input", { bubbles: true }));
}

function setVoiceButton(recording) {
  const button = $("#voice-button");
  button.classList.toggle("recording", recording);
  button.setAttribute("aria-pressed", String(recording));
  button.setAttribute("aria-label", recording ? "结束科大讯飞语音输入" : "开始科大讯飞语音输入");
  button.title = recording ? "结束语音输入" : "开始科大讯飞语音输入";
}

async function startXfyunVoice() {
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!state.speechAvailable) throw new Error("科大讯飞语音服务尚未配置");
  if (!window.isSecureContext && location.hostname !== "127.0.0.1" && location.hostname !== "localhost") {
    throw new Error("麦克风仅能在 HTTPS 或本机地址使用");
  }
  if (!navigator.mediaDevices?.getUserMedia || !AudioContextClass) {
    throw new Error("当前浏览器不支持麦克风录音");
  }
  $("#voice-copy").textContent = "正在连接科大讯飞";
  const microphone = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
  });
  let started;
  try {
    started = await voiceRequest("/api/voice/start");
  } catch (error) {
    microphone.getTracks().forEach(track => track.stop());
    throw error;
  }
  const context = new AudioContextClass();
  const source = context.createMediaStreamSource(microphone);
  const processor = context.createScriptProcessor(8192, 1, 1);
  const mute = context.createGain();
  mute.gain.value = 0;
  await context.resume();
  const mission = $("#mission-input");
  const useSelection = document.activeElement === mission;
  const selectionStart = useSelection ? mission.selectionStart : 0;
  const selectionEnd = useSelection ? mission.selectionEnd : mission.value.length;
  const voice = {
    sessionId: started.session_id,
    microphone, context, source, processor, mute,
    prefix: mission.value.slice(0, selectionStart),
    suffix: mission.value.slice(selectionEnd),
    transcript: "",
    queue: Promise.resolve(),
    capturing: true,
    failed: false,
    stopping: false,
  };
  source.connect(processor);
  processor.connect(mute);
  mute.connect(context.destination);
  processor.onaudioprocess = event => {
    if (!voice.capturing) return;
    const audio = pcm16Base64(event.inputBuffer.getChannelData(0), context.sampleRate);
    voice.queue = voice.queue.then(async () => {
      const result = await voiceRequest("/api/voice/chunk", {
        session_id: voice.sessionId,
        audio,
      });
      updateVoiceText(voice, result.text);
      $("#voice-copy").textContent = result.text
        ? `实时识别：${result.text.slice(-20)}`
        : "正在听写";
    }).catch(error => {
      voice.failed = true;
      voice.capturing = false;
      $("#voice-copy").textContent = `识别失败：${error.message}`;
    });
  };
  state.voice = voice;
  state.recording = true;
  setVoiceButton(true);
  $("#voice-copy").textContent = "正在听写，再次点击结束";
}

async function stopXfyunVoice() {
  const voice = state.voice;
  if (!voice || voice.stopping) return;
  voice.stopping = true;
  voice.capturing = false;
  state.recording = false;
  setVoiceButton(false);
  voice.processor.onaudioprocess = null;
  voice.processor.disconnect();
  voice.source.disconnect();
  voice.mute.disconnect();
  voice.microphone.getTracks().forEach(track => track.stop());
  await voice.queue;
  try {
    const result = await voiceRequest("/api/voice/finish", { session_id: voice.sessionId });
    updateVoiceText(voice, result.text);
    $("#voice-copy").textContent = result.text
      ? (voice.failed ? "识别完成，末段上传异常" : "科大讯飞识别完成")
      : "未识别到有效语音";
  } finally {
    if (voice.context.state !== "closed") await voice.context.close();
    if (state.voice === voice) state.voice = null;
    state.recording = false;
    setVoiceButton(false);
  }
}

function attachSpeechInput() {
  $("#voice-button").addEventListener("click", async () => {
    try {
      if (state.voice?.stopping) return;
      if (state.recording) await stopXfyunVoice();
      else await startXfyunVoice();
    } catch (error) {
      state.recording = false;
      setVoiceButton(false);
      $("#voice-copy").textContent = `语音识别失败：${error.message}`;
    }
  });
}

async function runInference(payloadOverride = null) {
  if (state.recording) {
    try {
      await stopXfyunVoice();
    } catch (error) {
      $("#voice-copy").textContent = `语音结束异常：${error.message}`;
    }
  }
  if (!payloadOverride && state.quickDirty && !writeQuickTask({ markChanged: false })) return;
  const message = $("#mission-input").value.trim();
  if ((!message && !payloadOverride) || state.running) {
    if (!message && !payloadOverride) $("#mission-input").focus();
    return;
  }
  const requestId = ++state.requestSequence;
  const submittedRevision = state.inputRevision;
  const controller = typeof AbortController === "function"
    ? new AbortController()
    : { signal: undefined, abort() {} };
  state.activeRequest = { id: requestId, controller };
  const isCurrentRequest = () => state.activeRequest?.id === requestId;
  state.running = true;
  $("#run-button").disabled = true;
  $("#voice-button").disabled = true;
  updateStatus("running", "推理中");
  updateVesselView("running", { message: "五工具链正在计算航行方案" });
  resetResult();
  try {
    const response = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payloadOverride || { task_text: message }),
      signal: controller.signal,
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "请求失败");
    if (!isCurrentRequest()) return;
    if (submittedRevision !== state.inputRevision) {
      discardStaleResponse();
      return;
    }
    renderResult(result);
  } catch (error) {
    if (!isCurrentRequest() || error.name === "AbortError") return;
    updateStatus("error", "计算失败");
    $("#mission-title").textContent = "任务执行失败";
    $("#mission-subtitle").textContent = error.message;
    $("#model-mode").textContent = "调用失败";
    $("#reply-answer").textContent = "工具链或模型调用失败，请检查本地服务状态后重试。";
    $("#reply-panel").hidden = false;
    updateVesselView("failed", { message: "计算失败，回放已停止" });
  } finally {
    if (isCurrentRequest()) {
      state.activeRequest = null;
      state.running = false;
      $("#run-button").disabled = false;
      $("#voice-button").disabled = !state.speechAvailable;
    }
  }
}

function quickTaskError(message, selector) {
  const error = new Error(message);
  error.selector = selector;
  throw error;
}

function writeQuickTask({ silent = false, markChanged = true } = {}) {
  const origin = $("#start-select").value;
  const destination = $("#end-select").value;
  const departureValue = $("#departure-input").value;
  const hoursValue = $("#time-input").value;
  const socValue = $("#adv-soc").value;
  const load = $("#adv-load").value;
  try {
    if (!origin) quickTaskError("请选择起点。", "#start-select");
    if (!destination) quickTaskError("请选择终点。", "#end-select");
    if (origin === destination) quickTaskError("起点和终点必须不同。", "#end-select");
    if (!departureValue) quickTaskError("请填写出发时间。", "#departure-input");
    if (hoursValue === "") quickTaskError("请填写限时。", "#time-input");
    if (socValue === "") quickTaskError("请填写初始 SOC。", "#adv-soc");
    if (!load) quickTaskError("请选择装载状态。", "#adv-load");
    const hours = Number(hoursValue);
    const soc = Number(socValue);
    if (!Number.isFinite(hours) || hours < 0.5 || hours > 72) {
      quickTaskError("限时必须在 0.5 到 72 小时之间。", "#time-input");
    }
    if (!Number.isFinite(soc) || soc < 0 || soc > 100) {
      quickTaskError("初始 SOC 必须在 0% 到 100% 之间。", "#adv-soc");
    }
    const departure = departureValue.replace("T", " ");
    $("#time-input").value = hours;
    $("#adv-time").value = hours;
    $("#mission-input").value = `从${origin}到${destination}，${departure}出发，SOC${soc}%，${load}，${hours}小时内到达`;
    state.quickDirty = false;
    if (markChanged) markResultStale();
    return true;
  } catch (error) {
    state.quickDirty = true;
    if (!silent) {
      $("#mission-subtitle").textContent = error.message;
      $(error.selector)?.focus();
    }
    return false;
  }
}

function markQuickInputChanged() {
  state.quickDirty = true;
  writeQuickTask({ silent: true, markChanged: false });
  markResultStale();
}

function cancelActiveRequest() {
  state.activeRequest?.controller.abort();
  state.activeRequest = null;
  state.running = false;
  $("#run-button").disabled = false;
  $("#voice-button").disabled = !state.speechAvailable;
}

async function loadHealth() {
  try {
    const health = await (await fetch("/healthz")).json();
    const enabled = health.llm_mode === "enabled";
    const speechEnabled = health.speech_mode === "iflytek_iat";
    const release = health.release || {};
    const version = release.app_version ? `v${release.app_version}` : "版本未知";
    const profile = release.parameter_profile || "参数档案未知";
    const parameterDate = release.parameter_approved_at || "日期未知";
    $("#release-meta").textContent = `正式网站 · ${version} · 参数 ${parameterDate}`;
    $("#release-meta").title = [
      `基线：${release.baseline_id || "未知"}`,
      `参数档案：${profile}`,
      `有效容量：${formatNumber(release.effective_capacity_kwh, " kWh")}`,
      `SOC 规划下限：${formatNumber(Number(release.soc_planning_min) * 100, "%")}`,
    ].join("；");
    $("#model-label").textContent = enabled ? "DeepSeek API 已启用" : "DeepSeek 模板回退";
    $("#model-dot").className = enabled ? "status-dot" : "status-dot offline";
    state.speechAvailable = speechEnabled;
    $("#voice-button").disabled = !speechEnabled;
    $("#voice-copy").textContent = speechEnabled ? "点击麦克风语音输入" : "语音服务待配置";
  } catch (_error) {
    $("#release-meta").textContent = "正式网站 · 版本信息暂不可用";
    $("#model-label").textContent = "本地决策引擎未连接";
    $("#model-dot").className = "status-dot offline";
    state.speechAvailable = false;
    $("#voice-button").disabled = true;
    $("#voice-copy").textContent = "语音服务未连接";
  }
}

async function restorePendingRun() {
  const threadId = localStorage.getItem(ACTIVE_THREAD_KEY);
  if (!threadId) return;
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(threadId)}`);
    const result = await response.json();
    if (!response.ok) {
      if (response.status === 400 || response.status === 404) {
        localStorage.removeItem(ACTIVE_THREAD_KEY);
        $("#mission-subtitle").textContent = "上次任务状态已失效，请重新发起任务。";
      } else {
        $("#mission-subtitle").textContent = "暂时无法恢复上次任务，恢复标识已保留；请稍后刷新重试。";
      }
      return;
    }
    if (result.status === "awaiting_choice") {
      if (result.request) syncCorrectionToTask(result.request);
      renderResult(result);
      $("#correction-feedback").textContent = "已恢复上次等待确认的任务。";
    } else {
      localStorage.removeItem(ACTIVE_THREAD_KEY);
    }
  } catch (_error) {
    $("#mission-subtitle").textContent = "网络暂时不可用，恢复标识已保留；请稍后刷新重试。";
  }
}

function bindControls() {
  $("#segment-table-scroll").addEventListener("keydown", handleTableScrollKeydown);
  $("#route-select").addEventListener("change", () => { syncRouteNodes(); markQuickInputChanged(); });
  $("#mission-input").addEventListener("input", () => { state.quickDirty = false; markResultStale(); });
  ["#start-select", "#end-select", "#departure-input", "#adv-soc", "#adv-load"].forEach(selector => {
    $(selector).addEventListener("input", markQuickInputChanged);
    $(selector).addEventListener("change", markQuickInputChanged);
  });
  ["#correction-origin", "#correction-destination", "#correction-departure", "#correction-duration", "#correction-soc", "#correction-load"].forEach(selector => {
    $(selector).addEventListener("input", markResultStale);
    $(selector).addEventListener("change", markResultStale);
  });
  $("#time-input").addEventListener("input", () => { $("#adv-time").value = $("#time-input").value; markQuickInputChanged(); });
  $("#adv-time").addEventListener("input", () => { $("#time-input").value = $("#adv-time").value; markQuickInputChanged(); });
  $("#time-minus").addEventListener("click", () => {
    $("#time-input").value = Math.max(0.5, Number($("#time-input").value || 6) - 0.5);
    $("#adv-time").value = $("#time-input").value;
    markQuickInputChanged();
  });
  $("#time-plus").addEventListener("click", () => {
    $("#time-input").value = Math.min(72, Number($("#time-input").value || 6) + 0.5);
    $("#adv-time").value = $("#time-input").value;
    markQuickInputChanged();
  });
  $("#quick-fill").addEventListener("click", writeQuickTask);
  $("#run-button").addEventListener("click", () => runInference());
  attachSpeechInput();
  $("#mission-input").addEventListener("keydown", event => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") runInference();
  });
  $("#clear-button").addEventListener("click", async () => {
    cancelActiveRequest();
    if (state.recording) {
      try {
        await stopXfyunVoice();
      } catch (error) {
        $("#voice-copy").textContent = `语音结束异常：${error.message}`;
      }
    }
    $("#mission-input").value = "";
    state.inputRevision += 1;
    state.quickDirty = false;
    state.lastResult = null;
    localStorage.removeItem(ACTIVE_THREAD_KEY);
    resetResult();
    updateStatus("idle", "待命");
    $("#mission-title").textContent = "等待航行任务";
    $("#mission-subtitle").textContent = "输入自然语言需求，或使用左侧快捷航线开始。";
    updateVesselView("idle");
  });
  document.querySelectorAll("[data-sample]").forEach(button => button.addEventListener("click", () => {
    const kind = button.dataset.sample;
    $("#route-select").value = "forward";
    syncRouteNodes();
    $("#start-select").value = "平顶山港";
    $("#end-select").value = "军李船闸";
    $("#departure-input").value = localDateTime();
    $("#adv-time").value = kind === "time" ? 1 : 6;
    $("#time-input").value = $("#adv-time").value;
    $("#adv-soc").value = kind === "soc" ? 31 : 85;
    $("#adv-load").value = "半载";
    writeQuickTask();
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
  initializeTaskDefaults();
  bindControls();
  refreshIcons();
  await loadHealth();
  await restorePendingRun();
  window.setTimeout(() => {
    const modelLoader = $("#vessel-loading");
    if (!window.Ship3DReady && modelLoader && !modelLoader.hidden) {
      modelLoader.classList.add("error");
      modelLoader.lastChild.textContent = "三维组件未能加载，请检查网络后刷新页面";
    }
  }, 15000);
});

window.addEventListener("beforeunload", () => {
  if (!state.voice) return;
  state.voice.capturing = false;
  state.voice.microphone.getTracks().forEach(track => track.stop());
});
