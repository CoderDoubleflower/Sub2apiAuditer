    const state = { version: 0, token: sessionStorage.getItem('sub2apiAuditerAdminToken') || '' };
    const $ = (id) => document.getElementById(id);
    $('adminToken').value = state.token;

    function headers(json = false) {
      const value = {};
      if (json) value['Content-Type'] = 'application/json';
      if (state.token) value['Authorization'] = `Bearer ${state.token}`;
      return value;
    }

    async function request(path, options = {}) {
      const response = await fetch(path, options);
      let data = {};
      try { data = await response.json(); } catch (_) {}
      if (!response.ok) {
        const message = data?.error?.message || `HTTP ${response.status}`;
        const error = new Error(message);
        error.status = response.status;
        throw error;
      }
      return data;
    }

    function setMessage(element, text, type = '') {
      element.textContent = text || '';
      element.className = `message ${type}`;
    }

    function setBusy(button, busy, busyText) {
      if (!button.dataset.originalText) button.dataset.originalText = button.textContent;
      button.disabled = busy;
      button.textContent = busy ? busyText : button.dataset.originalText;
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
        setMessage($('configMessage'), data.config_error ? `配置文件异常：${data.config_error}` : '配置读取成功', data.config_error ? 'error' : 'ok');
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
        setMessage($('configMessage'), `配置已保存，当前版本 v${state.version}`, 'ok');
        updateReady(data.config.ready);
        await loadStatus();
      } catch (error) {
        setMessage($('configMessage'), error.message, 'error');
      } finally {
        setBusy(button, false, '');
      }
    }

    function updateReady(ready, customText = '') {
      $('readyDot').classList.toggle('ok', Boolean(ready));
      $('readyText').textContent = customText || (ready ? '服务已就绪' : '服务未就绪');
    }

    async function loadStatus() {
      try {
        const data = await request('/api/status', { headers: headers() });
        $('metricVersion').textContent = `v${data.config_version}`;
        $('metricLatency').textContent = data.stats.last_latency_ms ? `${data.stats.last_latency_ms} ms` : '-';
        $('metricSuccess').textContent = data.stats.success;
        $('metricFailed').textContent = data.stats.failed;
        updateReady(data.ready);
      } catch (error) {
        if (error.status !== 401) updateReady(false, '状态读取失败');
      }
    }

    async function runTest() {
      const button = $('testButton');
      setBusy(button, true, '正在调用…');
      setMessage($('testMessage'), '');
      $('testOutput').style.display = 'none';
      try {
        const data = await request('/api/test', {
          method: 'POST', headers: headers(true), body: JSON.stringify({ text: $('testText').value }),
        });
        $('normalizedOutput').textContent = `${data.normalized.sub2api_content}\n\n延迟：${data.latency_ms} ms`;
        $('rawOutput').textContent = data.raw_model_output;
        $('testOutput').style.display = 'block';
        setMessage($('testMessage'), '上游调用和 sub2api 格式转换均成功。', 'ok');
        await loadStatus();
      } catch (error) {
        setMessage($('testMessage'), error.message, 'error');
      } finally {
        setBusy(button, false, '');
      }
    }

    $('configForm').addEventListener('submit', saveConfig);
    $('reloadButton').addEventListener('click', loadConfig);
    $('testButton').addEventListener('click', runTest);
    $('applyToken').addEventListener('click', async () => {
      state.token = $('adminToken').value.trim();
      sessionStorage.setItem('sub2apiAuditerAdminToken', state.token);
      await loadConfig(); await loadStatus();
    });
    $('forgetToken').addEventListener('click', async () => {
      state.token = ''; $('adminToken').value = ''; sessionStorage.removeItem('sub2apiAuditerAdminToken');
      await loadConfig(); await loadStatus();
    });

    loadConfig();
    loadStatus();
    setInterval(loadStatus, 10000);
