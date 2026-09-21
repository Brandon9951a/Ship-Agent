const state = { routes: {}, routeDetails: {}, stream: null, trace: [], finalAnswer: "", recording: false, hasRouteResult: false };

const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));

function refreshIcons() {
  if (window.lucide) window.lucide.createIcons();
}

function tickClock() {
  $("#clock").textContent = new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date());
}

function populateSelect(select, values, value) {
  select.innerHTML = values.map((item) => `<option value="${escapeHtml(item)}">${escapeHtml(item)}</option>`).join("");
  if (value && values.includes(value)) select.value = value;
}

async function loadRoutes() {
  const [routesResponse, detailsResponse] = await Promise.all([
    fetch("/api/routes"),
    fetch("/api/route-details"),
  ]);
  state.routes = await routesResponse.json();
  state.routeDetails = await detailsResponse.json();
  const names = Object.keys(state.routes);
  populateSelect($("#route-select"), names, names[0]);
  syncRouteNodes();
}

function syncRouteNodes() {
  const nodes = state.routes[$("#route-select").value] || [];
  populateSelect($("#start-select"), nodes, nodes[0]);
  populateSelect($("#end-select"), nodes, nodes[nodes.length - 1]);
}

function updateStatus(kind, text) {
  const target = $("#decision-status");
  target.className = `decision-status ${kind}`;
  target.innerHTML = `<span></span>${escapeHtml(text)}`;
}

function formatNumber(value, suffix = "") {
  if (value === undefined || value === null || value === "" || value === "-") return "--";
  const numeric = Number(value);
  return Number.isFinite(numeric) ? `${numeric.toFixed(numeric % 1 ? 1 : 0)}${suffix}` : `${value}${suffix}`;
}

function renderRouteLegacy(route, segments) {
  const start = route.origin && route.origin !== "-" ? route.origin : "起点";
  const end = route.destination && route.destination !== "-" ? route.destination : "终点";
  const names = [start, ...segments.map((segment) => (segment.route || "").split("→").pop()).filter(Boolean)];
  if (names[names.length - 1] !== end) names.push(end);
  $("#route-chip").textContent = route.lock_count && route.lock_count !== "-" ? route.lock_count : "航线待识别";
  $("#route-track").innerHTML = `<div class="route-line" style="--stops:${Math.max(names.length, 2)}">${names.map((name, index) => `<div class="route-stop"><i data-lucide="${index === 0 ? "circle-dot" : index === names.length - 1 ? "flag" : "map-pin"}"></i><span>${escapeHtml(name)}</span></div>`).join("")}</div>`;
  $("#segment-summary").innerHTML = segments.map((segment) => `<span>${escapeHtml(segment.route)} · ${escapeHtml(formatNumber(segment.distance_km, " km"))}</span>`).join("");
}

function renderSelectedRoutePreview() {
  if (state.hasRouteResult) return;
  renderRoute({
    origin: $("#start-select").value,
    destination: $("#end-select").value,
    lock_count: $("#route-select").value,
  }, []);
}

function resolveRouteNodes(route, segments) {
  const routeNodes = state.routeDetails[route.lock_count] || [];
  const startIndex = routeNodes.findIndex((node) => node.name === route.origin);
  const endIndex = routeNodes.findIndex((node) => node.name === route.destination);
  if (startIndex >= 0 && endIndex >= 0) return { nodes: routeNodes, startIndex, endIndex };

  const fallbackNames = [route.origin, ...segments.map((segment) => (segment.route || "").split("→").pop()).filter(Boolean)];
  if (fallbackNames[fallbackNames.length - 1] !== route.destination) fallbackNames.push(route.destination);
  return { nodes: fallbackNames.map((name) => ({ name, distance_km: 0 })), startIndex: 0, endIndex: fallbackNames.length - 1 };
}

