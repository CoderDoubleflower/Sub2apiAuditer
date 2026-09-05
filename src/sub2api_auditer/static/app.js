(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));
  const storageKey = 'sub2apiAuditerAdminToken';
  const storage = {
    get() { try { return sessionStorage.getItem(storageKey) || ''; } catch (_) { return ''; } },
    set(value) { try { if (value) sessionStorage.setItem(storageKey, value); else sessionStorage.removeItem(storageKey); } catch (_) { /* Sandboxed embeds can still use an in-memory token. */ } },
  };
  const state = { token: storage.get(), version: 0, activeTab: 'statistics', logs: [], persistence: 'memory', unauthorizedShown: false, refreshing: false };
  const validTabs = new Set(['statistics', 'logs', 'config']);
  // Resolve under the browser-visible directory, including a stripped proxy prefix.
  // The server/proxy must redirect /auditer to /auditer/ before serving this page.
  const apiBase = new URL('./', window.location.href);
  const params = new URLSearchParams(window.location.search);
  function applyTheme(theme) {
    if (['light', 'dark', 'system'].includes(theme)) document.documentElement.dataset.theme = theme;
  }
  applyTheme(params.get('theme') || 'system');
  const embedded = params.get('embedded') === '1' || window.self !== window.top;
  if (embedded) {
    document.body.classList.add('embedded');
    try {
      const root = window.parent.document.documentElement;
      const sync = () => applyTheme(root.classList.contains('dark') ? 'dark' : 'light');
      sync();
      new MutationObserver(sync).observe(root, { attributes: true, attributeFilter: ['class'] });
    } catch (_) { /* Cross-origin parent can send an explicit theme. */ }
  }
  window.addEventListener('message', (event) => {
    if (event.source !== window.parent) return;
    if (['sub2api-theme', 'sub2api:theme'].includes(event.data?.type)) applyTheme(event.data.theme || 'system');
  });
  let lastHeight = 0;
  function notifyHeight() {
    if (window.self === window.top) return;
    const height = document.documentElement.scrollHeight;
    if (height === lastHeight) return;
    lastHeight = height;
    window.parent.postMessage({ type: 'sub2api-auditer:resize', height }, '*');
  }
  if ('ResizeObserver' in window) new ResizeObserver(notifyHeight).observe(document.documentElement);
  function headers(json = false) {
    const result = json ? { 'Content-Type': 'application/json' } : {};
    if (state.token) result.Authorization = `Bearer ${state.token}`;
    return result;
  }
  function openDialog(dialog) {
    if (!dialog || dialog.open) return;
    if (typeof dialog.showModal === 'function') dialog.showModal(); else dialog.setAttribute('open', '');
  }
  function closeDialog(dialog) {
    if (typeof dialog.close === 'function') dialog.close(); else dialog.removeAttribute('open');
  }
  async function request(path, options = {}) {
    const response = await fetch(new URL(path.replace(/^\/+/, ''), apiBase), options);
    let data = {};
    try { data = await response.json(); } catch (_) { /* Report the HTTP error below. */ }
    if (!response.ok) {
      const error = new Error(data?.error?.message || `HTTP ${response.status}`);
      error.status = response.status;
      if (response.status === 401 && !state.unauthorizedShown) {
        state.unauthorizedShown = true;
        openDialog($('tokenDialog'));
      }
      throw error;
    }
    return data;
  }
  const escapeHtml = (value) => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#039;');
  function message(id, text, type = '') {
    $(id).textContent = text || '';
    $(id).className = `inline-message ${type}`;
  }
  function busy(id, value, text) {
    const button = $(id);
    button.dataset.originalText ||= button.textContent;
    button.disabled = value;
    button.textContent = value ? text : button.dataset.originalText;
  }
  function formatMs(value, digits) {
    if (value == null || !Number.isFinite(Number(value))) return '—';
    const number = Number(value);
    return `${number.toFixed(digits ?? (number < 10 ? 3 : number < 100 ? 1 : 0))} ms`;
  }
  function formatTime(value) {
    if (!value) return '—';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    const pad = (v, n = 2) => String(v).padStart(n, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${pad(date.getMilliseconds(), 3)}`;
  }
  function timeCell(value) {
    if (!value) return '<span class="time-cell">—</span>';
    const [day, time] = formatTime(value).split(' ');
    return `<div class="time-cell"><span>${escapeHtml(time || day)}</span><small>${escapeHtml(day)}</small></div>`;
  }
  function updateReady(ready, text = '') {
    $('readyBadge').className = `status-badge ${ready ? 'status-success' : 'status-warning'}`;
    $('readyText').textContent = text || (ready ? '服务已就绪' : '服务未就绪');
    $('runtimeReady').textContent = text || (ready ? '已就绪' : '未就绪');
  }
  function updatePersistence(value) {
    state.persistence = value || 'memory';
    $('storageHint').textContent = state.persistence === 'sqlite'
      ? 'SQLite 持久化；正常重启后恢复已保存记录。'
      : '仅内存模式；重启后清空。';
    $('runtimeStorage').textContent = state.persistence === 'sqlite' ? 'SQLite' : '内存';
  }
  async function loadStatus() {
    try {
      const data = await request('api/status', { headers: headers() });
      state.unauthorizedShown = false;
      $('versionBadge').textContent = `v${data.version}`;
      $('configVersionBadge').textContent = `v${data.config_version}`;
      $('runtimeLatency').textContent = data.stats.last_latency_ms ? formatMs(data.stats.last_latency_ms) : '—';
      $('runtimeInFlight').textContent = data.stats.in_flight || 0;
      $('runtimeResult').textContent = `${data.stats.success || 0} / ${data.stats.failed || 0}`;
      $('runtimeCapacity').textContent = data.stats.capacity || 100;
      updatePersistence(data.stats.persistence);
      message('storageMessage', data.stats.persistence_error || '', 'error');
      updateReady(data.ready);
    } catch (error) { if (error.status !== 401) updateReady(false, '状态读取失败'); }
  }
  async function loadConfig() {
    try {
      const data = await request('api/config', { headers: headers() });
      const config = data.config;
      state.version = config.version || 0;
      for (const [id, key] of [['baseUrl', 'base_url'], ['model', 'model'], ['prompt', 'prompt'], ['timeout', 'timeout_seconds'], ['maxTokens', 'max_tokens']]) $(id).value = config[key] ?? '';
      $('apiKey').value = '';
      $('clearApiKey').checked = false;
      $('apiKeyHint').textContent = config.has_api_key ? `已配置：${config.api_key_masked}` : '尚未配置 API Key（部分上游允许匿名访问）';
      $('configVersionBadge').textContent = `v${state.version}`;
      message('configMessage', data.config_error ? `配置文件异常：${data.config_error}` : '配置读取成功', data.config_error ? 'error' : 'success');
      updateReady(config.ready && !data.config_error);
    } catch (error) { message('configMessage', error.message, 'error'); }
  }
  async function saveConfig(event) {
    event.preventDefault();
    busy('saveButton', true, '正在保存…');
    try {
      const payload = { base_url: $('baseUrl').value.trim(), api_key: $('apiKey').value.trim(), clear_api_key: $('clearApiKey').checked,
        model: $('model').value.trim(), prompt: $('prompt').value, timeout_seconds: Number($('timeout').value),
        max_tokens: Number($('maxTokens').value), expected_version: state.version };
      const data = await request('api/config', { method: 'PUT', headers: headers(true), body: JSON.stringify(payload) });
      state.version = data.config.version;
      $('apiKey').value = '';
      $('clearApiKey').checked = false;
      $('apiKeyHint').textContent = data.config.has_api_key ? `已配置：${data.config.api_key_masked}` : '尚未配置 API Key';
      message('configMessage', `配置已保存，当前版本 v${state.version}`, 'success');
      await loadStatus();
    } catch (error) { message('configMessage', error.message, 'error'); }
    finally { busy('saveButton', false); }
  }
  async function runTest() {
    busy('testButton', true, '正在调用…');
    $('testOutput').classList.add('hidden');
    try {
      const data = await request('api/test', { method: 'POST', headers: headers(true), body: JSON.stringify({ text: $('testText').value }) });
      $('normalizedOutput').textContent = `${data.normalized.sub2api_content}\n\n上游调用耗时：${data.latency_ms} ms\nTrace ID：${data.trace_id}`;
      $('rawOutput').textContent = data.raw_model_output;
      $('testOutput').classList.remove('hidden');
      message('testMessage', '上游调用和 sub2api 格式转换均成功。', 'success');
    } catch (error) { message('testMessage', error.message, 'error'); }
    finally { busy('testButton', false); await Promise.allSettled([loadStatus(), loadLogs(), loadStatistics()]); }
  }
  function renderLatencyChart(series) {
    const svg = $('latencyChart');
    $('chartEmpty').classList.toggle('hidden', series.length > 0);
    if (!series.length) { svg.innerHTML = ''; return; }
    const width = 760, height = 260, left = 52, right = 18, top = 18, bottom = 34;
    const pw = width - left - right, ph = height - top - bottom;
    const max = Math.max(1, ...series.map((item) => Number(item.total_ms || 0))) * 1.08;
    const x = (i) => left + (series.length === 1 ? pw / 2 : i * pw / (series.length - 1));
    const y = (v) => top + ph - Number(v || 0) * ph / max;
    const points = (key) => series.map((item, i) => `${x(i).toFixed(2)},${y(item[key]).toFixed(2)}`).join(' ');
    let grid = '';
    for (let i = 0; i <= 4; i++) {
      const yy = top + i * ph / 4;
      grid += `<line class="chart-grid-line" x1="${left}" y1="${yy}" x2="${width - right}" y2="${yy}"></line><text class="chart-axis-label" x="${left - 8}" y="${yy + 4}" text-anchor="end">${escapeHtml(formatMs(max * (1 - i / 4), 0))}</text>`;
    }
    const labels = [...new Set([0, Math.floor((series.length - 1) / 2), series.length - 1])].map((i) => `<text class="chart-axis-label" x="${x(i)}" y="${height - 8}" text-anchor="middle">${escapeHtml(series[i].label)}</text>`).join('');
    svg.innerHTML = `${grid}<polygon class="chart-area" points="${left},${top + ph} ${points('total_ms')} ${left + pw},${top + ph}"></polygon><polyline class="chart-line-total" points="${points('total_ms')}"></polyline><polyline class="chart-line-upstream" points="${points('upstream_ms')}"></polyline>${labels}`;
  }
  function renderStatistics(data) {
    updatePersistence(data.persistence);
    const latency = data.latency || {}, phases = data.phases || {}, decisions = data.decisions || {};
    const text = { statsCapacity: data.capacity, statTotal: data.window_size, statRpm: `${data.rpm_1m || 0} 窗口 RPM · ${data.in_flight || 0} 处理中`,
      statSuccessRate: `${Number(data.success_rate || 0).toFixed(1)}%`, statSuccess: `${data.success || 0} 成功`, statAverage: formatMs(latency.average_ms),
      statP50: `P50 ${formatMs(latency.p50_ms)}`, statP95: formatMs(latency.p95_ms), statMaximum: `最大 ${formatMs(latency.maximum_ms)}`,
      statUpstream: formatMs(phases.upstream_average_ms), statUpstreamP95: `P95 ${formatMs(latency.upstream_p95_ms)}`, statFailed: data.failed || 0,
      statInFlight: `${data.in_flight || 0} 处理中`, statUnsafe: decisions.Unsafe || 0, statControversial: `${decisions.Controversial || 0} Controversial`,
      statSafe: decisions.Safe || 0, statUnclassified: `${decisions.Unclassified || 0} 未分类` };
    for (const [id, value] of Object.entries(text)) $(id).textContent = value ?? '';
    const values = [phases.preprocess_average_ms || 0, phases.upstream_average_ms || 0, phases.response_average_ms || 0];
    const max = Math.max(1, ...values);
    ['phasePreprocess', 'phaseUpstream', 'phaseResponse'].forEach((id, i) => { $(id).textContent = formatMs(values[i]); $(`${id}Bar`).style.width = `${Math.max(values[i] > 0 ? 3 : 0, values[i] * 100 / max)}%`; });
    renderLatencyChart(data.series || []);
    $('errorBreakdown').innerHTML = (data.errors || []).map((item) => `<div class="breakdown-item"><code>${escapeHtml(item.code)}</code><strong>${escapeHtml(item.count)}</strong></div>`).join('') || '<div class="empty-inline">当前日志窗口没有错误。</div>';
    $('slowestList').innerHTML = (data.slowest || []).map((item) => `<div class="slow-item"><code>${escapeHtml(item.id)} · ${escapeHtml(item.error_code || item.status)}</code><strong>${escapeHtml(formatMs(item.total_ms))}</strong></div>`).join('') || '<div class="empty-inline">暂无已完成请求。</div>';
    notifyHeight();
  }
  async function loadStatistics() {
    try { renderStatistics(await request('api/statistics', { headers: headers() })); message('statisticsMessage', ''); }
    catch (error) { message('statisticsMessage', error.message, 'error'); }
  }
  function statusBadge(log) {
    const [style, label] = log.status === 'success' ? ['success', '成功'] : log.status === 'error' ? ['danger', '失败'] : ['warning', '处理中'];
    return `<span class="badge badge-${style}">${label}</span>`;
  }
  function resultHtml(log) {
    if (log.status === 'success') return `<div class="result-cell"><span class="badge badge-${log.safety === 'Unsafe' ? 'danger' : log.safety === 'Controversial' ? 'warning' : 'success'}">${escapeHtml(log.safety || 'Success')}</span><div class="result-sub">${escapeHtml((log.categories || []).join(', ') || 'None')}</div></div>`;
    return `<div class="result-cell"><div class="result-main">${escapeHtml(log.error_code || '等待完成')}</div><div class="result-sub">${escapeHtml(log.error_message || '')}</div></div>`;
  }
  function renderLogs() {
    const query = $('logSearch').value.trim().toLowerCase();
    const logs = state.logs.filter((log) => ($('logStatus').value === 'all' || log.status === $('logStatus').value)
      && ($('logSource').value === 'all' || log.source === $('logSource').value)
      && (!query || [log.id, log.error_code, log.request_model, log.upstream_model, log.client_request_id, log.upstream_request_id].some((v) => String(v || '').toLowerCase().includes(query))));
    $('logCountBadge').textContent = `${logs.length} / ${state.logs.length}`;
    $('logsEmpty').classList.toggle('hidden', logs.length !== 0);
    $('logsBody').innerHTML = logs.map((log) => `<tr><td>${statusBadge(log)}<div class="result-sub">${log.source === 'manual_test' ? '网页测试' : 'sub2api'}</div></td>${['received_at', 'forwarded_at', 'llm_replied_at', 'sub2api_replied_at'].map((key) => `<td>${timeCell(log[key])}</td>`).join('')}<td class="phase-cell"><div class="phase-stack">${[['前处理', 'preprocess_ms'], ['上游', 'upstream_ms'], ['回写', 'response_ms']].map(([label, key]) => `<span>${label} <strong>${formatMs(log[key])}</strong></span>`).join('')}</div></td><td><span class="total-latency">${formatMs(log.total_ms ?? log.elapsed_ms)}</span>${log.total_ms == null ? '<div class="result-sub">当前耗时</div>' : ''}</td><td>${resultHtml(log)}</td><td><button type="button" class="row-action" data-trace-id="${escapeHtml(log.id)}">详情</button></td></tr>`).join('');
    notifyHeight();
  }
  async function loadLogs() {
    try {
      const data = await request('api/logs?limit=100', { headers: headers() });
      state.logs = data.items || []; updatePersistence(data.persistence); renderLogs(); message('logsMessage', '');
    } catch (error) { message('logsMessage', error.message, 'error'); }
  }
  function openLogDetail(traceId) {
    const log = state.logs.find((item) => item.id === traceId);
    if (!log) return;
    $('detailTraceId').textContent = traceId;
    const timeline = [['收到请求', 'received_at', null], ['开始转发给上游 LLM', 'forwarded_at', 'preprocess_ms'], ['完整接收上游 LLM 回复', 'llm_replied_at', 'upstream_ms'], ['完成 ASGI 响应发送（非 sub2api 确认）', 'sub2api_replied_at', 'response_ms']];
    const cards = [['请求模型', log.request_model], ['实际上游模型', log.upstream_model], ['输入规模', `${log.input_chars || 0} 字符 / ${log.input_bytes || 0} bytes`], ['上游响应规模', `${log.upstream_response_bytes || 0} bytes`], ['返回 / 本地结束状态', log.http_status], ['上游 HTTP 状态', log.upstream_http_status], ['sub2api Request ID', log.client_request_id], ['上游 Request ID', log.upstream_request_id], ['前处理耗时', formatMs(log.preprocess_ms)], ['上游耗时', formatMs(log.upstream_ms)], ['响应回写耗时', formatMs(log.response_ms)], ['总耗时', formatMs(log.total_ms ?? log.elapsed_ms)]];
    const result = log.status === 'success' ? `${log.safety || 'Success'} · ${(log.categories || []).join(', ') || 'None'}` : `${log.error_code || log.status} · ${log.error_message || ''}`;
    $('logDetailBody').innerHTML = `<div class="detail-hero"><div class="detail-hero-main"><h3>${statusBadge(log)} ${escapeHtml(result)}</h3><p>${log.source === 'manual_test' ? '网页测试' : 'sub2api Prompt Audit 请求'}；取消或发送失败时，未发生的时间点留空。</p></div><div class="detail-total">${formatMs(log.total_ms ?? log.elapsed_ms)}</div></div><div class="timeline">${timeline.map(([label, key, duration]) => `<div class="timeline-item ${log[key] ? '' : 'missing'}"><div class="timeline-title"><span>${label}</span><strong>${duration ? formatMs(log[duration]) : 'T+0'}</strong></div><div class="timeline-time">${escapeHtml(formatTime(log[key]))}</div></div>`).join('')}</div><dl class="detail-grid">${cards.map(([label, value]) => `<div class="detail-card"><dt>${label}</dt><dd>${escapeHtml(value ?? '—')}</dd></div>`).join('')}</dl>`;
    openDialog($('logDialog'));
  }
  async function clearLogs() {
    const storageText = state.persistence === 'sqlite' ? '内存和 SQLite 数据库' : '内存';
    if (!window.confirm(`确定清空当前实例的${storageText}处理日志吗？统计窗口也会清空，已清空记录不会在重启后恢复。`)) return;
    busy('clearLogs', true, '清空中…');
    try {
      const data = await request('api/logs', { method: 'DELETE', headers: headers() });
      await Promise.allSettled([loadLogs(), loadStatistics(), loadStatus()]);
      message('logsMessage', `已清空 ${data.cleared || 0} 条日志。`, 'success');
    } catch (error) { message('logsMessage', error.message, 'error'); }
    finally { busy('clearLogs', false); }
  }
  function selectTab(tab, updateHash = true) {
    state.activeTab = validTabs.has(tab) ? tab : 'statistics';
    $$('[data-tab]').forEach((button) => { const active = button.dataset.tab === state.activeTab; button.classList.toggle('tab-active', active); button.setAttribute('aria-selected', String(active)); });
    $$('[data-panel]').forEach((panel) => panel.classList.toggle('hidden', panel.dataset.panel !== state.activeTab));
    if (updateHash) history.replaceState(null, '', `${window.location.pathname}${window.location.search}#${state.activeTab}`);
    if (state.activeTab === 'statistics') loadStatistics();
    if (state.activeTab === 'logs') loadLogs();
    if (state.activeTab === 'config') loadConfig();
    notifyHeight();
  }
  async function refreshActiveTab() {
    if (document.hidden || state.refreshing) return;
    state.refreshing = true;
    try { await Promise.allSettled([loadStatus(), state.activeTab === 'statistics' ? loadStatistics() : state.activeTab === 'logs' ? loadLogs() : Promise.resolve()]); }
    finally { state.refreshing = false; }
  }
  $('configForm').addEventListener('submit', saveConfig);
  for (const [id, callback] of [['reloadButton', loadConfig], ['testButton', runTest], ['refreshStatistics', loadStatistics], ['refreshLogs', loadLogs], ['clearLogs', clearLogs]]) $(id).addEventListener('click', callback);
  $('logStatus').addEventListener('change', renderLogs); $('logSource').addEventListener('change', renderLogs); $('logSearch').addEventListener('input', renderLogs);
  $('logsBody').addEventListener('click', (event) => { const button = event.target.closest('[data-trace-id]'); if (button) openLogDetail(button.dataset.traceId); });
  $('closeLogDialog').addEventListener('click', () => closeDialog($('logDialog')));
  $('tokenButton').addEventListener('click', () => openDialog($('tokenDialog')));
  $('adminToken').value = state.token;
  async function changeToken(value) {
    state.token = value; $('adminToken').value = value; storage.set(value); state.unauthorizedShown = false; closeDialog($('tokenDialog'));
    await Promise.allSettled([loadConfig(), loadStatus(), loadLogs(), loadStatistics()]);
  }
  $('applyToken').addEventListener('click', () => changeToken($('adminToken').value.trim()));
  $('forgetToken').addEventListener('click', () => changeToken(''));
  $$('[data-tab]').forEach((button) => button.addEventListener('click', () => selectTab(button.dataset.tab)));
  window.addEventListener('hashchange', () => selectTab(window.location.hash.slice(1), false));
  $('endpointExample').textContent = apiBase.href.replace(/\/$/, '');
  selectTab(window.location.hash.slice(1), false);
  Promise.allSettled([loadStatus(), loadConfig(), loadLogs()]).finally(notifyHeight);
  setInterval(refreshActiveTab, 4000);
})();
