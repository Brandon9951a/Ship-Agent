const ALL_ROUTE_NODES = ["平顶山港", "军李船闸", "马湾船闸", "漯河船闸", "漯河港", "大路李船闸", "葫芦湾船闸", "周口船闸", "周口港"];
// 与 configs/route_facts.yaml 一致；来源：豫交投001港、闸之间距离情况.pptx，第 1–9 页。
const ROUTE_SEGMENT_METERS = [25500, 25000, 44100, 689, 22600, 24900, 27400, 19600];

const state = {
  trace: [], finalAnswer: "", running: false, lastResult: null,
  voice: null, recording: false, speechAvailable: false, resultStale: false,
  inputRevision: 0, quickDirty: false, requestSequence: 0, activeRequest: null,
  llmMode: "unknown", authUser: null, csrfToken: "", sessionExpiresAt: null,
  workspaceInitialized: false, authTransitioning: false, clockTimer: null,
};

const OPTION_LABELS = {
  accept_late: "接受延时",
  adjust_departure: "提前出发",
  recharge: "补能后出发",
  recharge_and_extend: "补能并延长航时",
  shorten_route: "缩短航线",
  give_up: "放弃任务",
};
const MISSING_FIELD_LABELS = {
  origin: "起点", destination: "终点", departure_at: "出发时间",
  soc_initial: "初始 SOC", load_state_or_draft_m: "载况或吃水",
  arrival_deadline_or_max_duration_h: "到达约束或最长航时",
};
const ACTIVE_THREAD_KEY = "ship-agent.active-thread";
const ACTIVE_TASK_TEXT_KEY = "ship-agent.active-task-text";
const ACTIVE_REQUEST_KEY = "ship-agent.active-request";
const SAVED_PLANS_KEY = "ship-agent.saved-plans";
const HISTORY_KEY = "ship-agent.conversation-history";
const HISTORY_MAX_CHARS = 1500000;
const USER_STORAGE_KEYS = [
  ACTIVE_THREAD_KEY, ACTIVE_TASK_TEXT_KEY, ACTIVE_REQUEST_KEY, SAVED_PLANS_KEY, HISTORY_KEY,
];
const AUTH_ERROR_MESSAGES = {
  invalid_auth_request: "请检查用户名和密码格式。",
  invalid_credentials: "账号或密码错误。",
  auth_required: "登录状态已失效，请重新登录。",
  forbidden: "无权限或请求已失效，请刷新后重试。",
  too_many_attempts: "尝试次数过多，请稍后再试。",
  auth_unavailable: "认证服务暂不可用，请稍后重试。",
};
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

function formatHours(value) {
  if (value === undefined || value === null || value === "") return "--";
  const number = Number(value);
  return Number.isFinite(number) ? `${Number(number.toFixed(3))} h` : "--";
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
  $("#correction-goal").value = request.optimization_goal || "min_energy";
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
    optimization_goal: $("#correction-goal").value,
  };
}

function syncCorrectionToTask(payload, { rewriteDescription = true } = {}) {
  const departure = localDateTimeValue(payload.departure_at).replace("T", " ");
  const soc = Number(payload.soc_initial) * 100;
  const socText = String(Number(soc.toFixed(1)));
  const durationText = String(Math.ceil(Number(payload.max_duration_h) * 1000) / 1000);
  const goalText = payload.optimization_goal === "min_time" ? "，优先最短航时" : "";
  if (rewriteDescription) $("#mission-input").value = `从${payload.origin}到${payload.destination}，${departure}出发，SOC${socText}%，${payload.load_state}，${durationText}小时内到达${goalText}`;
  $("#departure-input").value = localDateTimeValue(payload.departure_at);
  $("#time-input").value = payload.max_duration_h;
  $("#adv-soc").value = Number(soc.toFixed(1));
  $("#adv-load").value = payload.load_state;
  $("#optimization-goal").value = payload.optimization_goal || "min_energy";
  $("#start-select").value = ALL_ROUTE_NODES.includes(payload.origin) ? payload.origin : "";
  $("#end-select").value = ALL_ROUTE_NODES.includes(payload.destination) ? payload.destination : "";
  renderQuickRoutePreview();
  state.quickDirty = false;
}

function requestCompleteForForm(request) {
  return Boolean(request?.origin && request?.destination && request?.departure_at &&
    request.max_duration_h != null && request.soc_initial != null && request.load_state);
}

function syncPartialRequestToInputs(request = {}) {
  $("#start-select").value = ALL_ROUTE_NODES.includes(request.origin) ? request.origin : "";
  $("#end-select").value = ALL_ROUTE_NODES.includes(request.destination) ? request.destination : "";
  renderQuickRoutePreview();
  $("#departure-input").value = localDateTimeValue(request.departure_at);
  $("#time-input").value = request.max_duration_h ?? "";
  $("#adv-soc").value = request.soc_initial == null ? "" : Number((request.soc_initial * 100).toFixed(1));
  $("#adv-load").value = request.load_state || "";
  $("#optimization-goal").value = request.optimization_goal || "min_energy";
  state.quickDirty = false;
}

function initializeQuickRouteNodes() {
  populateSelect($("#start-select"), ALL_ROUTE_NODES, ALL_ROUTE_NODES[0], "请选择起点");
  populateSelect($("#end-select"), ALL_ROUTE_NODES, ALL_ROUTE_NODES.at(-1), "请选择终点");
}

function formatRouteKilometers(meters) {
  return `${Number((meters / 1000).toFixed(3))} km`;
}

function renderQuickRoutePreview() {
  const startIndex = ALL_ROUTE_NODES.indexOf($("#start-select").value);
  const endIndex = ALL_ROUTE_NODES.indexOf($("#end-select").value);
  const steps = $("#quick-route-steps");
  const count = $("#quick-route-count");
  const distance = $("#quick-route-distance");
  const source = $("#quick-route-source");
  if (startIndex < 0 || endIndex < 0 || startIndex === endIndex) {
    steps.textContent = startIndex === endIndex && startIndex >= 0
      ? "起点和终点不能相同。" : "选择起点和终点后显示完整航段。";
    count.textContent = "";
    distance.textContent = "--";
    source.textContent = "";
    return;
  }

  const direction = startIndex < endIndex ? 1 : -1;
  const nodeIndexes = [];
  for (let index = startIndex; ; index += direction) {
    nodeIndexes.push(index);
    if (index === endIndex) break;
  }
  let totalMeters = 0;
  steps.innerHTML = nodeIndexes.map((index, position) => {
    const label = position === 0 ? "起点" : position === nodeIndexes.length - 1 ? "终点" : "";
    const node = `<div class="quick-route-node"><span class="quick-route-dot" aria-hidden="true"></span><strong>${escapeHtml(ALL_ROUTE_NODES[index])}</strong><small>${label}</small></div>`;
    if (position === nodeIndexes.length - 1) return node;
    const legMeters = ROUTE_SEGMENT_METERS[Math.min(index, nodeIndexes[position + 1])];
    totalMeters += legMeters;
    const legLabel = legMeters < 1000 ? `${legMeters} m` : formatRouteKilometers(legMeters);
    return `${node}<div class="quick-route-leg"><span>${legLabel}</span></div>`;
  }).join("");
  count.textContent = `${nodeIndexes.length} 个节点 · ${nodeIndexes.length - 1} 个航段`;
  distance.textContent = formatRouteKilometers(totalMeters);
  source.textContent = direction < 0
    ? "距离来源：豫交投001港、闸之间距离情况（第 1–9 页）。返程沿用相同航段距离，逆向通航条件尚未确认。"
    : "距离来源：豫交投001港、闸之间距离情况（第 1–9 页）。仅为资料距离，不包含船闸等待。";
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

function researchToolTrace(events) {
  return events.filter(event => event.type === "tool_completed" || event.type === "tool_failed")
    .map(event => ({ node: event.tool, status: event.type === "tool_completed" ? "ok" : "failed" }));
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

function scopedStorageKey(key, userId = state.authUser?.id) {
  if (!userId) return null;
  const suffix = String(key).replace(/^ship-agent\./, "");
  return `ship-agent.user.${encodeURIComponent(userId)}.${suffix}`;
}

function userStorageGet(key) {
  const scopedKey = scopedStorageKey(key);
  if (!scopedKey) return null;
  try { return localStorage.getItem(scopedKey); } catch (_error) { return null; }
}

function userStorageSet(key, value) {
  const scopedKey = scopedStorageKey(key);
  if (!scopedKey) return false;
  try { localStorage.setItem(scopedKey, value); return true; } catch (_error) { return false; }
}

function userStorageRemove(key) {
  const scopedKey = scopedStorageKey(key);
  if (!scopedKey) return;
  try { localStorage.removeItem(scopedKey); } catch (_error) { /* Storage may be unavailable. */ }
}

function clearUserStorage(userId) {
  for (const key of USER_STORAGE_KEYS) {
    const scopedKey = scopedStorageKey(key, userId);
    if (scopedKey) {
      try { localStorage.removeItem(scopedKey); } catch (_error) { /* Storage may be unavailable. */ }
    }
  }
}

function clearLegacyStorage() {
  for (const key of USER_STORAGE_KEYS) {
    try { localStorage.removeItem(key); } catch (_error) { /* Ignore old unscoped snapshots. */ }
  }
}

function authMessage(error, fallback = "请求失败，请重试。") {
  return AUTH_ERROR_MESSAGES[error] || fallback;
}

async function apiFetch(path, options = {}) {
  const method = String(options.method || "GET").toUpperCase();
  const headers = { ...(options.headers || {}) };
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method) && path !== "/api/auth/login" && state.csrfToken) {
    headers["X-CSRF-Token"] = state.csrfToken;
  }
  const response = await fetch(path, {
    ...options,
    headers,
    credentials: "same-origin",
    cache: "no-store",
  });
  if (response.status === 401 && path !== "/api/auth/me" && path !== "/api/auth/login") {
    let body = {};
    try { body = await response.clone().json(); } catch (_error) { /* Keep generic auth handling. */ }
    if (body.error === "auth_required") expireSession(body.message || authMessage(body.error));
  }
  return response;
}

