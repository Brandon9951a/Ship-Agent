const $ = selector => document.querySelector(selector);

const examples = {
  normal: '从平顶山港到军李船闸，2026-09-18 09:00出发，SOC85%，半载，6小时内到达',
  time: '从平顶山港到军李船闸，2026-09-18 09:00出发，SOC85%，半载，1小时内到达',
  soc: '从平顶山港到军李船闸，2026-09-18 09:00出发，SOC31%，半载，6小时内到达',
};

function structuredPayload() {
  return {
    origin: $('#origin').value.trim(),
    destination: $('#destination').value.trim(),
    departure_at: '2026-09-18T09:00:00+08:00',
    max_duration_h: Number($('#duration').value),
    soc_initial: Number($('#soc').value) / 100,
    load_state: '半载',
    environment: {},
  };
}

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[character]));
}

function source(value) {
  return value
    ? `${esc(value.source_id)} · ${value.confirmed ? '已核对' : '待核实'}`
    : '未知来源';
}

function renderSteps(trace) {
  const statuses = Object.fromEntries((trace || []).map(item => [item.node, item.status]));
  const nodes = ['Tdata', 'Tseg', 'Tenergy', 'Tspeed', 'Tmanagement'];
  $('#steps').querySelectorAll('span').forEach((element, index) => {
    const status = statuses[nodes[index]];
    element.className = status === 'ok' ? 'done' : status ? 'error' : '';
  });
}

function renderRoute(rawSegments) {
  if (!rawSegments?.length) {
    $('#routeBody').innerHTML = '<div class="empty">未获得可展示航段</div>';
    return;
  }
  const stops = rawSegments.map((segment, index) =>
    `${index ? '<span class="connector"></span>' : ''}<div class="stop"><i></i>${esc(segment.origin)}</div>`
  ).join('');
  const destination = esc(rawSegments.at(-1).destination);
  const tags = rawSegments.map(segment =>
    `<span>${esc(segment.segment_id)} · ${segment.distance_km} km</span>`
  ).join('');
  $('#routeBody').innerHTML = `<div class="route-line">${stops}<span class="connector"></span><div class="stop"><i></i>${destination}</div></div><div class="seg-tags">${tags}</div>`;
}

