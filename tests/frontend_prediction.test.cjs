const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');

test('environment evidence expands inside existing decision panel with both speed meanings', async () => {
  const nodes = new Map();
  const context = vm.createContext({
    $: id => { if (!nodes.has(id)) nodes.set(id, {}); return nodes.get(id); },
    escapeHtml: String, formatNumber: value => value == null ? '--' : Number(value).toFixed(3),
    state: { lastResult: { thread_id: 'env' } },
  });
  vm.runInContext(fs.readFileSync('ui/frontend/research-bridge.js', 'utf8'), context);
  context.result = { thread_id: 'env', research_events: [], research_environment: {
    snapshot: { label: '分段水流', assumption: '具名测试假设', load_state: '半载', load_factor: 1,
      version: 'test-v1', content_hash: 'verified-env-hash' },
    segment_ids: ['leg'], ground_speeds_kmh: [12.72], water_speeds_kmh: [12],
    segments: { leg: { current_along_route_mps: .2, waiting_duration_s: 300, wind_factor: 1, channel_factor: 1 } },
  } };
  await vm.runInContext('ResearchBridge.renderDetails(result)', context);
  const html = nodes.get('#agent-decision-list').innerHTML;
  assert.match(html, /<details><summary>本次环境与双航速依据/);
  for (const label of ['对地 km\/h', '对水 km\/h', '12.720', '12.000', 'verified-env-hash']) {
    assert.ok(html.includes(label.replace('\\/', '/')));
  }
});

async function project(outcome, backend = 'analytic', events = []) {
  const prediction = { outcome_code: outcome === 'prediction_completed' ? 'ok' : outcome,
    backend, summary: { motor_energy_kwh: 1.5 }, power_series_ref: 'power-hash',
    diagnostics: outcome === 'model_out_of_domain' ? [{ code: 'long_horizon_unvalidated' }] : [] };
  const task = { thread_id: 'task', status: 'completed', task_type: 'prediction',
    outcome_code: outcome, request: {}, result_refs: { Tenergy: 'energy-hash' }, metrics: {} };
  const nodes = new Map();
  const context = vm.createContext({
    apiFetch: async path => ({ ok: true, json: async () => path.endsWith('/energy-hash')
      ? { payload: prediction } : path.includes('/events?') ? { events } : task }),
    $: id => { if (!nodes.has(id)) nodes.set(id, {}); return nodes.get(id); },
    escapeHtml: String, formatNumber: String, state: { lastResult: { thread_id: 'task' } },
  });
  vm.runInContext(fs.readFileSync('ui/frontend/research-bridge.js', 'utf8'), context);
  const result = await vm.runInContext('ResearchBridge.load("task", () => {})', context);
  context.result = result;
  await vm.runInContext('ResearchBridge.renderDetails(result)', context);
  return { result, nodes, context };
}

test('prediction success is separate from a feasible voyage and shows real backend/evidence', async () => {
  for (const backend of ['analytic', 'lstm']) {
    const { result, nodes } = await project('prediction_completed', backend);
    assert.equal(result.status, 'prediction_completed');
    assert.equal(result.research_prediction.backend, backend);
    assert.equal(result.plan_alternatives, undefined);
    assert.match(nodes.get('#agent-decision-list').innerHTML, /power-hash/);
    assert.match(nodes.get('#agent-decision-list').innerHTML, new RegExp(backend));
  }
});

test('LSTM domain rejection displays diagnostics and is never voyage infeasibility', async () => {
  const { result, nodes } = await project('model_out_of_domain', 'lstm');
  assert.equal(result.status, 'failed');
  assert.match(nodes.get('#agent-decision-list').innerHTML, /long_horizon_unvalidated/);
});

test('dashboard prediction rendering clears voyage metrics and never starts voyage playback', async () => {
  const { result, context, nodes } = await project('prediction_completed');
  const app = fs.readFileSync('ui/frontend/app.js', 'utf8');
  const render = app.slice(app.indexOf('function renderResult(result)'), app.indexOf('function pcm16Base64'));
  let vessel;
  Object.assign(context, {
    requestCompleteForForm: () => false, syncPartialRequestToInputs: () => {},
    rawSegments: () => [], planSegments: () => [], updateStatus: () => {},
    renderProgress: () => {}, renderRoute: () => {}, renderModel: () => {}, renderTrace: () => {},
    renderRecommendations: () => {}, renderEnergyChart: () => {}, renderTable: () => {},
    renderSafety: () => {}, updateVesselView: mode => { vessel = mode; },
    formatHours: String, userStorageSet: () => {}, operatorSummary: () => '预测完成',
    renderHistory: () => {}, refreshIcons: () => {}, renderRuntimeControls: () => {}, ACTIVE_THREAD_KEY: '',
    ACTIVE_TASK_TEXT_KEY: '', ACTIVE_REQUEST_KEY: '',
  });
  context.$('#infeasible-alert').setAttribute = () => {};
  context.$('#correction-feedback').classList = { remove() {} };
  vm.runInContext(render + '\nrenderResult(result)', context);
  assert.equal(nodes.get('#metric-soc').textContent, '--');
  assert.equal(nodes.get('#metric-eta').textContent, '--');
  assert.equal(nodes.get('#infeasible-alert').hidden, true);
  assert.equal(nodes.get('#result-actions').hidden, true);
  assert.equal(vessel, 'incomplete');
});