function renderRoute(route, segments) {
  const start = route.origin && route.origin !== "-" ? route.origin : "起点";
  const end = route.destination && route.destination !== "-" ? route.destination : "终点";
  const view = { ...route, origin: start, destination: end };
  const { nodes, startIndex, endIndex } = resolveRouteNodes(view, segments);
  const lower = Math.min(startIndex, endIndex);
  const upper = Math.max(startIndex, endIndex);
  const from = (startIndex / Math.max(nodes.length - 1, 1)) * 100;
  const to = (endIndex / Math.max(nodes.length - 1, 1)) * 100;

  $("#route-chip").textContent = route.lock_count && route.lock_count !== "-" ? route.lock_count : "航线待识别";
  $("#route-track").innerHTML = `<div class="metro-route" style="--stops:${Math.max(nodes.length, 2)}; --ship-from:${from}%; --ship-to:${to}%">
    <span class="metro-ship" aria-hidden="true"><i data-lucide="ship"></i></span>
    ${nodes.map((node, index) => {
      const onJourney = index >= lower && index <= upper;
      const kind = index === startIndex ? "metro-origin" : index === endIndex ? "metro-destination" : onJourney ? "metro-waypoint" : "metro-off-route";
      const distance = index ? `${formatNumber(node.distance_km, " km")}` : "";
      return `<div class="metro-stop ${kind}">${distance ? `<span class="metro-distance">${escapeHtml(distance)}</span>` : ""}<span class="metro-dot"></span><span class="metro-port">${escapeHtml(node.name)}</span></div>`;
    }).join("")}
  </div>`;
  $("#segment-summary").innerHTML = `<span class="route-key origin-key">起点</span><span class="route-key waypoint-key">途经港口</span><span class="route-key destination-key">终点</span><span class="route-key">${escapeHtml(start)} → ${escapeHtml(end)}</span>`;
}

function renderEnergyChart(segments) {
  if (!segments.length) return;
  const energy = segments.map((segment) => Number(segment.energy_kwh) || 0);
  const speed = segments.map((segment) => Number(segment.speed_kmh) || 0);
  const maxEnergy = Math.max(...energy, 1);
  $(".energy-surface .legend").innerHTML = '<span class="legend-energy"></span> 推荐能耗 <span class="legend-speed"></span> 对地航速';
  $("#energy-chart").innerHTML = `<div class="bar-chart" style="--count:${segments.length}">${segments.map((segment, index) => {
    const height = Math.max(8, (energy[index] / maxEnergy) * 100);
    return `<div class="bar-group"><span class="bar-metrics"><span class="bar-speed-value">${formatNumber(speed[index], " km/h")}</span><span class="bar-value">${formatNumber(energy[index], " kWh")}</span></span><div class="bar" style="height:${height}%"></div><span class="bar-label">${escapeHtml(segment.route)}</span></div>`;
  }).join("")}</div>`;
}

function renderTable(segments) {
  $("#result-count").textContent = `${segments.length} 个航段`;
  $("#segment-table").innerHTML = segments.length ? segments.map((segment) => `<tr><td>${escapeHtml(segment.route)}</td><td>${escapeHtml(formatNumber(segment.distance_km, " km"))}</td><td>${escapeHtml(formatNumber(segment.speed_kmh, " km/h"))}</td><td>${escapeHtml(segment.speed_kn || "--")}</td><td>${escapeHtml(segment.lock_wait_h || "--")}</td><td>${escapeHtml(formatNumber(segment.energy_kwh, " kWh"))}</td></tr>`).join("") : '<tr class="placeholder-row"><td colspan="6">等待航速优化结果</td></tr>';
}

function formatSafetyText(text) {
  const tokenPattern = /(当前电池状态不支持安全跑完全程|不支持跑完全程|支持跑完全程|航次不可行|时间约束|SOC均衡|节能效果|剩余续航|不可行|需补能|超出|风险|SOC|OK|[+]?\d+(?:\.\d+)?%|\d+(?:\.\d+)?\s*km)/g;
  const tokenClass = (token) => {
    if (/不支持|不可行|需补能|超出|风险/.test(token)) return "safety-key-red";
    if (/支持跑完全程|^OK$/.test(token)) return "safety-key-green";
    if (/%$/.test(token)) return "safety-value-green";
    if (/km$/.test(token)) return "safety-value-blue";
    return "safety-key-blue";
  };
  return escapeHtml(text).replace(tokenPattern, (token) => `<span class="${tokenClass(token)}">${token}</span>`);
}

function renderSafety(items) {
  $("#safety-list").innerHTML = items.length ? items.map((item) => {
    const name = String(item.name || "");
    const category = /节能|续航/.test(name) ? "benefit" : (/SOC/.test(name) ? "soc" : "state");
    return `<div class="safety-item ${escapeHtml(item.status || "unknown")} ${category}"><span class="safety-label">${escapeHtml(name)}</span><strong class="safety-content">${formatSafetyText(item.text)}</strong></div>`;
  }).join("") : '<div class="empty-inline">暂无校验结果</div>';
}