function showLoginScreen(message = "") {
  $("#app-shell").hidden = true;
  $("#login-screen").hidden = false;
  $("#session-identity").hidden = true;
  $("#admin-diagnostics").hidden = true;
  $("#admin-diagnostics").textContent = "";
  $("#login-feedback").textContent = message;
  $("#login-submit").disabled = false;
  $("#login-submit").textContent = "登录";
  refreshIcons();
}

function expireSession(message = authMessage("auth_required")) {
  if (state.authTransitioning) return;
  state.authTransitioning = true;
  const userId = state.authUser?.id;
  if (userId) clearUserStorage(userId);
  clearLegacyStorage();
  state.activeRequest?.controller.abort();
  state.activeRequest = null;
  state.running = false;
  state.lastResult = null;
  state.authUser = null;
  state.csrfToken = "";
  state.sessionExpiresAt = null;
  state.llmMode = "unknown";
  showLoginScreen(message);
  state.authTransitioning = false;
  // A fresh document also removes rendered voyage details and in-memory UI state.
  // Hiding the workspace alone leaves the previous account's result in the DOM.
  window.location?.reload?.();
}

function acceptSession(session) {
  const user = session?.user;
  if (!user || typeof user.id !== "string" || !user.id ||
      !["user", "admin"].includes(user.role) ||
      typeof session.csrf_token !== "string" || !session.csrf_token) {
    throw new Error("登录服务返回了无效会话信息。");
  }
  if (state.authUser?.id && state.authUser.id !== user.id) clearUserStorage(state.authUser.id);
  clearLegacyStorage();
  state.authUser = {
    id: user.id,
    username: user.username,
    display_name: user.display_name || user.username,
    role: user.role,
  };
  state.csrfToken = session.csrf_token;
  state.sessionExpiresAt = session.session_expires_at || null;
  $("#login-screen").hidden = true;
  $("#app-shell").hidden = false;
  $("#session-identity").hidden = false;
  $("#session-name").textContent = state.authUser.display_name;
  $("#session-role").textContent = user.role === "admin" ? "管理员" : "普通用户";
  $("#session-role").className = `role-badge ${user.role}`;
  $("#admin-diagnostics").hidden = true;
  $("#admin-diagnostics").textContent = "";
  refreshIcons();
}

async function loginWithPassword(event) {
  event?.preventDefault();
  if (state.authTransitioning) return;
  const username = $("#login-username").value.trim();
  const password = $("#login-password").value;
  if (!username || !password) {
    $("#login-feedback").textContent = "请输入用户名和密码。";
    return;
  }
  state.authTransitioning = true;
  $("#login-submit").disabled = true;
  $("#login-submit").textContent = "正在验证…";
  $("#login-feedback").textContent = "";
  try {
    const response = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      cache: "no-store",
      body: JSON.stringify({ username, password }),
    });
    const body = response.status === 204 ? {} : await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(authMessage(body.error, body.message || "登录失败，请重试。"));
    acceptSession(body);
    $("#login-password").value = "";
    state.authTransitioning = false;
    await initializeAuthenticatedWorkspace();
  } catch (error) {
    $("#login-feedback").textContent = error.message || authMessage("auth_unavailable");
    $("#login-submit").disabled = false;
    $("#login-submit").textContent = "登录";
    state.authTransitioning = false;
  }
}

async function logoutCurrentUser() {
  const button = $("#logout-button");
  button.disabled = true;
  try {
    const response = await apiFetch("/api/auth/logout", { method: "POST" });
    if (!response.ok && response.status !== 204) {
      const body = await response.json().catch(() => ({}));
      throw new Error(authMessage(body.error, "退出登录失败，请重试。"));
    }
    expireSession("已退出登录。请使用账号重新登录。");
  } catch (error) {
    if (state.authUser) {
      $("#mission-subtitle").textContent = error.message || "退出登录失败，请重试。";
      button.disabled = false;
    }
  }
}

function bindAuthControls() {
  $("#login-form").addEventListener("submit", loginWithPassword);
  $("#logout-button").addEventListener("click", logoutCurrentUser);
}

async function restoreLoginSession() {
  try {
    const response = await fetch("/api/auth/me", {
      credentials: "same-origin", cache: "no-store",
    });
    const body = response.status === 204 ? {} : await response.json().catch(() => ({}));
    if (!response.ok) {
      const message = body.error === "auth_required"
        ? "请登录后继续。"
        : authMessage(body.error, "登录服务暂不可用，请稍后重试。");
      showLoginScreen(response.status === 401 ? "" : message);
      return false;
    }
    acceptSession(body);
    await initializeAuthenticatedWorkspace();
    return true;
  } catch (_error) {
    showLoginScreen("无法连接登录服务，请检查网络后重试。");
    return false;
  }
}

function renderAdminDiagnostics(payload) {
  if (state.authUser?.role !== "admin") return;
  const release = payload.release || {};
  const rows = [
    ["发布版本", release.app_version || "未知"],
    ["基线", release.baseline_id || "未知"],
    ["参数档案", release.parameter_profile || "未知"],
    ["参数批准日期", release.parameter_approved_at || "未知"],
    ["模型服务配置", payload.llm_mode || "未知"],
    ["检查点存储", payload.checkpoint_backend || "未知"],
    ["检查点状态", payload.checkpoint_ready ? "可用" : "未就绪"],
    ["语音服务", payload.speech_mode || "未知"],
  ];
  $("#admin-diagnostics").innerHTML = `<header><div><span class="surface-kicker">ADMINISTRATOR</span><h2>管理员服务诊断</h2></div><span class="role-badge admin">仅管理员可见</span></header><dl>${rows.map(([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("")}</dl><p>服务配置只说明功能是否配置；单次模型调用结果以具体任务记录为准。</p>`;
  $("#admin-diagnostics").hidden = false;
}