test('dashboard distinguishes an unfinished solver from a proven candidate energy shortfall', async () => {
  const app = fs.readFileSync('ui/frontend/app.js', 'utf8');
  const render = app.slice(app.indexOf('function renderResult(result)'), app.indexOf('function pcm16Base64'));
  for (const label of ['计算未完成', '候选方案电量不足']) {
    const { result, context } = await project('solver_unresolved');
    result.operator_guidance = { title: label, status_label: label, reason: '原因', suggestion: '下一步' };
    let status;
    Object.assign(context, {
      requestCompleteForForm: () => false, syncPartialRequestToInputs: () => {},
      rawSegments: () => [], planSegments: () => [], updateStatus: (_, text) => { status = text; },
      renderProgress: () => {}, renderRoute: () => {}, renderModel: () => {}, renderTrace: () => {},
      renderRecommendations: () => {}, renderEnergyChart: () => {}, renderTable: () => {},
      renderSafety: () => {}, updateVesselView: () => {}, formatHours: String,
      userStorageSet: () => {}, operatorSummary: () => '未完成', renderHistory: () => {},
      refreshIcons: () => {}, renderRuntimeControls: () => {}, showInfeasible: () => {}, ACTIVE_THREAD_KEY: '',
      ACTIVE_TASK_TEXT_KEY: '', ACTIVE_REQUEST_KEY: '',
    });
    vm.runInContext(render + '\nrenderResult(result)', context);
    assert.equal(status, label);
  }
});

test('research progress treats completed Tenergy candidates without outcome_code as finished', () => {
  const app = fs.readFileSync('ui/frontend/app.js', 'utf8');
  const traceHelper = app.slice(app.indexOf('function researchToolTrace(events)'), app.indexOf('function rawSegments(result)'));
  const context = vm.createContext({});
  vm.runInContext(traceHelper, context);
  const events = [
    { type: 'tool_completed', tool: 'Tenergy', backend: 'analytic' },
    { type: 'tool_failed', tool: 'Tmanagement', outcome_code: 'solver_unresolved' },
  ];
  context.events = events;
  const trace = vm.runInContext('researchToolTrace(events)', context);
  assert.deepEqual(Array.from(trace, item => ({ node: item.node, status: item.status })), [
    { node: 'Tenergy', status: 'ok' },
    { node: 'Tmanagement', status: 'failed' },
  ]);
});

test('tool call details show finish for completed calls and retain real failures', async () => {
  const events = [
    { seq: 1, type: 'tool_completed', tool: 'Tenergy', backend: 'analytic' },
    { seq: 2, type: 'tool_failed', tool: 'Tmanagement', outcome_code: 'solver_unresolved' },
  ];
  const { nodes } = await project('model_out_of_domain', 'lstm', events);
  const html = nodes.get('#trace-list').innerHTML;
  assert.match(html, /Tenergy · completed/);
  assert.match(html, /finish · analytic/);
  assert.match(html, /Tmanagement · failed/);
  assert.match(html, /solver_unresolved/);
});

test('running decision panel shows live public actions and model response', async () => {
  const nodes = new Map();
  const context = vm.createContext({
    apiFetch: async () => ({ ok: true, json: async () => ({ payload: {
      response: { message: { role: 'assistant', content: '正在评估航段' } },
    } }) }),
    $: id => { if (!nodes.has(id)) nodes.set(id, {}); return nodes.get(id); },
    escapeHtml: String,
    state: { running: true, activeRequest: { taskId: 'task' }, lastResult: null },
    userStorageGet: () => 'task', ACTIVE_THREAD_KEY: 'active',
  });
  vm.runInContext(fs.readFileSync('ui/frontend/research-bridge.js', 'utf8'), context);
  context.liveResult = { thread_id: 'task', research_events: [
    { seq: 1, type: 'model_completed', model: 'test-model', artifact_ref: 'response-ref' },
    { seq: 2, type: 'action_requested', tool: 'Tseg', public_rationale: '读取登记航段' },
  ] };
  await vm.runInContext('ResearchBridge.renderDetails(liveResult, { live: true })', context);
  assert.match(nodes.get('#agent-decision-meta').textContent, /等待任务完成 · 1 次模型响应/);
  assert.match(nodes.get('#agent-decision-list').innerHTML, /读取登记航段/);
  assert.match(nodes.get('#agent-decision-list').innerHTML, /正在评估航段/);
});
