(() => {
  'use strict';

  const state = {
    version: 0,
    token: sessionStorage.getItem('sub2apiAuditerAdminToken') || '',
    activeTab: 'statistics',
    logs: [],
    statistics: null,
    unauthorizedShown: false,
  };
  const $ = (id) => document.getElementById(id);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));
  const validTabs = new Set(['statistics', 'logs', 'config']);

  function applyTheme(theme) {
    if (!['light', 'dark', 'system'].includes(theme)) return;
    document.documentElement.dataset.theme = theme;
  }

  const params = new URLSearchParams(window.location.search);
  applyTheme(params.get('theme') || 'system');
  const embedded = params.get('embedded') === '1' || window.self !== window.top;
  if (embedded) {
    document.body.classList.add('embedded');
    // When the page is reverse-proxied under the same origin as sub2api, inherit
    // and follow the parent's `.dark` class without requiring integration code.
    try {
      const parentRoot = window.parent.document.documentElement;
      const syncParentTheme = () => applyTheme(parentRoot.classList.contains('dark') ? 'dark' : 'light');
      syncParentTheme();
      new MutationObserver(syncParentTheme).observe(parentRoot, { attributes: true, attributeFilter: ['class'] });
    } catch (_) {
      // Cross-origin embeds can pass ?theme=... or send a postMessage below.
    }
  }
  window.addEventListener('message', (event) => {
    const data = event.data || {};
    if (data.type === 'sub2api-theme' || data.type === 'sub2api:theme') {
      applyTheme(String(data.theme || 'system'));
    }
  });

  function notifyHeight() {
    if (window.self === window.top) return;
    window.parent.postMessage({
      type: 'sub2api-auditer:resize',
      height: document.documentElement.scrollHeight,
    }, '*');
  }
  if ('ResizeObserver' in window) {
    new ResizeObserver(() => notifyHeight()).observe(document.documentElement);
  }

  function headers(json = false) {
    const value = {};
    if (json) value['Content-Type'] = 'application/json';
    if (state.token) value.Authorization = `Bearer ${state.token}`;
    return value;
  }

  async function request(path, options = {}) {
    const response = await fetch(path, options);
    let data = {};
    try { data = await response.json(); } catch (_) { data = {}; }
    if (!response.ok) {
      const error = new Error(data?.error?.message || `HTTP ${response.status}`);
      error.status = response.status;
      error.code = data?.error?.code || '';
      if (response.status === 401) promptForToken();
      throw error;
    }
    return data;
  }

  function promptForToken() {
    if (state.unauthorizedShown) return;
    state.unauthorizedShown = true;
    openDialog($('tokenDialog'));
  }

  function openDialog(dialog) {
    if (!dialog || dialog.open) return;
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
  }

  function closeDialog(dialog) {
    if (!dialog) return;
    if (typeof dialog.close === 'function') dialog.close();
    else dialog.removeAttribute('open');
  }

  function escapeHtml(value) {
    return String(value ?? '')
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#039;');
  }

  function setMessage(element, text, type = '') {
    if (!element) return;
    element.textContent = text || '';
    element.className = `inline-message ${type}`;
  }

  function setBusy(button, busy, busyText) {
    if (!button) return;
    if (!button.dataset.originalText) button.dataset.originalText = button.textContent;
    button.disabled = busy;
    button.textContent = busy ? busyText : button.dataset.originalText;
  }

  function formatMs(value, digits = 'auto') {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
    const number = Number(value);
    let precision = 0;
    if (digits === 'auto') precision = number < 10 ? 3 : number < 100 ? 1 : 0;
    else precision = Number(digits);
    return `${number.toFixed(precision)} ms`;
  }

  function formatTime(value) {
    if (!value) return '—';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    const pad = (number, width = 2) => String(number).padStart(width, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${pad(date.getMilliseconds(), 3)}`;
  }

  function timeCell(value) {
    if (!value) return '<span class="time-cell">—</span>';
    const formatted = formatTime(value);
    const parts = formatted.split(' ');
    return `<div class="time-cell"><span>${escapeHtml(parts[1] || formatted)}</span><small>${escapeHtml(parts[0] || '')}</small></div>`;
  }

  function updateReady(ready, customText = '') {
    const badge = $('readyBadge');
    badge.className = `status-badge ${ready ? 'status-success' : 'status-warning'}`;
    $('readyText').textContent = customText || (ready ? '服务已就绪' : '服务未就绪');
    $('runtimeReady').textContent = customText || (ready ? '已就绪' : '未就绪');
  }

  async function loadStatus() {
    try {
      const data = await request('/api/status', { headers: headers() });
      state.unauthorizedShown = false;
      $('versionBadge').textContent = `v${data.version}`;
      $('configVersionBadge').textContent = `v${data.config_version}`;
      $('runtimeLatency').textContent = data.stats.last_latency_ms ? formatMs(data.stats.last_latency_ms) : '—';
      $('runtimeInFlight').textContent = String(data.stats.in_flight || 0);
      $('runtimeResult').textContent = `${data.stats.success || 0} / ${data.stats.failed || 0}`;
      $('runtimeCapacity').textContent = String(data.stats.capacity || 100);
      updateReady(Boolean(data.ready));
    } catch (error) {
      if (error.status !== 401) updateReady(false, '状态读取失败');
    }
  }

  async function loadConfig() {
    setMessage($('configMessage'), '正在读取配置…');
    try {
      const data = await request('/api/config', { headers: headers() });
      const config = data.config;
      state.version = config.version || 0;
      $('baseUrl').value = config.base_url || '';
      $('model').value = config.model || '';
      $('prompt').value = config.prompt || '';
      $('timeout').value = config.timeout_seconds ?? 20;
      $('maxTokens').value = config.max_tokens ?? 256;
      $('apiKey').value = '';
      $('clearApiKey').checked = false;
      $('apiKeyHint').textContent = config.has_api_key
        ? `已配置：${config.api_key_masked}`
        : '尚未配置 API Key（部分上游允许匿名访问）';
      $('configVersionBadge').textContent = `v${state.version}`;
      setMessage(
        $('configMessage'),
        data.config_error ? `配置文件异常：${data.config_error}` : '配置读取成功',
        data.config_error ? 'error' : 'success',
      );
      updateReady(config.ready && !data.config_error);
    } catch (error) {
      setMessage($('configMessage'), error.status === 401 ? '需要正确的管理员令牌才能读取配置。' : error.message, 'error');
      updateReady(false, error.status === 401 ? '等待管理员令牌' : '读取失败');
    }
  }

  async function saveConfig(event) {
    event.preventDefault();
    const button = $('saveButton');
    setBusy(button, true, '正在保存…');
    setMessage($('configMessage'), '');
    try {
      const payload = {
        base_url: $('baseUrl').value.trim(),
        api_key: $('apiKey').value.trim(),
        clear_api_key: $('clearApiKey').checked,
        model: $('model').value.trim(),
        prompt: $('prompt').value,
        timeout_seconds: Number($('timeout').value),
        max_tokens: Number($('maxTokens').value),
        expected_version: state.version,
      };
      const data = await request('/api/config', {
        method: 'PUT', headers: headers(true), body: JSON.stringify(payload),
      });
      state.version = data.config.version;
      $('apiKey').value = '';
      $('clearApiKey').checked = false;
      $('apiKeyHint').textContent = data.config.has_api_key
        ? `已配置：${data.config.api_key_masked}`
        : '尚未配置 API Key（部分上游允许匿名访问）';
      $('configVersionBadge').textContent = `v${state.version}`;
      setMessage($('configMessage'), `配置已保存，当前版本 v${state.version}`, 'success');
      updateReady(data.config.ready);
      await loadStatus();
    } catch (error) {
      setMessage($('configMessage'), error.message, 'error');
    } finally {
      setBusy(button, false, '');
    }
  }

  async function runTest() {
    const button = $('testButton');
    setBusy(button, true, '正在调用…');
    setMessage($('testMessage'), '');
    $('testOutput').classList.add('hidden');
    try {
      const data = await request('/api/test', {
        method: 'POST', headers: headers(true), body: JSON.stringify({ text: $('testText').value }),
      });
      $('normalizedOutput').textContent = `${data.normalized.sub2api_content}\n\n上游调用耗时：${data.latency_ms} ms\nTrace ID：${data.trace_id}`;
      $('rawOutput').textContent = data.raw_model_output;
      $('testOutput').classList.remove('hidden');
      setMessage($('testMessage'), '上游调用和 sub2api 格式转换均成功。', 'success');
      await Promise.all([loadStatus(), loadLogs(), loadStatistics()]);
    } catch (error) {
      setMessage($('testMessage'), error.message, 'error');
      await Promise.allSettled([loadStatus(), loadLogs(), loadStatistics()]);
    } finally {
      setBusy(button, false, '');
    }
  }

  function renderPhaseBars(phases) {
    const values = [
      Number(phases.preprocess_average_ms || 0),
      Number(phases.upstream_average_ms || 0),
      Number(phases.response_average_ms || 0),
    ];
    const max = Math.max(...values, 1);
    $('phasePreprocess').textContent = formatMs(values[0]);
    $('phaseUpstream').textContent = formatMs(values[1]);
    $('phaseResponse').textContent = formatMs(values[2]);
    $('phasePreprocessBar').style.width = `${Math.max(values[0] > 0 ? 3 : 0, values[0] * 100 / max)}%`;
    $('phaseUpstreamBar').style.width = `${Math.max(values[1] > 0 ? 3 : 0, values[1] * 100 / max)}%`;
    $('phaseResponseBar').style.width = `${Math.max(values[2] > 0 ? 3 : 0, values[2] * 100 / max)}%`;
  }

  function renderLatencyChart(series) {
    const svg = $('latencyChart');
    const empty = $('chartEmpty');
    if (!Array.isArray(series) || series.length === 0) {
      svg.innerHTML = '';
      empty.classList.remove('hidden');
      return;
    }
    empty.classList.add('hidden');
    const width = 760;
    const height = 260;
    const margin = { left: 52, right: 18, top: 18, bottom: 34 };
    const plotWidth = width - margin.left - margin.right;
    const plotHeight = height - margin.top - margin.bottom;
    const maxValue = Math.max(1, ...series.map((item) => Number(item.total_ms || 0))) * 1.08;
    const x = (index) => margin.left + (series.length === 1 ? plotWidth / 2 : index * plotWidth / (series.length - 1));
    const y = (value) => margin.top + plotHeight - (Number(value || 0) / maxValue) * plotHeight;
    const points = (key) => series.map((item, index) => `${x(index).toFixed(2)},${y(item[key]).toFixed(2)}`).join(' ');
    const totalPoints = points('total_ms');
    const upstreamPoints = points('upstream_ms');
    const areaPoints = `${margin.left},${margin.top + plotHeight} ${totalPoints} ${margin.left + plotWidth},${margin.top + plotHeight}`;
    let grid = '';
    for (let index = 0; index <= 4; index += 1) {
      const lineY = margin.top + index * plotHeight / 4;
      const value = maxValue * (1 - index / 4);
      grid += `<line class="chart-grid-line" x1="${margin.left}" y1="${lineY}" x2="${margin.left + plotWidth}" y2="${lineY}"></line>`;
      grid += `<text class="chart-axis-label" x="${margin.left - 8}" y="${lineY + 4}" text-anchor="end">${escapeHtml(formatMs(value, 0))}</text>`;
    }
    const labelIndexes = [...new Set([0, Math.floor((series.length - 1) / 2), series.length - 1])];
    const labels = labelIndexes.map((index) => `<text class="chart-axis-label" x="${x(index)}" y="${height - 8}" text-anchor="middle">${escapeHtml(series[index].label || '')}</text>`).join('');
    const circles = series.length <= 16
      ? series.map((item, index) => `<circle class="chart-point-total" cx="${x(index)}" cy="${y(item.total_ms)}" r="3.5"><title>${escapeHtml(item.label)} · ${escapeHtml(formatMs(item.total_ms))}</title></circle>`).join('')
      : '';
    svg.innerHTML = `${grid}<polygon class="chart-area" points="${areaPoints}"></polygon><polyline class="chart-line-total" points="${totalPoints}"></polyline><polyline class="chart-line-upstream" points="${upstreamPoints}"></polyline>${circles}${labels}`;
  }

  function renderStatistics(data) {
    state.statistics = data;
    $('statsCapacity').textContent = String(data.capacity || 100);
    $('statTotal').textContent = String(data.window_size || 0);
    $('statRpm').textContent = `${data.rpm_1m || 0} RPM · ${data.in_flight || 0} 处理中`;
    $('statSuccessRate').textContent = `${Number(data.success_rate || 0).toFixed(1)}%`;
    $('statSuccess').textContent = `${data.success || 0} 成功`;
    $('statAverage').textContent = formatMs(data.latency?.average_ms || 0);
    $('statP50').textContent = `P50 ${formatMs(data.latency?.p50_ms || 0)}`;
    $('statP95').textContent = formatMs(data.latency?.p95_ms || 0);
    $('statMaximum').textContent = `最大 ${formatMs(data.latency?.maximum_ms || 0)}`;
    $('statUpstream').textContent = formatMs(data.phases?.upstream_average_ms || 0);
    $('statUpstreamP95').textContent = `P95 ${formatMs(data.latency?.upstream_p95_ms || 0)}`;
    $('statFailed').textContent = String(data.failed || 0);
    $('statInFlight').textContent = `${data.in_flight || 0} 处理中`;
    $('statUnsafe').textContent = String(data.decisions?.Unsafe || 0);
    $('statControversial').textContent = `${data.decisions?.Controversial || 0} Controversial`;
    $('statSafe').textContent = String(data.decisions?.Safe || 0);
    $('statUnclassified').textContent = `${data.decisions?.Unclassified || 0} 未分类`;
    renderPhaseBars(data.phases || {});
    renderLatencyChart(data.series || []);

    const errors = data.errors || [];
    $('errorBreakdown').innerHTML = errors.length
      ? errors.map((item) => `<div class="breakdown-item"><code title="${escapeHtml(item.code)}">${escapeHtml(item.code)}</code><strong>${item.count}</strong></div>`).join('')
      : '<div class="empty-inline">当前日志窗口没有错误。</div>';

    const slowest = data.slowest || [];
    $('slowestList').innerHTML = slowest.length
      ? slowest.map((item) => `<div class="slow-item"><code title="${escapeHtml(item.id)}">${escapeHtml(item.id)} · ${escapeHtml(item.error_code || item.status)}</code><strong>${escapeHtml(formatMs(item.total_ms))}</strong></div>`).join('')
      : '<div class="empty-inline">暂无已完成请求。</div>';
    notifyHeight();
  }

  async function loadStatistics() {
    setMessage($('statisticsMessage'), '');
    try {
      const data = await request('/api/statistics', { headers: headers() });
      renderStatistics(data);
    } catch (error) {
      setMessage($('statisticsMessage'), error.status === 401 ? '需要管理员令牌才能读取统计。' : error.message, 'error');
    }
  }

  function statusBadge(log) {
    if (log.status === 'success') return '<span class="badge badge-success">成功</span>';
    if (log.status === 'error') return '<span class="badge badge-danger">失败</span>';
    return '<span class="badge badge-warning">处理中</span>';
  }

  function resultHtml(log) {
    if (log.status === 'success') {
      const badgeClass = log.safety === 'Unsafe' ? 'badge-danger' : log.safety === 'Controversial' ? 'badge-warning' : 'badge-success';
      return `<div class="result-cell"><span class="badge ${badgeClass}">${escapeHtml(log.safety || 'Success')}</span><div class="result-sub" title="${escapeHtml((log.categories || []).join(', '))}">${escapeHtml((log.categories || []).join(', ') || 'None')}</div></div>`;
    }
    if (log.status === 'error') {
      return `<div class="result-cell"><div class="result-main">${escapeHtml(log.error_code || `HTTP ${log.http_status || '-'}`)}</div><div class="result-sub" title="${escapeHtml(log.error_message)}">${escapeHtml(log.error_message || '处理失败')}</div></div>`;
    }
    return '<div class="result-cell"><div class="result-main">等待完成</div><div class="result-sub">processing</div></div>';
  }

  function filteredLogs() {
    const status = $('logStatus').value;
    const source = $('logSource').value;
    const query = $('logSearch').value.trim().toLowerCase();
    return state.logs.filter((log) => {
      if (status !== 'all' && log.status !== status) return false;
      if (source !== 'all' && log.source !== source) return false;
      if (!query) return true;
      return [log.id, log.error_code, log.request_model, log.upstream_model, log.client_request_id, log.upstream_request_id]
        .some((value) => String(value || '').toLowerCase().includes(query));
    });
  }

  function renderLogs() {
    const logs = filteredLogs();
    const body = $('logsBody');
    $('logCountBadge').textContent = `${logs.length} / ${state.logs.length}`;
    $('logsEmpty').classList.toggle('hidden', logs.length !== 0);
    body.innerHTML = logs.map((log) => `
      <tr>
        <td>${statusBadge(log)}<div class="result-sub">${log.source === 'manual_test' ? '网页测试' : 'sub2api'}</div></td>
        <td>${timeCell(log.received_at)}</td>
        <td>${timeCell(log.forwarded_at)}</td>
        <td>${timeCell(log.llm_replied_at)}</td>
        <td>${timeCell(log.sub2api_replied_at)}</td>
        <td class="phase-cell"><div class="phase-stack"><span>前处理 <strong>${escapeHtml(formatMs(log.preprocess_ms))}</strong></span><span>上游 <strong>${escapeHtml(formatMs(log.upstream_ms))}</strong></span><span>回写 <strong>${escapeHtml(formatMs(log.response_ms))}</strong></span></div></td>
        <td><span class="total-latency">${escapeHtml(formatMs(log.total_ms ?? log.elapsed_ms))}</span>${log.total_ms === null ? '<div class="result-sub">当前耗时</div>' : ''}</td>
        <td>${resultHtml(log)}</td>
        <td><button type="button" class="row-action" data-trace-id="${escapeHtml(log.id)}">详情</button></td>
      </tr>`).join('');
    notifyHeight();
  }

  async function loadLogs() {
    setMessage($('logsMessage'), '');
    try {
      const data = await request('/api/logs?limit=100', { headers: headers() });
      state.logs = data.items || [];
      $('runtimeCapacity').textContent = String(data.capacity || 100);
      renderLogs();
    } catch (error) {
      setMessage($('logsMessage'), error.status === 401 ? '需要管理员令牌才能读取日志。' : error.message, 'error');
    }
  }

  function detailCard(label, value) {
    return `<div class="detail-card"><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value || '—')}</dd></div>`;
  }

  function timelineItem(title, timestamp, duration, missing = false) {
    return `<div class="timeline-item ${missing ? 'missing' : ''}"><div class="timeline-title"><span>${escapeHtml(title)}</span><strong>${escapeHtml(duration || '')}</strong></div><div class="timeline-time">${escapeHtml(formatTime(timestamp))}</div></div>`;
  }

  function openLogDetail(traceId) {
    const log = state.logs.find((item) => item.id === traceId);
    if (!log) return;
    $('detailTraceId').textContent = log.id;
    const result = log.status === 'success'
      ? `${log.safety || 'Success'} · ${(log.categories || []).join(', ') || 'None'}`
      : `${log.error_code || log.status}${log.error_message ? ` · ${log.error_message}` : ''}`;
    $('logDetailBody').innerHTML = `
      <div class="detail-hero">
        <div class="detail-hero-main"><h3>${statusBadge(log)} ${escapeHtml(result)}</h3><p>${log.source === 'manual_test' ? '网页连通性测试' : 'sub2api Prompt Audit 请求'}</p></div>
        <div class="detail-total">${escapeHtml(formatMs(log.total_ms ?? log.elapsed_ms))}</div>
      </div>
      <div class="timeline">
        ${timelineItem('收到 sub2api 请求', log.received_at, 'T+0')}
        ${timelineItem('开始转发给上游 LLM', log.forwarded_at, formatMs(log.preprocess_ms), !log.forwarded_at)}
        ${timelineItem('完整接收上游 LLM 回复', log.llm_replied_at, formatMs(log.upstream_ms), !log.llm_replied_at)}
        ${timelineItem('响应体发送给 sub2api', log.sub2api_replied_at, formatMs(log.response_ms), !log.sub2api_replied_at)}
      </div>
      <dl class="detail-grid">
        ${detailCard('请求模型', log.request_model)}
        ${detailCard('实际上游模型', log.upstream_model)}
        ${detailCard('输入规模', `${log.input_chars || 0} 字符 / ${log.input_bytes || 0} bytes`)}
        ${detailCard('上游响应规模', `${log.upstream_response_bytes || 0} bytes`)}
        ${detailCard('返回 HTTP 状态', log.http_status)}
        ${detailCard('上游 HTTP 状态', log.upstream_http_status)}
        ${detailCard('sub2api Request ID', log.client_request_id)}
        ${detailCard('上游 Request ID', log.upstream_request_id)}
        ${detailCard('前处理耗时', formatMs(log.preprocess_ms))}
        ${detailCard('上游耗时', formatMs(log.upstream_ms))}
        ${detailCard('响应回写耗时', formatMs(log.response_ms))}
        ${detailCard('总耗时', formatMs(log.total_ms ?? log.elapsed_ms))}
      </dl>`;
    openDialog($('logDialog'));
  }

  async function clearLogs() {
    if (!window.confirm('确定清空当前实例的内存处理日志吗？统计数据也会同时清空。')) return;
    const button = $('clearLogs');
    setBusy(button, true, '清空中…');
    try {
      const data = await request('/api/logs', { method: 'DELETE', headers: headers() });
      setMessage($('logsMessage'), `已清空 ${data.cleared || 0} 条日志。`, 'success');
      await Promise.all([loadLogs(), loadStatistics(), loadStatus()]);
    } catch (error) {
      setMessage($('logsMessage'), error.message, 'error');
    } finally {
      setBusy(button, false, '');
    }
  }

  function selectTab(tab, updateHash = true) {
    if (!validTabs.has(tab)) tab = 'statistics';
    state.activeTab = tab;
    $$('[data-tab]').forEach((button) => {
      const active = button.dataset.tab === tab;
      button.classList.toggle('tab-active', active);
      button.setAttribute('aria-selected', String(active));
    });
    $$('[data-panel]').forEach((panel) => panel.classList.toggle('hidden', panel.dataset.panel !== tab));
    if (updateHash) history.replaceState(null, '', `${window.location.pathname}${window.location.search}#${tab}`);
    if (tab === 'statistics') loadStatistics();
    if (tab === 'logs') loadLogs();
    if (tab === 'config') Promise.all([loadConfig(), loadStatus()]);
    notifyHeight();
  }

  async function refreshActiveTab() {
    if (document.hidden) return;
    if (state.activeTab === 'statistics') await loadStatistics();
    if (state.activeTab === 'logs') await loadLogs();
    await loadStatus();
  }

  $('configForm').addEventListener('submit', saveConfig);
  $('reloadButton').addEventListener('click', loadConfig);
  $('testButton').addEventListener('click', runTest);
  $('refreshStatistics').addEventListener('click', loadStatistics);
  $('refreshLogs').addEventListener('click', loadLogs);
  $('clearLogs').addEventListener('click', clearLogs);
  $('logStatus').addEventListener('change', renderLogs);
  $('logSource').addEventListener('change', renderLogs);
  $('logSearch').addEventListener('input', renderLogs);
  $('logsBody').addEventListener('click', (event) => {
    const button = event.target.closest('[data-trace-id]');
    if (button) openLogDetail(button.dataset.traceId);
  });
  $('closeLogDialog').addEventListener('click', () => closeDialog($('logDialog')));
  $('tokenButton').addEventListener('click', () => openDialog($('tokenDialog')));
  $('adminToken').value = state.token;
  $('applyToken').addEventListener('click', async () => {
    state.token = $('adminToken').value.trim();
    sessionStorage.setItem('sub2apiAuditerAdminToken', state.token);
    state.unauthorizedShown = false;
    closeDialog($('tokenDialog'));
    await Promise.allSettled([loadConfig(), loadStatus(), loadLogs(), loadStatistics()]);
  });
  $('forgetToken').addEventListener('click', async () => {
    state.token = '';
    $('adminToken').value = '';
    sessionStorage.removeItem('sub2apiAuditerAdminToken');
    state.unauthorizedShown = false;
    closeDialog($('tokenDialog'));
    await Promise.allSettled([loadConfig(), loadStatus(), loadLogs(), loadStatistics()]);
  });
  $$('[data-tab]').forEach((button) => button.addEventListener('click', () => selectTab(button.dataset.tab)));
  window.addEventListener('hashchange', () => selectTab(window.location.hash.slice(1), false));

  $('endpointExample').textContent = `${window.location.protocol}//${window.location.host || 'auditer:8080'}`;
  const initialTab = validTabs.has(window.location.hash.slice(1)) ? window.location.hash.slice(1) : 'statistics';
  selectTab(initialTab, false);
  Promise.allSettled([loadStatus(), loadConfig(), loadLogs(), loadStatistics()]).finally(notifyHeight);
  setInterval(refreshActiveTab, 4000);
})();