async function loadAdminDiagnostics() {
  if (state.authUser?.role !== "admin") {
    $("#admin-diagnostics").hidden = true;
    $("#admin-diagnostics").textContent = "";
    return;
  }
  try {
    const response = await apiFetch("/api/admin/system-status");
    const body = await response.json().catch(() => ({}));
    if (response.status === 403) {
      $("#admin-diagnostics").hidden = true;
      $("#admin-diagnostics").textContent = "";
      $("#mission-subtitle").textContent = authMessage(body.error, "当前账号无权读取管理员诊断信息。");
      return;
    }
    if (!response.ok) throw new Error(authMessage(body.error, "管理员诊断信息暂不可用。"));
    renderAdminDiagnostics(body);
  } catch (_error) {
    if (state.authUser?.role === "admin") {
      $("#admin-diagnostics").innerHTML = "<header><div><span class=\"surface-kicker\">ADMINISTRATOR</span><h2>管理员服务诊断</h2></div></header><p>诊断信息暂不可用，航次功能仍可继续使用。</p>";
      $("#admin-diagnostics").hidden = false;
    }
  }
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

function renderEnergyChart(segments, summary = {}) {
  if (!segments.length) {
    $("#energy-chart").innerHTML = '<div class="empty-inline"><i data-lucide="chart-no-axes-combined"></i><span>优化完成后显示分段对比</span></div>';
    $("#energy-breakdown").hidden = true;
    refreshIcons();
    return;
  }
  const maxEnergy = Math.max(...segments.map(segment => Number(segment.energy_kwh) || 0), 1);
  $(".energy-surface .legend").innerHTML = '<span class="legend-energy"></span> 推进能耗 <span class="legend-speed"></span> 对地航速';
  $("#energy-chart").innerHTML = `<div class="bar-chart" style="--count:${segments.length}">${segments.map(segment => {
    const height = Math.max(0, ((Number(segment.energy_kwh) || 0) / maxEnergy) * 100);
    return `<div class="bar-group"><span class="bar-metrics"><span class="bar-speed-value">${formatNumber(segment.speed_kmh, " km/h", 2)}</span><span class="bar-value">${formatNumber(segment.energy_kwh, " kWh")}</span></span><div class="bar-track"><div class="bar" style="height:${height}%"></div></div><span class="bar-label" title="${escapeHtml(segment.route)}">${escapeHtml(segment.route)}</span></div>`;
  }).join("")}</div>`;
  const breakdown = $("#energy-breakdown");
  const propulsion = summary.propulsion_energy?.value;
  const auxiliary = summary.auxiliary_energy?.value;
  const total = summary.required_energy?.value;
  breakdown.hidden = propulsion == null || auxiliary == null || total == null;
  if (!breakdown.hidden) {
    $("#energy-propulsion").textContent = formatNumber(propulsion, " kWh");
    $("#energy-auxiliary").textContent = formatNumber(auxiliary, " kWh");
    $("#energy-total").textContent = formatNumber(total, " kWh");
  }
}

function renderTable(segments) {
  $("#result-count").textContent = `${segments.length} 个航段`;
  $("#segment-table").innerHTML = segments.length ? segments.map(segment => `<tr>
    <td>${escapeHtml(segment.route)}</td>
    <td>${escapeHtml(formatNumber(segment.distance_km, " km"))}</td>
    <td>${escapeHtml(formatNumber(segment.speed_kmh, " km/h", 3))}</td>
    <td>${escapeHtml(formatHours(segment.duration_h))}</td>
    <td>${escapeHtml(formatNumber(segment.energy_kwh, " kWh"))}</td>
  </tr>`).join("") : '<tr class="placeholder-row"><td colspan="5">等待航速优化结果</td></tr>';
}

const CHECK_LABELS = { time: "航时约束", speed: "航速范围", power: "推进功率", soc: "电量余度", soc_1: "甲组电量余度", soc_2: "乙组电量余度" };

function checkValue(value, unit) {
  if (value == null) return "--";
  if (unit === "fraction") return formatNumber(Number(value) * 100, "%");
  if (unit === "h") return formatHours(value);
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
    $("#recommendation-list").innerHTML = '<div class="empty-inline">请先在上方选择调整方案；确认后更新能量管理建议。</div>';
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
    const status = item.status === "ok" ? "finish" : item.status || "完成";
    return `<li class="trace-item"><span class="trace-index">${index + 1}</span><div><strong>${escapeHtml(label)}</strong><p>${escapeHtml(`${round} · ${status}${option}`)}</p></div></li>`;
  }).join("");
  $("#trace-meta").textContent = state.trace.length ? `${state.trace.length} 个步骤` : state.running ? "等待任务完成" : "等待任务";
}

function renderModel(result) {
  if (result.workflow_mode === "research_agent") {
    $("#model-mode").textContent = result.answer_status === "accepted" ? "真实模型答复" : "工具核验结果";
    $("#reply-answer").textContent = result.final_answer || result.final_message || "任务尚未生成答复。";
    $("#reply-panel").hidden = false;
    return;
  }
  const reportMode = result.report?.explanation_mode;
  const understandingMode = result.task_understanding?.mode;
  const llmUsed = reportMode === "llm_qualitative" || understandingMode === "llm_qualitative";
  const explanation = result.status === "ok"
    ? (result.report?.explanation || "仿真方案已生成。请结合现场条件核对后再作判断。")
    : (result.status === "infeasible" || result.status === "awaiting_choice")
      ? "当前输入条件下未生成仿真航速方案；下方展示系统内备选计算结果。"
      : "请补充或修正任务信息后重新计算。";
  $("#model-mode").textContent = state.authUser?.role === "admin"
    ? (llmUsed ? "本任务实际调用模型" : "本任务使用工具/模板结果")
    : "工具计算结果";
  $("#reply-answer").textContent = explanation;
  $("#reply-panel").hidden = false;
}

function optionAction(option) {
  if (option.direction === "give_up") return "保留结论";
  if (option.requires_input) return "需填写实测值";
  if (option.verified && option.modification) return "采用并重算";
  return "不可直接采用";
}

