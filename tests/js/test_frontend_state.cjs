const fs = require("fs");
const path = require("path");
const vm = require("vm");

function element() {
  return {
    value: "", textContent: "", innerHTML: "", className: "", hidden: false,
    disabled: false, title: "", options: [], lastChild: { textContent: "" },
    classList: { add() {}, remove() {}, toggle() {} },
    setAttribute() {}, addEventListener() {}, focus() {},
    querySelectorAll() { return []; }, querySelector() { return element(); },
  };
}

const nodes = new Map();
const getNode = selector => {
  if (!nodes.has(selector)) nodes.set(selector, element());
  return nodes.get(selector);
};
const storage = new Map();
const context = vm.createContext({
  console, Intl, Date, Number, String, Math, JSON, Set, Map, Promise,
  AbortController, encodeURIComponent,
  document: {
    querySelector: getNode, querySelectorAll: () => [],
    dispatchEvent() {}, addEventListener() {}, activeElement: null,
  },
  window: { addEventListener() {} },
  localStorage: {
    getItem: key => storage.get(key) || null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: key => storage.delete(key),
  },
  navigator: {},
  CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
  Event: class { constructor(type) { this.type = type; } },
  setInterval() {}, setTimeout() {},
  fetch: async () => { throw new Error("fetch not configured"); },
});
const appPath = path.join(__dirname, "..", "..", "ui", "frontend", "app.js");
vm.runInContext(fs.readFileSync(appPath, "utf8"), context);

async function run() {
  vm.runInContext(`
    $("#start-select").value = "平顶山港";
    $("#end-select").value = "军李船闸";
    $("#departure-input").value = "2026-10-03T09:00";
    $("#time-input").value = "6";
    $("#adv-time").value = "6";
    $("#adv-soc").value = "31";
    $("#adv-load").value = "半载";
    if (!writeQuickTask({ markChanged: false })) throw new Error("valid quick task rejected");
    if (!$("#mission-input").value.includes("SOC31%")) throw new Error("quick SOC not synchronized");
    const priorText = $("#mission-input").value;
    $("#adv-soc").value = "";
    if (writeQuickTask({ silent: true, markChanged: false })) throw new Error("empty SOC accepted");
    if ($("#mission-input").value !== priorText) throw new Error("empty SOC silently rewrote task");
    if (!state.quickDirty) throw new Error("invalid quick form not marked dirty");
  `, context);

  let resolveRun;
  context.fetch = () => new Promise(resolve => { resolveRun = resolve; });
  vm.runInContext(`
    state.quickDirty = false;
    $("#mission-input").value = "从平顶山港到军李船闸，SOC85%，半载，6小时内到达";
    globalThis.pendingRun = runInference();
  `, context);
  vm.runInContext("markResultStale();", context);
  resolveRun({ ok: true, json: async () => ({ status: "ok", marker: "old-result" }) });
  await context.pendingRun;
  const stale = vm.runInContext(`({
    result: state.lastResult,
    title: $("#mission-title").textContent,
    stale: state.resultStale,
  })`, context);
  if (stale.result !== null || stale.title !== "当前计算结果已忽略" || !stale.stale) {
    throw new Error("stale response changed the current task");
  }

  let resolveResume;
  context.fetch = () => new Promise(resolve => { resolveResume = resolve; });
  vm.runInContext(`
    state.lastResult = {
      status: "awaiting_choice", thread_id: "thread-1",
      decision: { decision_id: "decision-1" },
      adjustment_options: [{
        option_id: "option-1", direction: "accept_late",
        verified: true, modification: { max_duration_h: 8 },
      }],
    };
    state.resultStale = false;
    globalThis.pendingResume = applyAdjustmentOption(0);
  `, context);
  vm.runInContext("markResultStale();", context);
  resolveResume({ ok: true, json: async () => ({ status: "ok", marker: "old-resume-result" }) });
  await context.pendingResume;
  const resumed = vm.runInContext("state.lastResult", context);
  if (resumed.marker === "old-resume-result") throw new Error("stale resume response changed the current task");

  let resolveCancelled;
  context.fetch = () => new Promise(resolve => { resolveCancelled = resolve; });
  vm.runInContext(`
    state.lastResult = { marker: "before-cancel" };
    state.resultStale = false;
    $("#mission-input").value = "从平顶山港到军李船闸，SOC85%，半载，6小时内到达";
    globalThis.cancelledRun = runInference();
    cancelActiveRequest();
  `, context);
  resolveCancelled({ ok: true, json: async () => ({ status: "ok", marker: "cancelled-result" }) });
  await context.cancelledRun;
  const cancelled = vm.runInContext("state.lastResult", context);
  if (cancelled?.marker === "cancelled-result" || vm.runInContext("state.running", context)) {
    throw new Error("cancelled request changed the current task");
  }

  storage.set("ship-agent.active-thread", "temporary-thread");
  context.fetch = async () => { throw new Error("temporary network failure"); };
  await vm.runInContext("restorePendingRun()", context);
  if (!storage.has("ship-agent.active-thread")) throw new Error("network error removed recovery id");

  context.fetch = async () => ({ ok: false, status: 500, json: async () => ({ error: "workflow_failed" }) });
  await vm.runInContext("restorePendingRun()", context);
  if (!storage.has("ship-agent.active-thread")) throw new Error("server error removed recovery id");

  context.fetch = async () => ({ ok: false, status: 404, json: async () => ({ error: "workflow_not_found" }) });
  await vm.runInContext("restorePendingRun()", context);
  if (storage.has("ship-agent.active-thread")) throw new Error("confirmed missing task kept recovery id");
}

run().then(() => process.stdout.write("ok\n"));
