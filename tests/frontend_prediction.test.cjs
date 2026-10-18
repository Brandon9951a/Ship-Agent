const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');

async function project(outcome, backend = 'analytic') {
  const prediction = { outcome_code: outcome === 'prediction_completed' ? 'ok' : outcome,
    backend, summary: { motor_energy_kwh: 1.5 }, power_series_ref: 'power-hash',
    diagnostics: outcome === 'model_out_of_domain' ? [{ code: 'long_horizon_unvalidated' }] : [] };
  const task = { thread_id: 'task', status: 'completed', task_type: 'prediction',
    outcome_code: outcome, request: {}, result_refs: { Tenergy: 'energy-hash' }, metrics: {} };
  const nodes = new Map();
  const context = vm.createContext({
    apiFetch: async path => ({ ok: true, json: async () => path.endsWith('/energy-hash')
      ? { payload: prediction } : path.includes('/events?') ? { events: [] } : task }),
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