function formatRecommendation(text) {
  let html = escapeHtml(text);
  html = html.replace(/(节能率|节能效果|节能)/g, '<span class="advice-green">$1</span>');
  html = html.replace(/(剩余续航|SOC均衡|初始SOC|最终SOC|SOC)/g, '<span class="advice-blue">$1</span>');
  html = html.replace(/(FDP|模式\d*|平均功率|持续)/g, '<span class="advice-amber">$1</span>');
  html = html.replace(/([+]?\d+(?:\.\d+)?%)/g, '<span class="advice-green">$1</span>');
  html = html.replace(/(\d+(?:\.\d+)?\s*km)/g, '<span class="advice-blue">$1</span>');
  html = html.replace(/(\d+(?:\.\d+)?\s*kW(?:h)?)/g, '<span class="advice-amber">$1</span>');
  return html;
}

function renderRecommendations(items) {
  $("#recommendation-list").innerHTML = items.length ? items.map((item) => `<div class="recommendation ${escapeHtml(item.type || "info")}">${formatRecommendation(item.text)}</div>`).join("") : '<div class="empty-inline">等待 EMS 仿真结果</div>';
}

function renderInfeasibleAlert(message) {
  const fullMessage = String(message || "当前航次不满足执行条件。").trim();
  var reason, suggestion;

  // 判断不可行类型
  if (fullMessage.indexOf('电量不可行') !== -1 || fullMessage.indexOf('航次电量预检') !== -1) {
    // 电量不足：显示完整分析
    var lines = fullMessage.split('\n');
    reason = '';
    suggestion = '';
    var inSuggestion = false;
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i].trim();
      if (!line) continue;
      if (line.indexOf('💡') !== -1 || line.indexOf('建议') !== -1) inSuggestion = true;
      if (!inSuggestion) {
        reason += line + '\n';
      } else {
        suggestion += line + '\n';
      }
    }
    if (!suggestion) suggestion = '建议在中途港口补能后再继续航行。';
  } else if (fullMessage.indexOf('时间不可行') !== -1 || fullMessage.indexOf('航次可行性预检') !== -1) {
    // 时间不足
    var lines = fullMessage.split('\n');
    reason = '';
    suggestion = '';
    var inSuggestion = false;
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i].trim();
      if (!line) continue;
      if (line.indexOf('建议') !== -1) inSuggestion = true;
      if (!inSuggestion) {
        reason += line + '\n';
      } else {
        suggestion += line + '\n';
      }
    }
  } else {
    // 其他错误：显示完整信息
    reason = fullMessage.length > 300 ? fullMessage.substring(0, 300) + '...' : fullMessage;
    suggestion = '请检查输入参数后重试。';
  }

  $("#infeasible-reason").textContent = reason || '分析中...';
  $("#infeasible-suggestion").textContent = suggestion || '请参考上方原因分析。';
  $("#infeasible-alert").hidden = false;
  $("#reply-panel").classList.add("infeasible-reply");
}

function renderDashboard(dashboard) {
  const route = dashboard.route || {};
  const summary = dashboard.summary || {};
  const segments = dashboard.segments || [];
  $("#metric-energy").textContent = formatNumber(summary.total_energy_kwh, " kWh");
  $("#metric-saving").textContent = formatNumber(summary.saving_pct, "%");
  $("#metric-time").textContent = formatNumber(summary.total_time_h, " h");
  $("#metric-soc").textContent = summary.arrival_soc_pct === "需补能" ? "需补能" : formatNumber(summary.arrival_soc_pct, "%");
  if (route.origin && route.origin !== "-") {
    $("#mission-title").textContent = `${route.origin} → ${route.destination}`;
    $("#mission-subtitle").textContent = `${route.total_km ?? "--"} km · ${route.segment_count ?? "--"} 个航段 · ${route.lock_wait_h ?? ""}`;
  }
  if (segments.length || route.origin) {
    state.hasRouteResult = true;
    renderRoute(route, segments);
  }
  renderEnergyChart(segments);
  renderTable(segments);
  renderSafety(dashboard.safety || []);
  renderRecommendations(dashboard.recommendations || []);
  refreshIcons();
}

function renderTrace() {
  $("#trace-list").innerHTML = state.trace.map((item, index) => `<li class="trace-item"><span class="trace-index">${index + 1}</span><div><strong>${escapeHtml(item.tool || "最终回复")}</strong><p>${escapeHtml(item.thought || item.observation || "已完成")}</p>${item.params && Object.keys(item.params).length ? `<code>${escapeHtml(JSON.stringify(item.params, null, 2))}</code>` : ""}</div></li>`).join("");
  $("#trace-meta").textContent = state.trace.length ? `${state.trace.length} 个步骤` : "等待任务";
}

