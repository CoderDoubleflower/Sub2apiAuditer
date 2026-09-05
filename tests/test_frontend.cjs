'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { resolve } = require('node:path');
const root = resolve(__dirname, '../src/sub2api_auditer/static');
const html = fs.readFileSync(resolve(root, 'index.html'), 'utf8');
const script = fs.readFileSync(resolve(root, 'app.js'), 'utf8');
const settle = () => new Promise((r) => setImmediate(r));

function boot(url, { deniedStorage = false, persistence = 'sqlite', clearFails = false } = {}) {
  const nodes = new Map();
  for (const match of html.matchAll(/id="([^"]+)"/g)) {
    const id = match[1];
    assert(!nodes.has(id), `duplicate DOM id ${id}`);
    nodes.set(id, { value: ['logStatus', 'logSource'].includes(id) ? 'all' : '', textContent: '', innerHTML: '',
      dataset: {}, style: {}, checked: false, open: false, handlers: {},
      classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
      addEventListener(event, handler) { this.handlers[event] = handler; },
      setAttribute() {}, removeAttribute() {}, showModal() { this.open = true; }, close() { this.open = false; } });
  }
  const doc = { getElementById(id) { assert(nodes.has(id), `missing DOM id ${id}`); return nodes.get(id); },
    querySelectorAll() { return []; }, hidden: false,
    documentElement: { dataset: {}, scrollHeight: 1000, classList: { contains() { return false; } } },
    body: { classList: { add() {} } } };
  const calls = [], confirmations = [];
  const location = new URL(url);
  const win = { location, document: doc, addEventListener() {}, postMessage() {}, confirm(text) { confirmations.push(text); return true; } };
  win.self = win.top = win.parent = win;
  const storage = { getItem() { if (deniedStorage) throw Error('denied'); return ''; }, setItem() { if (deniedStorage) throw Error('denied'); }, removeItem() { if (deniedStorage) throw Error('denied'); } };
  const config = { version: 1, base_url: 'https://gateway.invalid/v1', model: 'test', prompt: 'policy', timeout_seconds: 20, max_tokens: 256, ready: true };
  const context = { window: win, document: doc, sessionStorage: storage, URL, URLSearchParams,
    history: { replaceState() {} }, setInterval() {}, console,
    async fetch(input, options = {}) {
      const url = new URL(input);
      calls.push({ url, options });
      let data = {};
      let status = 200;
      if (url.pathname.endsWith('/api/status')) data = { version: '1.2.1', ready: true, config_version: 1, stats: { persistence, persistence_error: '', capacity: 100 } };
      else if (url.pathname.endsWith('/api/config')) data = { config };
      else if (url.pathname.endsWith('/api/statistics')) data = { persistence, capacity: 100, latency: {}, phases: {}, decisions: {}, series: [], errors: [], slowest: [] };
      else if (options.method === 'DELETE') { status = clearFails ? 500 : 200; data = clearFails ? { error: { message: '清空日志数据库失败' } } : { cleared: 1, ok: true }; }
      else data = { items: [], capacity: 100, persistence };
      return { ok: status === 200, status, async json() { return data; } };
    },
  };
  vm.runInNewContext(script, context, { filename: 'app.js' });
  return { nodes, calls, confirmations };
}

for (const prefix of ['', '/auditer', '/tools/auditer']) {
  test(`all management calls retain browser path prefix: ${prefix || '/'}`, async () => {
    const page = boot(`https://example.invalid${prefix}/?embedded=1&theme=dark#statistics`);
    await settle();
    assert(page.calls.length >= 4);
    for (const call of page.calls) assert(call.url.pathname.startsWith(`${prefix}/api/`), call.url.href);
    assert.equal(page.nodes.get('endpointExample').textContent, `https://example.invalid${prefix}`);
    for (const match of html.matchAll(/(?:href|src)="([^"]+assets[^\"]+)"/g)) assert(match[1].startsWith('./assets/'));
  });
}

test('sandbox-denied sessionStorage does not prevent authentication', async () => {
  const page = boot('https://example.invalid/auditer/', { deniedStorage: true });
  await settle();
  page.nodes.get('adminToken').value = 'admin-token';
  await page.nodes.get('applyToken').handlers.click();
  assert(page.calls.slice(-4).every((call) => call.options.headers.Authorization === 'Bearer admin-token'));
});

test('SQLite mode is visible and clear confirmation mentions database', async () => {
  const page = boot('https://example.invalid/');
  await settle();
  assert(page.nodes.get('storageHint').textContent.includes('SQLite'));
  await page.nodes.get('clearLogs').handlers.click();
  assert(page.confirmations[0].includes('SQLite'));
  assert(page.nodes.get('logsMessage').textContent.includes('已清空'));
});

test('failed database clear is shown as an error, not a success', async () => {
  const page = boot('https://example.invalid/auditer/', { clearFails: true });
  await settle();
  await page.nodes.get('clearLogs').handlers.click();
  assert.equal(page.nodes.get('logsMessage').textContent, '清空日志数据库失败');
  assert(!page.nodes.get('clearLogs').disabled);
});

test('memory-only deployments display the correct restart semantics', async () => {
  const page = boot('https://example.invalid/', { persistence: 'memory' });
  await settle();
  assert(page.nodes.get('storageHint').textContent.includes('仅内存模式'));
  assert.equal(page.nodes.get('runtimeStorage').textContent, '内存');
});
