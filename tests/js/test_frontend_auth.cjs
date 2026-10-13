const fs = require("fs");
const path = require("path");
const vm = require("vm");

function element() {
  return {
    value: "", textContent: "", innerHTML: "", className: "", hidden: false,
    disabled: false, title: "", classList: { add() {}, remove() {}, toggle() {} },
    setAttribute() {}, addEventListener() {}, focus() {},
  };
}

const nodes = new Map();
const getNode = selector => {
  if (!nodes.has(selector)) nodes.set(selector, element());
  return nodes.get(selector);
};
const storage = new Map();
let requestSeen = null;
let reloadCount = 0;
const context = vm.createContext({
  console, Intl, Date, Number, String, Math, JSON, Set, Map, Promise,
  AbortController, encodeURIComponent,
  document: {
    querySelector: getNode, querySelectorAll: () => [],
    dispatchEvent() {}, addEventListener() {}, activeElement: null,
    createElement: () => ({ relList: { supports: () => true } }),
  },
  window: { addEventListener() {}, location: { reload() { reloadCount += 1; } } },
  storage,
  localStorage: {
    getItem: key => storage.has(key) ? storage.get(key) : null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: key => storage.delete(key),
  },
  navigator: {},
  CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
  Event: class { constructor(type) { this.type = type; } },
  setInterval() {}, setTimeout() {},
  fetch: async (path, options) => {
    requestSeen = { path, options };
    return { ok: true, status: 200, json: async () => ({ status: "ok" }) };
  },
});
const appPath = path.join(__dirname, "..", "..", "ui", "frontend", "app.js");
vm.runInContext(fs.readFileSync(appPath, "utf8"), context);

async function run() {
  vm.runInContext(`
    acceptSession({
      user: { id: "user-a", username: "crew", display_name: "值班员", role: "user" },
      csrf_token: "csrf-a", session_expires_at: "2026-10-14T00:00:00Z",
    });
    userStorageSet(HISTORY_KEY, "user-a-history");
    if (!$("#login-screen").hidden || $("#app-shell").hidden) throw new Error("login did not enter workspace");
    if ($("#session-role").textContent !== "普通用户") throw new Error("user role label missing");
    if (!$ ("#admin-diagnostics").hidden) throw new Error("admin panel exposed to user");
    renderAdminDiagnostics({llm_mode:"enabled"});
    if (!$ ("#admin-diagnostics").hidden) throw new Error("user rendered admin diagnostics");
  `, context);

  context.fetch = async (path, options) => {
    requestSeen = { path, options };
    return { ok: true, status: 200, json: async () => ({}) };
  };
  await vm.runInContext("loadAdminDiagnostics()", context);
  if (requestSeen?.path === "/api/admin/system-status") throw new Error("ordinary user requested admin diagnostics");
  if (vm.runInContext("state.authUser.role", context) !== "user") throw new Error("ordinary-user role changed");

  await vm.runInContext(`apiFetch("/api/run", {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"})`, context);
  if (requestSeen.options.credentials !== "same-origin") throw new Error("session cookie not included");
  if (requestSeen.options.headers["X-CSRF-Token"] !== "csrf-a") throw new Error("write request missing CSRF token");

  vm.runInContext(`
    acceptSession({
      user: { id: "admin-b", username: "chief", display_name: "管理员", role: "admin" },
      csrf_token: "csrf-b", session_expires_at: "2026-10-14T00:00:00Z",
    });
    if (storage.get("ship-agent.user.user-a.conversation-history")) throw new Error("previous user's cache survived account switch");
    renderAdminDiagnostics({
      release:{app_version:"1.2.3", baseline_id:"release", parameter_profile:"demo", parameter_approved_at:"2026-10-01"},
      llm_mode:"enabled", checkpoint_backend:"sqlite", checkpoint_ready:true, speech_mode:"unconfigured",
    });
    if ($("#admin-diagnostics").hidden || !$("#admin-diagnostics").innerHTML.includes("1.2.3")) throw new Error("admin diagnostics unavailable");
    userStorageSet(ACTIVE_THREAD_KEY, "private-thread");
  `, context);

  let adminStatusRequested = false;
  context.fetch = async (path, options) => {
    requestSeen = { path, options };
    if (path === "/api/admin/system-status") adminStatusRequested = true;
    return {
      ok: true, status: 200,
      json: async () => ({ status:"ok", llm_mode:"enabled", checkpoint_backend:"sqlite",
        checkpoint_ready:true, speech_mode:"unconfigured", release:{app_version:"1.2.3"} }),
    };
  };
  await vm.runInContext("loadAdminDiagnostics()", context);
  if (!adminStatusRequested) throw new Error("admin session did not request admin diagnostics");

  context.fetch = async (path, options) => {
    requestSeen = { path, options };
    return {
      ok: false, status: 401,
      clone: () => ({ json: async () => ({ status: "failed", error: "auth_required", message: "请登录后继续" }) }),
    };
  };
  await vm.runInContext(`apiFetch("/api/runs/private-thread")`, context);
  const expired = vm.runInContext(`({ user: state.authUser, loginHidden: $("#login-screen").hidden,
    appHidden: $("#app-shell").hidden, rolePanelHidden: $("#admin-diagnostics").hidden })`, context);
  if (expired.user !== null || expired.loginHidden || !expired.appHidden || !expired.rolePanelHidden) {
    throw new Error("expired session did not clear authenticated workspace");
  }
  if (reloadCount !== 1) throw new Error("expired session kept the prior account's rendered workspace");
  if (storage.has("ship-agent.user.admin-b.active-thread")) throw new Error("logout/session expiry kept active task cache");

  context.fetch = async (path, options) => {
    requestSeen = { path, options };
    return { ok: true, status: 200, json: async () => ({}) };
  };
  await vm.runInContext(`apiFetch("/api/auth/login", {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"})`, context);
  if (requestSeen.options.headers["X-CSRF-Token"]) throw new Error("login must not require an existing CSRF token");
  if (requestSeen.options.credentials !== "same-origin") throw new Error("login omitted same-origin cookie policy");
}

run().then(() => process.stdout.write("ok\n"));