function resetResult() {
  state.trace = []; state.finalAnswer = ""; state.hasRouteResult = false;
  $("#mission-title").textContent = "正在准备决策任务";
  $("#mission-subtitle").textContent = "正在连接本地工具链并执行多阶段推理。";
  ["#metric-energy", "#metric-saving", "#metric-time", "#metric-soc"].forEach((id) => { $(id).textContent = "--"; });
  $("#route-chip").textContent = "推理中";
  $("#route-track").innerHTML = '<div class="empty-inline"><i data-lucide="loader-circle"></i><span>正在识别航段与环境工况</span></div>';
  $("#segment-summary").innerHTML = "";
  $("#energy-chart").innerHTML = '<div class="empty-inline"><i data-lucide="activity"></i><span>正在计算能耗曲线</span></div>';
  renderTable([]); renderSafety([]); renderRecommendations([]); renderTrace();
  $("#reply-panel").hidden = true;
  $("#reply-panel").classList.remove("infeasible-reply");
  $("#reply-answer").textContent = "";
  $("#infeasible-alert").hidden = true;
  $("#infeasible-reason").textContent = "";
  $("#infeasible-suggestion").textContent = "";
  $("#final-response").hidden = true;
  refreshIcons();
}

async function runInference() {
  const message = $("#mission-input").value.trim();
  if (!message) { $("#mission-input").focus(); return; }
  if (state.stream) state.stream.abort();
  state.stream = new AbortController();
  $("#run-button").disabled = true;
  updateStatus("running", "推理中"); resetResult();
  try {
    const response = await fetch("/api/infer", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message: message, use_hardware: (document.getElementById("hw-enable-check")?.checked ?? true), soc: Number(document.getElementById("adv-soc")?.value||0), load_state: document.getElementById("adv-load")?.value||"自动", wind: Number(document.getElementById("adv-wind")?.value||0), time_limit_h: Number(document.getElementById("adv-time")?.value||38) }), signal: state.stream.signal });
    if (!response.ok) throw new Error((await response.json()).error || "请求失败");
    const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = "";
    while (true) {
      const { value, done } = await reader.read(); if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const events = buffer.split("\n\n"); buffer = events.pop();
      events.forEach((raw) => {
        const dataLine = raw.split("\n").find((line) => line.startsWith("data: "));
        if (!dataLine) return;
        const data = JSON.parse(dataLine.slice(6));
        if (raw.startsWith("event: error")) throw new Error(data.message || "推理失败");
        if (!data.dashboard) return;
        renderDashboard(data.dashboard);
        if (typeof renderProgress === 'function') renderProgress(data.phase, data.tool, data.complete, data.status === 'infeasible' || data.status === 'error');
        if (data.phase === "observation" || data.phase === "infeasible") {
          state.trace.push({ tool: data.tool, thought: data.thought, observation: data.observation, params: data.params }); renderTrace();
        }
        if (data.final_answer) {
          state.finalAnswer = data.final_answer;
          $("#reply-answer").textContent = data.final_reply || data.final_answer;
          $("#reply-panel").hidden = false;
          $("#final-answer").textContent = data.final_answer;
          $("#final-response").hidden = false;
        }
        if (data.complete) {
          const isInfeasible = data.status === "infeasible";
          updateStatus(isInfeasible ? "error" : "success", isInfeasible ? "航次不可行" : "方案已生成");
          if (isInfeasible) renderInfeasibleAlert(data.final_reply || data.final_answer || data.observation);
        }
      });
    }
  } catch (error) {
    if (error.name !== "AbortError") { updateStatus("error", "计算失败"); $("#mission-subtitle").textContent = error.message; }
  } finally { $("#run-button").disabled = false; state.stream = null; }
}

function pcm16Base64(input, sourceRate) {
  const ratio = sourceRate / 16000;
  const length = Math.max(1, Math.round(input.length / ratio));
  const output = new Int16Array(length);
  for (let index = 0; index < length; index += 1) {
    const start = Math.floor(index * ratio);
    const end = Math.min(input.length, Math.floor((index + 1) * ratio));
    let sum = 0;
    for (let cursor = start; cursor < Math.max(start + 1, end); cursor += 1) sum += input[cursor] || 0;
    const sample = Math.max(-1, Math.min(1, sum / Math.max(1, end - start)));
    output[index] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
  }
  const bytes = new Uint8Array(output.buffer);
  let binary = "";
  for (let index = 0; index < bytes.length; index += 1) binary += String.fromCharCode(bytes[index]);
  return btoa(binary);
}

async function voiceRequest(path, payload = {}) {
  const response = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || "语音服务请求失败");
  return body;
}

function updateVoiceText(voice, text) {
  voice.transcript = text || voice.transcript || "";
  $("#mission-input").value = `${voice.baseText}${voice.transcript}`;
}