function renderAdjustmentOptions(options) {
  if (!options.length) {
    $("#adjustment-options").innerHTML = "";
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
    const response = await apiFetch("/api/resume", {
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
    rememberHistoryResult(result, $("#mission-input").value);
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
  $("#infeasible-title").textContent = awaiting ? "当前输入条件不满足，等待选择" : infeasible ? "当前输入条件不可行" : result.status === "failed" ? "任务执行未完成" : "任务需要补充或修正";
  const missing = (result.missing_fields || []).map(field => MISSING_FIELD_LABELS[field] || field);
  if (result.workflow_mode !== "research_agent" && !result.request?.departure_at && !missing.includes("出发时间")) missing.push("出发时间");
  $("#infeasible-reason").textContent = !awaiting && !infeasible && missing.length
    ? `请补齐：${missing.join("、")}。`
    : failedRecord.reason || failedPayload.reason || (result.questions || []).join("；") || result.final_message || "当前任务未形成仿真方案。";
  const options = awaiting ? (result.adjustment_options || []) : [];
  $("#infeasible-suggestion-block").hidden = Boolean(options.length || missing.length);
  $("#infeasible-suggestion").textContent = result.replan_limit_reached
      ? "已达到两轮调整上限，请重新发起任务。"
      : (result.questions || []).join("\n") || result.final_message || "请补充所需参数后重新计算。";
  fillCorrectionForm(result.request || {});
  renderAdjustmentOptions(options);
  const round = result.decision?.round || Math.min((result.replan_count || 0) + 1, result.max_replans || 2);
  $("#correction-title").textContent = awaiting
    ? `等待选择 · 第 ${round}/${result.max_replans || 2} 轮`
    : result.replan_limit_reached ? "已达到调整上限" : result.status === "failed" ? "查看诊断或重试任务" : "补齐任务条件后重新计算";
  $("#correction-kicker").textContent = awaiting ? "VERIFIED ALTERNATIVES" : "TASK CORRECTION";
  $("#correction-lock").innerHTML = awaiting
    ? '<i data-lucide="database-zap"></i> 演示任务状态已保存'
    : '<i data-lucide="shield-check"></i> 模型规划线固定，非实船安全核验';
  $("#correction-note").textContent = result.replan_limit_reached
    ? "已达到两轮调整上限，请重新发起任务并修改初始条件。"
    : awaiting
      ? "已复算选项仅代表本系统内计算完成。补能选项须先在现场充电，再录入实测 SOC 重新计算；也可手动改写已知任务字段。"
      : result.status === "failed"
        ? "本次未生成方案；可查看模型与工具调用记录后重试。"
      : "请只填写本次任务已知的信息；缺失字段保持空白。提交后将按演示模型重新计算。";
  $(".correction-grid").hidden = false;
  $(".correction-actions").hidden = false;
  $("#manual-correction").open = !awaiting;
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
  $("#task-review").hidden = true;
  $("#plan-comparison").hidden = true;
  $("#result-actions").hidden = true;
  state.trace = [];
  state.finalAnswer = "";
  $("#mission-title").textContent = "正在准备决策任务";
  $("#mission-subtitle").textContent = "五工具链正在计算；结果状态和计算依据将在本次任务中显示。";
  ["#metric-energy", "#metric-time", "#metric-eta", "#metric-soc"].forEach(id => { $(id).textContent = "--"; });
  $("#route-chip").textContent = "推理中";
  renderRoute([]);
  renderEnergyChart([]);
  renderTable([]);
  renderSafety([]);
  renderRecommendations({});
  renderTrace([]);
  $("#agent-decision-list").replaceChildren();
  $("#agent-decision-meta").textContent = "等待任务完成";
  $("#agent-decision-list").textContent = "模型正在计算，公开决策记录会在这里陆续显示。";
  renderProgress([], true);
  $("#reply-panel").hidden = true;
  $("#infeasible-alert").hidden = true;
  $("#infeasible-alert").setAttribute("aria-hidden", "true");
  $("#final-response").hidden = true;
  refreshIcons();
}

function operatorSummary(result, route, summary) {
  if (result.workflow_mode === "research_agent") return result.final_answer || result.final_message || "本次未形成航行方案。";
  if (result.status === "ok") {
    const origin = route[0]?.origin || result.request?.origin || "起点";
    const destination = route.at(-1)?.destination || result.request?.destination || "终点";
    const lines = [
      `软件仿真：${origin} → ${destination}`,
      `计划耗时：${formatHours(summary.total_duration?.value)}`,
      `预计到达：${formatEta(summary.eta?.value)}`,
      `预计总能耗：${formatNumber(summary.required_energy?.value, " kWh")}`,
      `预计到港电量：${formatNumber((summary.soc_final?.value ?? 0) * 100, "%")}`,
    ];
    managementAdvice(result).forEach(item => lines.push(`提示：${item}`));
    return lines.join("\n");
  }
  const failedRecord = result[result.failed_tool?.toLowerCase()] || result.tspeed || {};
  const missing = (result.missing_fields || []).map(field => MISSING_FIELD_LABELS[field] || field);
  if (!result.request?.departure_at && !missing.includes("出发时间")) missing.push("出发时间");
  const reason = missing.length && result.status !== "awaiting_choice" && result.status !== "infeasible"
    ? `缺少${missing.join("、")}。`
    : failedRecord.reason || failedRecord.payload?.reason || (result.questions || []).join("；") || "当前任务信息不完整。";
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
      <section><h4>模型与范围</h4><p>模型：${escapeHtml(modelIds.join("、") || "未知")}</p><p>${result.workflow_mode === "research_agent" ? "当前结果来自研究智能体真实工具调用，船态与电池参数采用具名软件仿真情景。" : "当前结果为 synthetic_demo 软件仿真；推进能耗系数由调研工况单点锚定，历史数据只用于覆盖范围检查，未用于实船标定。"}</p></section>
      <section><h4>采用参数来源</h4><ul>${Object.entries(evidenceSourceLabels).map(([key, label]) => sourceLine(label, sources[key])).join("")}</ul></section>
      <section><h4>航段距离来源</h4><ul>${distances || "<li>未提供航段来源。</li>"}</ul></section>
      <section><h4>模型假设</h4><ul>${assumptions.length ? assumptions.map(item => `<li>${escapeHtml(item)}</li>`).join("") : "<li>报告未提供额外假设。</li>"}</ul></section>
    </div>
    <p class="evidence-limit">未核实的逐段真实限速、船闸等待、充电可用性与实船 BMS 参数不构成已通过的约束。</p>`;
  $("#evidence-panel").hidden = false;
}

function renderTaskReview(request) {
  const values = [
    ["航线", `${request.origin || "未识别"} → ${request.destination || "未识别"}`],
    ["出发", formatEta(request.departure_at)],
    ["限时", request.max_duration_h == null ? "未设置" : formatHours(request.max_duration_h)],
    ["初始 SOC", request.soc_initial == null ? "未识别" : formatNumber(request.soc_initial * 100, "%")],
    ["载况", request.load_state || "未识别"],
    ["目标", request.optimization_goal === "min_time" ? "最短航时" : "最低总能耗"],
  ];
  $("#task-review").innerHTML = `<h3>本次计算采用的条件</h3><dl>${values.map(([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("")}</dl>`;
  $("#task-review").hidden = false;
}

function renderPlanComparison(result) {
  const plans = result.plan_alternatives || [];
  if (plans.length !== 2) {
    $("#plan-comparison").hidden = true;
    return;
  }
  const selected = result.request?.optimization_goal || "min_energy";
  const same = plans[0].duration_h === plans[1].duration_h &&
    plans[0].required_energy_kwh === plans[1].required_energy_kwh &&
    JSON.stringify(plans[0].speeds_kmh) === JSON.stringify(plans[1].speeds_kmh);
  $("#plan-comparison .comparison-note").textContent = same
    ? "当前约束下，两种目标对应同一离散候选方案；切换目标仍会重新运行完整工具链。"
    : result.workflow_mode === "research_agent"
      ? "当前只展示已复核的目标。另一目标点击后会调用真实模型与完整工具链，未计算前不显示预测数字。"
    : "候选采用同一演示模型、航时、功率和 SOC 预算筛选；选用后会重新运行完整工具链。";
  $("#comparison-options").innerHTML = plans.map(plan => {
    const active = plan.goal === selected;
    const title = plan.goal === "min_time" ? "最短航时" : "最低总能耗";
    return `<article class="comparison-card${active ? " selected" : ""}">
      <header><strong>${title}</strong><span>${active ? "当前目标" : result.workflow_mode === "research_agent" ? "待完整重算" : "候选预览"}</span></header>
      <dl><div><dt>计划耗时</dt><dd>${formatHours(plan.duration_h)}</dd></div>
      <div><dt>预计总能耗</dt><dd>${formatNumber(plan.required_energy_kwh, " kWh")}</dd></div>
      <div><dt>预计到港 SOC</dt><dd>${formatNumber(plan.soc_final == null ? null : plan.soc_final * 100, "%")}</dd></div></dl>
      <p>航段航速：${(plan.speeds_kmh || []).length ? plan.speeds_kmh.map(speed => formatNumber(speed, " km/h")).join(" · ") : "待计算"}</p>
      ${active ? "" : `<button type="button" data-plan-goal="${plan.goal}">按此目标完整重算</button>`}
    </article>`;
  }).join("");
  $("#comparison-options").classList.toggle("same-plan", same);
  $("#plan-comparison").hidden = false;
}

function historyEntries() {
  try {
    const items = JSON.parse(userStorageGet(HISTORY_KEY) || "[]");
    return Array.isArray(items)
      ? items.filter(item => item && typeof item.threadId === "string" && item.threadId)
        .sort((a, b) => Date.parse(b.updatedAt || b.createdAt) - Date.parse(a.updatedAt || a.createdAt))
      : [];
  } catch (_error) {
    return [];
  }
}

function historyDateKey(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "unknown";
  const pad = part => String(part).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function historyDateLabel(key) {
  if (key === "unknown") return "日期未知";
  const today = historyDateKey(new Date());
  const yesterday = new Date();
  yesterday.setDate(yesterday.getDate() - 1);
  const dateText = key.replaceAll("-", "/");
  if (key === today) return `今天 · ${dateText}`;
  if (key === historyDateKey(yesterday)) return `昨天 · ${dateText}`;
  return dateText;
}

function historyTitle(result, taskText) {
  const request = result.request || {};
  if (request.origin && request.destination) return `${request.origin} → ${request.destination}`;
  return taskText.replace(/\s+/g, " ").trim().slice(0, 36) || "未命名任务";
}

function renderHistory() {
  const items = historyEntries();
  $("#history-count").textContent = String(items.length);
  if (!items.length) {
    $("#history-list").innerHTML = '<p class="history-empty">还没有历史对话。完成一次计算后会自动保存。</p>';
    return;
  }
  let lastDate = "";
  $("#history-list").innerHTML = items.map(item => {
    const date = historyDateKey(item.updatedAt || item.createdAt);
    const heading = date === lastDate ? "" : `<div class="history-date">${escapeHtml(historyDateLabel(date))}</div>`;
    lastDate = date;
    const time = new Date(item.updatedAt || item.createdAt);
    const timeText = Number.isNaN(time.getTime()) ? "--:--" : time.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
    const statusText = { ok: "方案已生成", prediction_completed: "预测完成", awaiting_choice: "等待确认", infeasible: "不可行", need_clarification: "需补充", failed: "失败" }[item.status] || "已记录";
    const active = state.lastResult?.thread_id === item.threadId ? " active" : "";
    return `${heading}<button class="history-entry${active}" type="button" data-history-id="${escapeHtml(item.threadId)}" title="${escapeHtml(item.title || "未命名任务")}"><strong>${escapeHtml(item.title || "未命名任务")}</strong><span><time>${escapeHtml(timeText)}</time><small>${escapeHtml(statusText)}</small></span></button>`;
  }).join("");
}

function persistHistory(items) {
  const limited = items.slice(0, 30);
  for (let count = limited.length; count > 0; count -= 1) {
    try {
      const serialized = JSON.stringify(limited.slice(0, count));
      if (serialized.length > HISTORY_MAX_CHARS) continue;
      return userStorageSet(HISTORY_KEY, serialized);
    } catch (_error) {
      // Keep the most recent records when browser storage is nearly full.
    }
  }
  try {
    return userStorageSet(HISTORY_KEY, JSON.stringify([{ ...limited[0], result: null }]));
  } catch (_error) {
    return false;
  }
}

function rememberHistoryResult(result, taskText, { touch = true } = {}) {
  if (!result?.thread_id) return;
  const items = historyEntries();
  const previous = items.find(item => item.threadId === result.thread_id);
  const now = new Date().toISOString();
  const text = String(taskText || previous?.taskText || "").slice(0, 4000);
  const item = {
    threadId: result.thread_id,
    title: historyTitle(result, text),
    taskText: text,
    status: result.status,
    createdAt: previous?.createdAt || now,
    updatedAt: touch ? now : previous?.updatedAt || now,
    result,
  };
  if (!persistHistory([item, ...items.filter(entry => entry.threadId !== item.threadId)])) {
    $("#history-feedback").textContent = "浏览器存储空间不足，未能保存本次历史对话。";
  }
  renderHistory();
}

function importSavedPlansIntoHistory() {
  const entries = historyEntries();
  const known = new Set(entries.map(item => item.threadId));
  const imported = savedPlans().filter(item => item.result?.thread_id && !known.has(item.threadId)).map(item => {
    const parsedDate = new Date(item.createdAt);
    const timestamp = Number.isNaN(parsedDate.getTime()) ? new Date().toISOString() : parsedDate.toISOString();
    return {
      threadId: item.threadId,
      title: item.title || historyTitle(item.result, ""),
      taskText: "",
      status: item.result.status || "ok",
      createdAt: timestamp,
      updatedAt: timestamp,
      result: item.result,
    };
  });
  if (imported.length) persistHistory([...entries, ...imported]);
}

function showHistorySnapshot() {
  state.resultStale = true;
  $("#result-stale").hidden = false;
  $("#result-stale").textContent = "服务器中的任务状态已不可用；当前显示浏览器保存的只读历史快照。可复制为新任务重新计算。";
  document.querySelectorAll(".adjustment-option").forEach(button => { button.disabled = true; });
  userStorageRemove(ACTIVE_THREAD_KEY);
  userStorageRemove(ACTIVE_TASK_TEXT_KEY);
  userStorageRemove(ACTIVE_REQUEST_KEY);
  renderHistory();
}

async function openHistoryConversation(threadId) {
  const item = historyEntries().find(entry => entry.threadId === threadId);
  if (!item) return;
  if (state.running) {
    $("#history-feedback").textContent = "请等待当前计算结束后再打开历史对话。";
    return;
  }
  const requestId = ++state.requestSequence;
  const revision = state.inputRevision;
  $("#history-feedback").textContent = "正在打开历史对话…";
  let result;
  let snapshot = false;
  try {
    const response = await apiFetch(`/api/runs/${encodeURIComponent(threadId)}`);
    if (!response.ok) throw new Error("server_state_unavailable");
    result = await response.json();
    if (result.workflow_mode === "research_agent") result = await ResearchBridge.load(threadId);
  } catch (_error) {
    if (!item.result) {
      $("#history-feedback").textContent = "服务器记录已失效，且此浏览器没有可用快照。";
      return;
    }
    result = item.result;
    snapshot = true;
  }
  if (requestId !== state.requestSequence || revision !== state.inputRevision) return;
  const requestChanged = !snapshot && item.result?.request &&
    JSON.stringify(item.result.request) !== JSON.stringify(result.request);
  $("#mission-input").value = item.taskText ||
    (result.request?.origin && result.request?.destination
      ? `从${result.request.origin}到${result.request.destination}` : "");
  if ((!item.taskText || requestChanged) && requestCompleteForForm(result.request)) {
    syncCorrectionToTask(result.request);
  }
  renderResult(result);
  if (snapshot) showHistorySnapshot();
  else rememberHistoryResult(result, $("#mission-input").value, { touch: false });
  $("#history-feedback").textContent = snapshot ? "已打开浏览器保存的只读快照。" : "已打开历史对话。";
  $("#decision-status").scrollIntoView({ behavior: "smooth", block: "start" });
}

function savedPlans() {
  try {
    const items = JSON.parse(userStorageGet(SAVED_PLANS_KEY) || "[]");
    return Array.isArray(items) ? items.filter(item => item && typeof item.threadId === "string") : [];
  } catch (_error) {
    return [];
  }
}

function renderSavedPlans() {
  const items = savedPlans();
  $("#saved-count").textContent = String(items.length);
  $("#saved-plan-list").innerHTML = items.length
    ? items.map((item, index) => `<button type="button" data-saved-index="${index}"><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.createdAt)}</small></button>`).join("")
    : '<p class="saved-empty">还没有保存的方案。</p>';
}

function saveCurrentPlan() {
  const result = state.lastResult;
  if (!result?.thread_id || result.status !== "ok" || state.resultStale) {
    $("#saved-feedback").textContent = "请先完成当前任务的计算。";
    return;
  }
  const items = savedPlans().filter(item => item.threadId !== result.thread_id);
  const request = result.request || {};
  items.unshift({
    threadId: result.thread_id,
    result,
    title: `${request.origin || "起点"} → ${request.destination || "终点"} · ${request.optimization_goal === "min_time" ? "最短航时" : "最低总能耗"}`,
    createdAt: new Date().toLocaleString("zh-CN"),
  });
  try {
    userStorageSet(SAVED_PLANS_KEY, JSON.stringify(items.slice(0, 20)));
    $("#saved-feedback").textContent = "方案快照已保存到此浏览器，可从下方打开。";
    renderSavedPlans();
  } catch (_error) {
    $("#saved-feedback").textContent = "浏览器存储不可用，未能保存。";
  }
}

async function openSavedPlan(index) {
  const item = savedPlans()[index];
  if (!item || state.running) return;
  if (item.result?.status === "ok") {
    ++state.requestSequence;
    syncCorrectionToTask(item.result.request);
    renderResult(item.result);
    $("#saved-open-feedback").textContent = "已打开保存时的仿真快照；复用请修改条件后重新计算。";
    $("#decision-status").scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  const requestId = ++state.requestSequence;
  const revision = state.inputRevision;
  $("#saved-open-feedback").textContent = "正在读取已保存方案…";
  try {
    const response = await apiFetch(`/api/runs/${encodeURIComponent(item.threadId)}`);
    const result = await response.json();
    if (requestId !== state.requestSequence || revision !== state.inputRevision) return;
    if (!response.ok) throw new Error(result.error || "方案已失效");
    const opened = result.workflow_mode === "research_agent" ? await ResearchBridge.load(item.threadId) : result;
    if (requestId !== state.requestSequence || revision !== state.inputRevision) return;
    if (opened.request) syncCorrectionToTask(opened.request);
    renderResult(opened);
    $("#saved-open-feedback").textContent = "已打开方案；输入框已同步为该航次。";
    $("#decision-status").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    $("#saved-open-feedback").textContent = `无法打开：${error.message}`;
  }
}

function markResultStale() {
  state.inputRevision += 1;
  if (state.running) {
    state.resultStale = true;
    $("#result-stale").hidden = false;
    $("#result-stale").textContent = "输入已修改；当前计算结果不会覆盖新任务，请重新发送。";
    updateVesselView("idle", { message: "输入已修改，回放已停止" });
    return;
  }
  if (!state.lastResult) return;
  state.resultStale = true;
  $("#result-stale").hidden = false;
  $("#result-stale").textContent = state.lastResult.status === "awaiting_choice"
    ? "输入已修改；下方待确认选项仍属于上一次任务。请重新计算后再选择。"
    : "输入已修改；当前结果对应之前的任务，请重新计算以更新方案。";
  updateVesselView("idle", { message: "输入已修改，回放已停止" });
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
  renderQuickRoutePreview();
  writeQuickTask({ markChanged: false });
}

function primeActiveTaskFromStorage() {
  if (!userStorageGet(ACTIVE_THREAD_KEY)) return;
  try {
    const request = JSON.parse(userStorageGet(ACTIVE_REQUEST_KEY) || "null");
    if (requestCompleteForForm(request)) syncCorrectionToTask(request);
    else if (request) syncPartialRequestToInputs(request);
  } catch (_error) {
    // An older saved task can still be restored from the server.
  }
  const text = userStorageGet(ACTIVE_TASK_TEXT_KEY);
  if (text) $("#mission-input").value = text;
  $("#mission-subtitle").textContent = "正在恢复上次任务状态…";
}

function renderRuntimeControls(result) {
  const runtime = result?.research_runtime || null;
  const wrap = $("#task-controls");
  const stage = $("#task-stage");
  const cancel = $("#cancel-task");
  const retry = $("#retry-task");
  if (!wrap) return;
  if (!runtime) {
    wrap.hidden = true;
    if (stage) stage.textContent = "";
    if (cancel) cancel.hidden = true;
    if (retry) retry.hidden = true;
    return;
  }
  wrap.hidden = false;
  if (stage) {
    const attempts = runtime.recovery_attempts ? ` · 已恢复 ${runtime.recovery_attempts} 次` : "";
    stage.textContent = `${runtime.stage || ""}${runtime.cancel_requested ? " · 正在取消" : ""}${attempts}`;
  }
  if (cancel) { cancel.hidden = !runtime.can_cancel; cancel.dataset.threadId = result.thread_id || ""; }
  if (retry) { retry.hidden = !runtime.can_retry; retry.dataset.threadId = result.thread_id || ""; }
}

function renderResult(result) {
  if (requestCompleteForForm(result.request)) {
    syncCorrectionToTask(result.request, { rewriteDescription: false });
  } else if (result.request) {
    syncPartialRequestToInputs(result.request);
  }
  state.lastResult = result;
  state.resultStale = false;
  $("#result-stale").hidden = true;
  const okay = result.status === "ok";
  const predicted = result.status === "prediction_completed";
  const infeasible = result.status === "infeasible";
  const awaiting = result.status === "awaiting_choice";
  if (okay || predicted) {
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

  updateStatus(okay || predicted ? "success" : awaiting ? "running" : "error", predicted ? "能耗预测完成" : okay ? "仿真方案已生成" : awaiting ? "等待确认" : infeasible ? "航次不可行" : result.status === "failed" ? "执行未完成" : "需要补充");
  $("#mission-title").textContent = predicted ? "独立能耗预测完成" : route.length ? `${route[0].origin} → ${route.at(-1).destination}` : (okay ? "仿真方案已生成" : "任务未完成");
  $("#mission-subtitle").textContent = predicted ? "工具已计算功率与能量；来源、后端和诊断见下方。此结果不代表航次可行。" : okay
    ? "以下为软件仿真结果，仅供演示和方法验证，不构成实船航行指令。"
    : awaiting
      ? "当前任务不满足执行条件，请选择下方调整方案。"
      : infeasible
        ? "本次任务保留不可行结论，未生成航行方案。"
        : result.status === "failed"
          ? "模型或工具执行未完成，详情见答复和调用记录。"
        : "请补充或修正任务信息后重新计算。";
  renderProgress(result.trace || []);
  renderRoute(route, result.tdata?.payload?.route_id || "航线待识别");
  renderModel(result);
  renderTrace(result.trace || []);
  if (result.workflow_mode === "research_agent") ResearchBridge.renderDetails(result);
  renderRuntimeControls(result);
  renderRecommendations(result);

  if (okay) {
    $("#metric-energy").textContent = formatNumber(summary.required_energy?.value, " kWh");
    $("#metric-time").textContent = formatHours(summary.total_duration?.value);
    $("#metric-eta").textContent = formatEta(summary.eta?.value);
    $("#metric-soc").textContent = formatNumber((summary.soc_final?.value ?? 0) * 100, "%");
    if (result.workflow_mode === "research_agent" && result.research_soc?.battery_1 != null && result.research_soc?.battery_2 != null) {
      $("#metric-soc").textContent = `${formatNumber(result.research_soc.battery_1 * 100, "%")} / ${formatNumber(result.research_soc.battery_2 * 100, "%")}`;
    }
    renderEnergyChart(segments, summary);
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
    renderTaskReview(result.request || {});
    renderPlanComparison(result);
    $("#result-actions").hidden = false;
  } else if (predicted) {
    $("#metric-energy").textContent = formatNumber(result.research_metrics?.motor_energy_kwh?.value, " kWh（电机侧）");
    $("#metric-time").textContent = formatHours((result.research_metrics?.duration_s?.value ?? 0) / 3600);
    $("#metric-eta").textContent = "--";
    $("#metric-soc").textContent = "--";
    ["#evidence-panel", "#result-scope-banner", "#task-review", "#plan-comparison", "#result-actions"].forEach(id => { $(id).hidden = true; });
    renderEnergyChart([]); renderTable([]); renderSafety([], []);
    updateVesselView("incomplete", { message: "独立预测已完成，未生成航行方案或回放。" });
  } else {
    $("#evidence-panel").hidden = true;
    $("#result-scope-banner").hidden = true;
    $("#task-review").hidden = true;
    $("#plan-comparison").hidden = true;
    $("#result-actions").hidden = true;
    renderEnergyChart([]);
    renderTable([]);
    renderSafety(checks, warnings);
    showInfeasible(result);
    updateVesselView((infeasible || awaiting) ? "infeasible" : result.status === "failed" ? "failed" : "incomplete", {
      message: (infeasible || awaiting) ? "当前航次不可行，回放已停止" : result.status === "failed" ? "执行未完成，回放已停止" : "任务信息需要补充，回放已停止",
    });
  }

  if (result.thread_id) {
    try {
      userStorageSet(ACTIVE_THREAD_KEY, result.thread_id);
      userStorageSet(ACTIVE_TASK_TEXT_KEY, $("#mission-input").value);
      userStorageSet(ACTIVE_REQUEST_KEY, JSON.stringify(result.request || {}));
    } catch (_error) {
      $("#history-feedback").textContent = "浏览器存储空间不足，刷新后可能无法恢复当前任务。";
    }
  } else {
    userStorageRemove(ACTIVE_THREAD_KEY);
    userStorageRemove(ACTIVE_TASK_TEXT_KEY);
    userStorageRemove(ACTIVE_REQUEST_KEY);
  }

  state.finalAnswer = operatorSummary(result, route, summary);
  $("#final-answer").textContent = state.finalAnswer;
  $("#final-response").hidden = !state.finalAnswer;
  renderHistory();
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
  const response = await apiFetch(path, {
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
  const submittedText = payloadOverride ? ResearchBridge.structuredText(payloadOverride) : message;
  if ((!message && !payloadOverride) || state.running) {
    if (!message && !payloadOverride) $("#mission-input").focus();
    return;
  }
  const pendingResearch = !payloadOverride && state.lastResult?.research_pending ? state.lastResult : null;
  // A new submission supersedes any older decision awaiting confirmation.
  userStorageRemove(ACTIVE_THREAD_KEY);
  userStorageRemove(ACTIVE_TASK_TEXT_KEY);
  userStorageRemove(ACTIVE_REQUEST_KEY);
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
  updateStatus("running", "计算中");
  updateVesselView("running", { message: "五工具链正在计算航行方案" });
  resetResult();
  try {
    const onProgress = (task, events) => {
      if (!isCurrentRequest()) return;
      const trace = researchToolTrace(events);
      renderProgress(trace, true);
      renderTrace(trace);
      if (task?.thread_id) {
        state.activeRequest.taskId = task.thread_id;
        void ResearchBridge.renderDetails({ thread_id: task.thread_id, research_events: events }, { live: true });
      }
      if (task?.status === "reconnecting") {
        $("#mission-subtitle").textContent = "服务暂时不可用，正在等待恢复；任务标识已保留。";
        return;
      }
      const stage = task?.status === "queued" ? "排队中"
        : task?.status === "running" ? `计算中${task?.phase ? ` · ${task.phase}` : ""}`
        : task?.status === "waiting_human" ? "等待补充信息" : "";
      $("#task-controls").hidden = false;
      $("#task-stage").textContent = task?.recovery_attempts ? `${stage} · 已恢复 ${task.recovery_attempts} 次` : stage;
      $("#cancel-task").hidden = !["queued", "running", "waiting_human"].includes(task?.status);
      $("#retry-task").hidden = true;
      $("#mission-subtitle").textContent = `真实模型与工具正在执行 · ${events.filter(event => event.type === "model_completed").length} 次模型响应 · ${trace.length} 次工具完成`;
    };
    const result = pendingResearch
      ? await ResearchBridge.resume(pendingResearch, submittedText, onProgress, controller.signal)
      : await ResearchBridge.submit(submittedText, onProgress, controller.signal);
    if (!isCurrentRequest()) return;
    if (submittedRevision !== state.inputRevision) {
      discardStaleResponse();
      return;
    }
    renderResult(result);
    rememberHistoryResult(result, submittedText);
  } catch (error) {
    if (!isCurrentRequest() || error.name === "AbortError") return;
    if (error.name === "TaskStillRunning") {
      // 长任务超出页面等待窗口：保留任务标识，刷新后可继续查看同一任务，
      // 不把“仍在计算”显示成执行失败，也不重复发布结果。
      updateStatus("running", "仍在计算");
      $("#mission-title").textContent = "任务仍在后台计算";
      $("#mission-subtitle").textContent = "服务仍在执行同一任务；刷新页面可继续查看，结果只会发布一次。";
      $("#task-controls").hidden = false;
      $("#task-stage").textContent = "仍在计算 · 可刷新继续查看";
      $("#cancel-task").hidden = false;
      $("#cancel-task").dataset.threadId = error.taskId || "";
      return;
    }
    updateStatus("error", "计算失败");
    $("#mission-title").textContent = "任务执行失败";
    $("#mission-subtitle").textContent = error.message;
    $("#model-mode").textContent = "调用失败";
    $("#reply-answer").textContent = "任务处理失败，请检查服务状态后重试。";
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

const QUICK_FIELD_IDS = ["start-select", "end-select", "time-input", "adv-soc", "adv-load", "departure-input", "optimization-goal"];
let quickDialogSnapshot = null;

function snapshotQuickFields() {
  return Object.fromEntries(QUICK_FIELD_IDS.map(id => [id, $(`#${id}`).value]));
}

function closeQuickDialog(apply = false) {
  if (!apply && quickDialogSnapshot) {
    QUICK_FIELD_IDS.forEach(id => {
      $(`#${id}`).value = quickDialogSnapshot.fields[id];
    });
    renderQuickRoutePreview();
    state.quickDirty = quickDialogSnapshot.quickDirty;
  }
  quickDialogSnapshot = null;
  $("#quick-error").textContent = "";
  $("#quick-dialog").close();
  $("#quick-open").focus();
}

function openQuickDialog() {
  if (state.running) return;
  quickDialogSnapshot = { fields: snapshotQuickFields(), quickDirty: state.quickDirty };
  $("#quick-error").textContent = "";
  renderQuickRoutePreview();
  $("#quick-dialog").showModal();
  $("#start-select").focus();
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
    const goalText = $("#optimization-goal").value === "min_time" ? "，优先最短航时" : "";
    $("#mission-input").value = `从${origin}到${destination}，${departure}出发，SOC${soc}%，${load}，${hours}小时内到达${goalText}`;
    state.quickDirty = false;
    $("#quick-error").textContent = "";
    if (markChanged) markResultStale();
    return true;
  } catch (error) {
    state.quickDirty = true;
    if (!silent) {
      if ($("#quick-dialog").open) $("#quick-error").textContent = error.message;
      else $("#mission-subtitle").textContent = error.message;
      $(error.selector)?.focus();
    }
    return false;
  }
}

function markQuickInputChanged() {
  state.quickDirty = true;
  $("#quick-error").textContent = "";
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
    const response = await apiFetch("/healthz");
    if (!response.ok) throw new Error("health unavailable");
    const health = await response.json();
    $("#service-label").textContent = health.status === "ok" ? "航次规划服务在线" : "航次规划服务异常";
    $("#service-dot").className = health.status === "ok" ? "status-dot" : "status-dot offline";
    $("#model-availability").textContent = "提交后显示本次工具执行状态、计算依据和结果限制。";
    await loadVoiceStatus();
  } catch (_error) {
    $("#service-label").textContent = "航次规划服务暂不可用";
    $("#service-dot").className = "status-dot offline";
    $("#model-availability").textContent = "无法连接规划服务，请稍后重试。";
    $("#model-availability").className = "model-availability warning";
    state.speechAvailable = false;
    $("#voice-button").disabled = true;
    $("#voice-copy").textContent = "语音服务暂不可用";
  }
}

async function loadVoiceStatus() {
  try {
    const response = await apiFetch("/api/voice/status");
    const status = response.ok ? await response.json() : {};
    state.speechAvailable = status.available === true;
    $("#voice-button").disabled = !state.speechAvailable;
    $("#voice-copy").textContent = state.speechAvailable ? "点击麦克风语音输入" : "语音服务待配置";
  } catch (_error) {
    state.speechAvailable = false;
    $("#voice-button").disabled = true;
    $("#voice-copy").textContent = "语音服务暂不可用";
  }
}

async function initializeAuthenticatedWorkspace() {
  if (!state.authUser) return;
  if (!state.clockTimer) {
    tickClock();
    state.clockTimer = setInterval(tickClock, 1000);
  }
  initializeQuickRouteNodes();
  initializeTaskDefaults();
  primeActiveTaskFromStorage();
  if (!state.workspaceInitialized) {
    bindControls();
    state.workspaceInitialized = true;
  }
  renderSavedPlans();
  importSavedPlansIntoHistory();
  renderHistory();
  refreshIcons();
  await loadHealth();
  if (state.authUser?.role === "admin") await loadAdminDiagnostics();
  else {
    $("#admin-diagnostics").hidden = true;
    $("#admin-diagnostics").textContent = "";
  }
  if (state.authUser) await restorePendingRun();
  window.setTimeout(() => {
    const modelLoader = $("#vessel-loading");
    if (!window.Ship3DReady && modelLoader && !modelLoader.hidden) {
      modelLoader.classList.add("error");
      modelLoader.lastChild.textContent = "三维组件未能加载，请检查网络后刷新页面";
    }
  }, 15000);
}

async function restorePendingRun() {
  const threadId = userStorageGet(ACTIVE_THREAD_KEY);
  if (!threadId) return;
  const revision = state.inputRevision;
  const requestSequence = state.requestSequence;
  try {
    const response = await apiFetch(`/api/runs/${encodeURIComponent(threadId)}`);
    const result = await response.json();
    if (userStorageGet(ACTIVE_THREAD_KEY) !== threadId ||
        state.inputRevision !== revision || state.requestSequence !== requestSequence) return;
    if (!response.ok) {
      if (response.status === 400 || response.status === 404) {
        const savedHistory = historyEntries().find(item => item.threadId === threadId);
        if (savedHistory?.result) {
          $("#mission-input").value = savedHistory.taskText || "";
          if (!savedHistory.taskText && requestCompleteForForm(savedHistory.result.request)) {
            syncCorrectionToTask(savedHistory.result.request);
          }
          renderResult(savedHistory.result);
          showHistorySnapshot();
          $("#history-feedback").textContent = "上次任务的服务器状态已失效，已恢复浏览器快照。";
          return;
        }
        userStorageRemove(ACTIVE_THREAD_KEY);
        userStorageRemove(ACTIVE_TASK_TEXT_KEY);
        userStorageRemove(ACTIVE_REQUEST_KEY);
        initializeTaskDefaults();
        $("#mission-subtitle").textContent = "上次任务状态已失效，请重新发起任务。";
      } else {
        $("#mission-subtitle").textContent = "暂时无法恢复上次任务，恢复标识已保留；请稍后刷新重试。";
      }
      return;
    }
    const restored = result.workflow_mode === "research_agent"
      ? await ResearchBridge.load(threadId, (task, events) => {
        $("#mission-subtitle").textContent = `正在恢复任务 · ${events.filter(event => event.type === "tool_completed").length} 次工具完成`;
      }) : result;
    if (userStorageGet(ACTIVE_THREAD_KEY) !== threadId ||
        state.inputRevision !== revision || state.requestSequence !== requestSequence) return;
    const storedText = userStorageGet(ACTIVE_TASK_TEXT_KEY);
    let storedRequest = null;
    try { storedRequest = JSON.parse(userStorageGet(ACTIVE_REQUEST_KEY) || "null"); } catch (_error) { /* Ignore an old invalid request cache. */ }
    const requestChanged = storedRequest && JSON.stringify(storedRequest) !== JSON.stringify(restored.request);
    if (storedText && !requestChanged) $("#mission-input").value = storedText;
    else if (requestCompleteForForm(restored.request)) syncCorrectionToTask(restored.request);
    else $("#mission-input").value = restored.request?.origin && restored.request?.destination
      ? `从${restored.request.origin}到${restored.request.destination}` : "";
    renderResult(restored);
    rememberHistoryResult(restored, $("#mission-input").value, { touch: false });
    if (restored.status === "awaiting_choice") {
      $("#correction-feedback").textContent = "已恢复上次等待确认的任务。";
    }
  } catch (error) {
    if (error.name === "TaskStillRunning") {
      $("#mission-subtitle").textContent = "上次任务仍在后台计算；刷新页面可继续查看，结果只会发布一次。";
      $("#task-controls").hidden = false;
      $("#task-stage").textContent = "仍在计算";
      $("#cancel-task").hidden = false;
      $("#cancel-task").dataset.threadId = threadId;
      return;
    }
    $("#mission-subtitle").textContent = "网络暂时不可用，恢复标识已保留；请稍后刷新重试。";
  }
}

async function cancelTaskById(threadId) {
  if (!threadId) return;
  try {
    $("#task-stage").textContent = "正在取消…";
    await ResearchBridge.cancel(threadId);
    const result = await ResearchBridge.load(threadId, task => {
      $("#task-stage").textContent = `正在取消 · ${task.status || ""}`;
    });
    renderResult(result);
  } catch (error) {
    $("#mission-subtitle").textContent = `取消失败：${error.message}`;
  }
}

async function retryTaskById(threadId) {
  if (!threadId) return;
  state.running = true;
  $("#run-button").disabled = true;
  try {
    $("#task-stage").textContent = "正在重试…";
    const result = await ResearchBridge.retry(threadId, task => {
      $("#task-stage").textContent = `重试中 · ${task.status || ""}${task.phase ? ` · ${task.phase}` : ""}`;
    });
    renderResult(result);
    rememberHistoryResult(result, $("#mission-input").value, { touch: false });
  } catch (error) {
    $("#mission-subtitle").textContent = `重试失败：${error.message}`;
  } finally {
    state.running = false;
    $("#run-button").disabled = false;
  }
}

function bindControls() {
  $("#segment-table-scroll").addEventListener("keydown", handleTableScrollKeydown);
  $("#history-list").addEventListener("click", event => {
    const button = event.target.closest("[data-history-id]");
    if (button) openHistoryConversation(button.dataset.historyId);
  });
  $("#quick-open").addEventListener("click", openQuickDialog);
  $("#quick-close").addEventListener("click", () => closeQuickDialog());
  $("#quick-cancel").addEventListener("click", () => closeQuickDialog());
  $("#quick-dialog").addEventListener("cancel", event => {
    event.preventDefault();
    closeQuickDialog();
  });
  $("#mission-input").addEventListener("input", () => { state.quickDirty = false; markResultStale(); });
  ["#start-select", "#end-select", "#departure-input", "#adv-soc", "#adv-load", "#optimization-goal"].forEach(selector => {
    $(selector).addEventListener("input", markQuickInputChanged);
    $(selector).addEventListener("change", markQuickInputChanged);
  });
  ["#start-select", "#end-select"].forEach(selector => {
    $(selector).addEventListener("input", renderQuickRoutePreview);
    $(selector).addEventListener("change", renderQuickRoutePreview);
  });
  ["#correction-origin", "#correction-destination", "#correction-departure", "#correction-duration", "#correction-soc", "#correction-load", "#correction-goal"].forEach(selector => {
    $(selector).addEventListener("input", markResultStale);
    $(selector).addEventListener("change", markResultStale);
  });
  $("#time-input").addEventListener("input", markQuickInputChanged);
  $("#time-minus").addEventListener("click", () => {
    $("#time-input").value = Math.max(0.5, Number($("#time-input").value || 6) - 0.5);
    markQuickInputChanged();
  });
  $("#time-plus").addEventListener("click", () => {
    $("#time-input").value = Math.min(72, Number($("#time-input").value || 6) + 0.5);
    markQuickInputChanged();
  });
  $("#quick-fill").addEventListener("click", () => {
    if (writeQuickTask()) closeQuickDialog(true);
  });
  $("#save-plan").addEventListener("click", saveCurrentPlan);
  $("#copy-task").addEventListener("click", () => {
    if (!state.lastResult?.request) return;
    syncCorrectionToTask(state.lastResult.request);
    markResultStale();
    $("#mission-input").focus();
  });
  $("#comparison-options").addEventListener("click", event => {
    const button = event.target.closest("[data-plan-goal]");
    if (!button || state.running || state.resultStale || !state.lastResult?.request) return;
    const payload = { ...state.lastResult.request, optimization_goal: button.dataset.planGoal };
    syncCorrectionToTask(payload);
    runInference(payload);
  });
  $("#saved-plan-list").addEventListener("click", event => {
    const button = event.target.closest("[data-saved-index]");
    if (button) openSavedPlan(Number(button.dataset.savedIndex));
  });
  $("#run-button").addEventListener("click", () => runInference());
  $("#cancel-task").addEventListener("click", () => cancelTaskById($("#cancel-task").dataset.threadId));
  $("#retry-task").addEventListener("click", () => retryTaskById($("#retry-task").dataset.threadId));
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
    userStorageRemove(ACTIVE_THREAD_KEY);
    userStorageRemove(ACTIVE_TASK_TEXT_KEY);
    userStorageRemove(ACTIVE_REQUEST_KEY);
    resetResult();
    updateStatus("idle", "待命");
    $("#mission-title").textContent = "等待航行任务";
    $("#mission-subtitle").textContent = "输入自然语言需求，或打开左侧快捷输入。";
    updateVesselView("idle");
    renderHistory();
  });
  document.querySelectorAll("[data-sample]").forEach(button => button.addEventListener("click", () => {
    const kind = button.dataset.sample;
    $("#start-select").value = "平顶山港";
    $("#end-select").value = "军李船闸";
    renderQuickRoutePreview();
    $("#departure-input").value = localDateTime();
    $("#time-input").value = kind === "time" ? 1 : 6;
    $("#adv-soc").value = kind === "soc" ? 31 : 85;
    $("#adv-load").value = "半载";
    $("#optimization-goal").value = "min_energy";
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
  bindAuthControls();
  refreshIcons();
  await restoreLoginSession();
});

window.addEventListener("beforeunload", () => {
  if (!state.voice) return;
  state.voice.capturing = false;
  state.voice.microphone.getTracks().forEach(track => track.stop());
});