function render(result) {
  const status = result.status;
  const okay = status === 'ok';
  const infeasible = status === 'infeasible';
  $('#status').textContent = okay ? '仿真完成' : infeasible ? '不可行' : '需要处理';
  $('#status').className = `badge ${okay ? 'ok' : 'error'}`;
  $('#title').textContent = okay
    ? '已生成软件仿真方案'
    : infeasible ? '航次不可行' : '需要补充或修正任务信息';
  $('#subtitle').textContent = result.final_message || result.error || '';
  renderSteps(result.trace || []);

  const report = result.report;
  const plan = result.plan;
  $('#propEnergy').textContent = report?.summary?.propulsion_energy?.value?.toFixed?.(1) || '--';
  $('#auxEnergy').textContent = report?.summary?.auxiliary_energy?.value?.toFixed?.(1) || '--';
  $('#energy').textContent = report?.summary?.required_energy?.value?.toFixed?.(1) || '--';
  $('#time').textContent = report?.summary?.total_duration?.value?.toFixed?.(2) || '--';
  $('#eta').textContent = report?.summary?.eta?.value?.replace?.('T', ' ') || '--';
  $('#endSoc').textContent = report?.summary?.soc_final?.value != null
    ? (report.summary.soc_final.value * 100).toFixed(1) : '--';
  $('#modelNote').textContent = report?.explanation
    || result.task_understanding?.text
    || '模型仅负责定性说明；工程数值由五工具生成。';
  const llmUsed = report?.explanation_mode === 'llm_qualitative'
    || result.task_understanding?.mode === 'llm_qualitative';
  $('#modelMode').textContent = llmUsed
    ? 'LLM 定性解释' : '模板回退';

  const routeSegments = result.tseg?.payload?.segments || [];
  const segments = okay ? (report?.segments || []) : [];
  $('#count').textContent = (okay ? segments.length : routeSegments.length) || '--';
  $('#tableCount').textContent = `${segments.length} 个航段`;
  $('#route').textContent = result.tdata?.payload?.route_id || '未识别';
  $('#traceBody').innerHTML = (result.trace || []).map((item, index) =>
    `<li><b>${index + 1}. ${esc(item.node)}</b> · ${esc(item.status)}</li>`
  ).join('') || '<li>无工具记录</li>';

  $('#rows').innerHTML = segments.length ? segments.map(segment => {
    const energy = segment.energy || {};
    const distance = segment.distance?.value ?? segment.distance_km ?? '--';
    const speed = segment.speed?.value ?? segment.max_speed_kmh ?? '--';
    const duration = segment.duration?.value?.toFixed?.(2) ?? '--';
    const propulsion = segment.propulsion_energy?.value?.toFixed?.(1)
      ?? energy.energy_kwh?.toFixed?.(1) ?? '--';
    const provenance = `${source(segment.distance_source || segment.source)} · 模型 ${esc(segment.model_id || '未提供')}`;
    return `<tr><td>${esc(segment.origin)} → ${esc(segment.destination)}</td><td>${distance} km</td><td>${speed} km/h</td><td>${duration} h</td><td>${propulsion} kWh</td><td class="source">${provenance}</td></tr>`;
  }).join('') : '<tr><td colspan="6" class="empty">等待结果</td></tr>';
  renderRoute(routeSegments);

  const checks = plan?.optimization?.checks || report?.checks || [];
  const warnings = report?.warnings || [];
  const reportSources = Object.entries(report?.sources || {});
  const assumptions = report?.assumptions || [];
  $('#checks').className = 'checks';
  $('#checks').innerHTML = checks.map(check =>
    `<div class="check ${check.passed ? 'pass' : 'fail'}"><span>${esc(check.name)}</span><b>${check.passed ? '通过' : '未通过'}</b></div>`
  ).join('') + warnings.map(warning =>
    `<div class="warning">风险提示：${esc(warning)}</div>`
  ).join('') + (okay && !warnings.length
    ? '<div class="warning safe">工具未返回额外告警；结果仍仅限 synthetic_demo 软件仿真。</div>' : '')
    + reportSources.map(([name, item]) =>
      `<div class="source-note">${esc(name)}：${source(item)}</div>`
    ).join('')
    + (assumptions.length
      ? `<details><summary>仿真假设与边界（${assumptions.length}）</summary>${assumptions.map(item => `<p>${esc(item)}</p>`).join('')}</details>`
      : '');
  if (!checks.length && !warnings.length && !reportSources.length && !assumptions.length) {
    $('#checks').innerHTML = '<div class="empty">暂无校验结果</div>';
  }

  const options = result.adjustment_options || [];
  $('#actions').className = options.length ? 'actions' : 'actions empty';
  $('#actions').innerHTML = options.map((option, index) =>
    `<div class="action">${index + 1}. ${esc(option.label || option)}</div>`
  ).join('') || '当前无待确认调整项';

  if (!okay) {
    const speedResult = result.tspeed?.payload || result.tspeed || {};
    $('#alert').hidden = false;
    $('#alert').innerHTML = `<strong>${infeasible ? '航次不可行' : '任务未完成'} · ${esc(speedResult.infeasible_type || status)}</strong>${esc(speedResult.reason || result.final_message || result.error || '请检查任务约束。')}`;
  } else {
    $('#alert').hidden = true;
  }
}

async function runRequest(payload) {
  const buttons = [$('#run'), $('#runStructured')];
  buttons.forEach(button => { button.disabled = true; });
  $('#status').textContent = '运行中';
  try {
    const response = await fetch('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    render(result);
  } catch (_error) {
    render({
      status: 'failed',
      error: '无法连接本地服务，请确认启动窗口仍在运行。',
      trace: [],
    });
  } finally {
    buttons.forEach(button => { button.disabled = false; });
  }
}

async function loadHealth() {
  try {
    const health = await (await fetch('/healthz')).json();
    $('#engineState').textContent = health.llm_mode === 'enabled'
      ? '云端文本解释已启用' : '模板回退模式';
  } catch (_error) {
    $('#engineState').textContent = '服务连接待确认';
  }
}

$('#run').onclick = () => runRequest({ task_text: $('#task').value.trim() });
$('#runStructured').onclick = () => runRequest(structuredPayload());
document.querySelectorAll('[data-task]').forEach(button => {
  button.onclick = () => {
    $('#task').value = examples[button.dataset.task];
    runRequest({ task_text: $('#task').value });
  };
});
$('#clock').textContent = new Date().toLocaleTimeString('zh-CN', { hour12: false });
loadHealth();