async function startXfyunVoice() {
  if (!navigator.mediaDevices?.getUserMedia || !window.AudioContext) throw new Error("当前浏览器不支持麦克风录音");
  $("#voice-copy").textContent = "正在连接科大讯飞...";
  const microphone = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
  let started;
  try {
    started = await voiceRequest("/api/voice/start");
  } catch (error) {
    microphone.getTracks().forEach((track) => track.stop());
    throw error;
  }
  const context = new AudioContext();
  const source = context.createMediaStreamSource(microphone);
  await context.resume();
  // 8192 samples keep the HTTP-to-WebSocket bridge from building up a large
  // request queue while still providing frequent enough interim results.
  const processor = context.createScriptProcessor(8192, 1, 1);
  const voice = {
    sessionId: started.session_id,
    microphone,
    context,
    source,
    processor,
    baseText: $("#mission-input").value,
    transcript: "",
    queue: Promise.resolve(),
    capturing: true,
    failed: false,
    stopping: false,
  };
  source.connect(processor); processor.connect(context.destination);
  processor.onaudioprocess = (event) => {
    if (!voice.capturing) return;
    const audio = pcm16Base64(event.inputBuffer.getChannelData(0), context.sampleRate);
    voice.queue = voice.queue.then(async () => {
      const result = await voiceRequest("/api/voice/chunk", { session_id: voice.sessionId, audio });
      updateVoiceText(voice, result.text);
      $("#voice-copy").textContent = result.text ? `实时识别：${result.text.slice(-24)}` : "正在听写...";
    }).catch((error) => {
      voice.failed = true;
      voice.capturing = false;
      $("#voice-copy").textContent = `识别失败：${error.message}`;
    });
  };
  state.voice = voice; state.recording = true;
  $("#voice-button").classList.add("recording");
  $("#voice-copy").textContent = "正在听写，再次点击结束";
}

async function stopXfyunVoice() {
  const voice = state.voice;
  // A second click can arrive while the finish request is still pending.
  // Make shutdown idempotent so it cannot close the same AudioContext twice.
  if (!voice || voice.stopping) return;
  voice.stopping = true;
  state.recording = false;
  // Stop capturing first, but keep the queue active.  Marking it inactive
  // before the queue drains used to discard the final spoken words.
  voice.capturing = false;
  voice.processor.disconnect(); voice.source.disconnect();
  voice.microphone.getTracks().forEach((track) => track.stop());
  await voice.queue;
  try {
    const result = await voiceRequest("/api/voice/finish", { session_id: voice.sessionId });
    updateVoiceText(voice, result.text);
    $("#voice-copy").textContent = result.text
      ? (voice.failed ? "语音识别完成，末段上传异常" : "科大讯飞识别完成")
      : "未识别到有效语音";
  } finally {
    if (voice.context && voice.context.state !== "closed") {
      await voice.context.close();
    }
    if (state.voice === voice) state.voice = null;
    state.recording = false;
    $("#voice-button").classList.remove("recording");
  }
}

function attachSpeechInput() {
  $("#voice-button").addEventListener("click", async () => {
    try {
      if (state.voice?.stopping) return;
      if (state.recording) await stopXfyunVoice(); else await startXfyunVoice();
    } catch (error) {
      state.recording = false;
      $("#voice-button").classList.remove("recording");
      $("#voice-copy").textContent = `语音识别失败：${error.message}`;
    }
  });
}

function bindControls() {
  $("#route-select").addEventListener("change", syncRouteNodes);
  $("#quick-fill").addEventListener("click", () => { const hours = $("#time-input").value || 5; $("#mission-input").value = `${$("#start-select").value}到${$("#end-select").value}，${hours}小时`; });
  $("#time-minus").addEventListener("click", () => { $("#time-input").value = Math.max(.5, Number($("#time-input").value || 5) - .5); });
  $("#time-plus").addEventListener("click", () => { $("#time-input").value = Math.min(72, Number($("#time-input").value || 5) + .5); });
  document.querySelectorAll("[data-sample]").forEach((button) => button.addEventListener("click", () => { $("#mission-input").value = button.dataset.sample; }));
  $("#run-button").addEventListener("click", runInference);
  $("#mission-input").addEventListener("keydown", (event) => { if ((event.ctrlKey || event.metaKey) && event.key === "Enter") runInference(); });
  $("#clear-button").addEventListener("click", resetResult);
  $("#copy-final").addEventListener("click", async () => { if (state.finalAnswer) await navigator.clipboard.writeText(state.finalAnswer); });
  attachSpeechInput();
}

window.addEventListener("DOMContentLoaded", async () => { tickClock(); setInterval(tickClock, 1000); await loadRoutes(); bindControls(); refreshIcons(); });
