const byId = (id) => document.getElementById(id);
const loginForm = byId('login-form');
const modelForm = byId('model-form');
let session = null;
let authEpoch = 0;
let refreshFlight = null;
let models = [];
let editing = null;
let modelBusy = false;
let modelRead = 0;
let deviceRead = 0;
const audioSettings = {
  speech: {label: '语音转写', path: '/duet/speech/config', fields: ['base_url', 'model'], choice: 'language_format', options: ['legacy', 'languages'], read: 0, ready: false, busy: false},
  tts: {label: '朗读', path: '/duet/tts/config', fields: ['base_url', 'model', 'voice'], choice: 'transport', options: ['speech_http', 'realtime_ws'], read: 0, ready: false, busy: false},
};
let usageRead = 0;
let connectorRead = 0;

class ApiError extends Error {
  constructor(status, code = '') { super(code); this.status = status; this.code = code; }
}

function announce(id, message = '', error = false) {
  const target = byId(id);
  target.textContent = message;
  target.classList.toggle('error', error);
}

function friendly(error) {
  if (error.code === 'invalid_credentials') return '账号或密码不正确，请检查后重试。';
  if (error.code === 'rate_limited' || error.status === 429) return '操作过于频繁，请稍后再试。';
  if (error.status === 401) return '登录已失效，请重新登录。';
  if (error.status === 409) return '操作有冲突。请刷新状态；删除前需要先选用其他模型。';
  if (error.status === 404) return '此记录已不存在，请刷新状态。';
  if (error.status === 400) return '服务器未接受此配置，请检查各项格式和值。';
  if (error.code === 'invalid_response') return '服务器响应格式不符合约定，请检查服务版本。';
  if (!error.status) return '无法确认服务器响应，请检查网络后刷新状态。';
  return `服务器暂时无法完成操作（HTTP ${error.status}），请稍后重试。`;
}

function report(id, error) {
  if (error.code === 'session_changed') return;
  announce(session ? id : 'login-status', friendly(error), true);
}

async function rawFetch(path, {method = 'GET', body, access} = {}) {
  const url = new URL(path, window.location.origin);
  if (url.origin !== window.location.origin || !url.pathname.startsWith('/duet/')) throw new ApiError(0, 'invalid_origin');
  const headers = {Accept: 'application/json'};
  if (access) headers.Authorization = `Bearer ${access}`;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 30000);
  try {
    const response = await fetch(url.pathname + url.search, {
      method, headers, body: body === undefined ? undefined : JSON.stringify(body),
      credentials: 'omit', mode: 'same-origin', redirect: 'error', cache: 'no-store',
      referrerPolicy: 'no-referrer', signal: controller.signal,
    });
    let data = null;
    if (response.status !== 204) {
      try { data = await response.json(); }
      catch { if (response.ok) throw new ApiError(response.status, 'invalid_response'); }
    }
    if (!response.ok) throw new ApiError(response.status, typeof data?.error?.code === 'string' ? data.error.code : '');
    return data;
  } finally { window.clearTimeout(timeout); }
}

function pair(data) {
  if (!data || typeof data.access_token !== 'string' || !data.access_token ||
      typeof data.refresh_token !== 'string' || !data.refresh_token ||
      typeof data.device_id !== 'string' || !Number.isFinite(data.expires_in) || data.expires_in <= 0) {
    throw new ApiError(200, 'invalid_response');
  }
  return {access: data.access_token, refresh: data.refresh_token, deviceId: data.device_id,
    user: data.user || {}, expiresAt: Date.now() + data.expires_in * 1000};
}

function signedOut(message = '') {
  authEpoch += 1;
  session = null;
  refreshFlight = null;
  models = [];
  editing = null;
  modelRead += 1;
  deviceRead += 1;
  usageRead += 1;
  connectorRead += 1;
  byId('dashboard').hidden = true;
  byId('account-tools').hidden = true;
  byId('login-panel').hidden = false;
  byId('account-name').textContent = '';
  byId('models-list').replaceChildren();
  byId('devices-list').replaceChildren();
  byId('usage-list').replaceChildren();
  byId('connectors-list').replaceChildren();
  announce('connectors-status');
  byId('usage-summary').textContent = '';
  announce('usage-status');
  byId('password').value = '';
  modelForm.reset();
  for (const [kind, state] of Object.entries(audioSettings)) {
    state.read += 1;
    state.ready = false;
    state.busy = false;
    byId(`${kind}-form`).reset();
    lockAudio(kind);
  }
  lockModels(false);
  announce('login-status', message);
}

async function refresh() {
  if (refreshFlight) return refreshFlight;
  if (!session) throw new ApiError(401);
  const original = session;
  const epoch = authEpoch;
  const pending = (async () => {
    try {
      const response = await rawFetch('/duet/auth/refresh', {method: 'POST', body: {refresh_token: original.refresh}});
      if (authEpoch !== epoch || session !== original) throw new ApiError(0, 'session_changed');
      session = pair(response);
      byId('account-name').textContent = session.user.display_name || session.user.username || 'duet';
    } catch (error) {
      if (authEpoch === epoch && error.status === 401) signedOut('登录已失效，请重新登录。');
      throw error;
    }
  })();
  refreshFlight = pending;
  try { await pending; }
  finally { if (refreshFlight === pending) refreshFlight = null; }
}

async function api(path, options = {}, retry = true) {
  if (!session) throw new ApiError(401);
  const epoch = authEpoch;
  if (retry && session.expiresAt <= Date.now() + 15000) await refresh();
  if (authEpoch !== epoch || !session) throw new ApiError(0, 'session_changed');
  const access = session.access;
  try {
    const result = await rawFetch(path, {...options, access});
    if (authEpoch !== epoch || !session) throw new ApiError(0, 'session_changed');
    return result;
  } catch (error) {
    if (authEpoch !== epoch) throw new ApiError(0, 'session_changed');
    if (error.status === 401 && retry && session) {
      // Another request may already have rotated the pair. Never reuse an old
      // refresh token or start a second refresh for the same expired access.
      if (session.access === access) await refresh();
      return api(path, options, false);
    }
    if (error.status === 401) signedOut('登录已失效，请重新登录。');
    throw error;
  }
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function action(label, callback, danger = false) {
  const button = element('button', danger ? 'danger quiet' : 'quiet', label);
  button.type = 'button';
  button.addEventListener('click', callback);
  return button;
}

function lockModels(locked) {
  modelBusy = locked;
  byId('model-fields').disabled = locked;
  for (const id of ['new-model', 'save-model', 'cancel-edit']) byId(id).disabled = locked;
  for (const button of byId('models-list').querySelectorAll('button')) button.disabled = locked;
}

function renderModels() {
  const list = byId('models-list');
  list.replaceChildren();
  byId('models-empty').hidden = models.length !== 0;
  for (const model of models) {
    const item = element('article', 'model-item');
    const heading = element('div', 'model-item-heading');
    heading.append(element('h3', '', model.label));
    if (model.current) heading.append(element('span', 'badge', '正在使用'));
    item.append(heading,
      element('p', 'model-meta', `${model.protocol === 'anthropic' ? 'Anthropic' : model.api_mode === 'responses' ? 'OpenAI Responses' : 'OpenAI Chat Completions'} 兼容 · ${model.model}`),
      element('p', 'model-meta', model.base_url),
      element('p', 'model-meta', model.has_key ? '服务器已保存密钥' : '未保存密钥'));
    const actions = element('div', 'item-actions');
    if (!model.current) actions.append(action('选用模型', () => selectModel(model)));
    actions.append(action('编辑', () => editModel(model)));
    if (!model.current) actions.append(action('删除', () => deleteModel(model), true));
    item.append(actions);
    list.append(item);
  }
  for (const button of list.querySelectorAll('button')) button.disabled = modelBusy;
}

async function readModels() {
  const request = ++modelRead;
  const data = await api('/duet/models');
  if (!Array.isArray(data?.models)) throw new ApiError(200, 'invalid_response');
  if (request !== modelRead) return;
  models = data.models;
  renderModels();
}

function newModel(focus = true) {
  if (modelBusy) return;
  editing = null;
  modelForm.reset();
  byId('editor-heading').textContent = '添加模型';
  byId('save-model').textContent = '保存模型';
  byId('cancel-edit').textContent = '清空草稿';
  byId('clear-key').disabled = true;
  byId('clear-prompt').disabled = true;
  byId('api-key').disabled = false;
  byId('system-prompt').disabled = false;
  byId('max-tokens').value = '8192';
  byId('max-tokens').placeholder = '默认 8192';
  syncModelOptions();
  announce('model-status');
  if (focus) byId('model-label').focus();
}

function editModel(model) {
  if (modelBusy) return;
  editing = model.id;
  modelForm.reset();
  byId('editor-heading').textContent = '编辑模型';
  byId('model-label').value = model.label;
  modelForm.querySelector(`input[name="protocol"][value="${model.protocol === 'anthropic' ? 'anthropic' : 'openai'}"]`).checked = true;
  byId('base-url').value = model.base_url;
  byId('model-name').value = model.model;
  byId('api-key').value = '';
  byId('api-key').disabled = false;
  byId('clear-key').disabled = !model.has_key;
  byId('clear-prompt').disabled = false;
  byId('system-prompt').disabled = false;
  byId('prompt-cache-enabled').checked = model.prompt_cache_enabled !== false;
  byId('stream-usage-enabled').checked = model.stream_usage_enabled !== false;
  modelForm.querySelector(`input[name="api_mode"][value="${model.api_mode === 'responses' ? 'responses' : 'chat_completions'}"]`).checked = true;
  byId('reasoning-effort').value = model.reasoning_effort ?? '';
  byId('prompt-cache-mode').value = model.prompt_cache_mode ?? '';
  byId('prompt-cache-ttl').value = model.prompt_cache_ttl ?? '';
  byId('max-tokens').value = Number.isSafeInteger(model.max_tokens) ? String(model.max_tokens) : '';
  byId('max-tokens').placeholder = '留空保留服务器原值';
  syncModelOptions();
  byId('save-model').textContent = '保存更改';
  byId('cancel-edit').textContent = '取消编辑';
  announce('model-status', '密钥和提示词不会回填；留空保留服务器原值。');
  byId('editor-heading').focus();
}

function syncModelOptions() {
  const openai = modelForm.querySelector('input[name="protocol"]:checked').value === 'openai';
  if (!openai) byId('api-chat').checked = true;
  const responses = openai && byId('api-responses').checked;
  byId('api-mode-fields').hidden = !openai;
  byId('api-mode-fields').disabled = !openai;
  byId('api-mode-help').hidden = !openai;
  byId('reasoning-field').hidden = !openai;
  const effort = byId('reasoning-effort');
  effort.disabled = !openai;
  const luna = byId('model-name').value.trim() === 'gpt-6-luna';
  const tools = models.find((model) => model.id === editing)?.allow_tools !== false;
  for (const option of effort.options) {
    option.disabled = luna && (option.value === 'minimal' || (!responses && tools && !['', 'none'].includes(option.value)));
  }
  if (!openai || effort.selectedOptions[0]?.disabled) effort.value = '';
  byId('reasoning-help').textContent = luna ? !responses && tools ?
    'Luna 的 Chat Completions 工具调用只支持 none；默认会使用 none。需要推理时请选择 Responses。' :
    'Luna 支持 none、low、medium、high、xhigh、max；Responses 推荐 high。默认由提供方决定。' :
    '仅支持推理参数的 OpenAI 兼容模型可使用；默认不发送指定值。';
  byId('max-tokens').max = luna ? '128000' : '131072';
  byId('tokens-help').textContent = `范围 1–${luna ? '128000' : '131072'}。编辑时留空会保留原值。`;
  byId('responses-cache-fields').hidden = !responses;
  byId('responses-cache-fields').disabled = !responses || !byId('prompt-cache-enabled').checked;
  if (!responses) {
    byId('prompt-cache-mode').value = '';
    byId('prompt-cache-ttl').value = '';
  }
  byId('stream-usage-enabled').disabled = !openai || responses;
  byId('cache-help').textContent = responses ?
    'Responses 使用稳定缓存键和下方策略。关闭时发送无断点的 explicit 模式；缓存提示不能保证命中。' : openai ?
    'Chat Completions 使用稳定缓存键；关闭仅停止发送提示，不会强制关闭提供方自动缓存。缓存提示不能保证命中。' :
    'Anthropic 使用短期缓存断点。关闭仅停止发送这些提示；是否命中请查看实际用量。';
}

async function selectModel(model) {
  if (modelBusy) return;
  lockModels(true);
  try {
    const confirmed = await api(`/duet/models/${encodeURIComponent(model.id)}/select`, {method: 'POST', body: {}});
    if (!confirmed?.current) throw new ApiError(200, 'invalid_response');
    await readModels();
    announce('dashboard-status', `服务器已选用「${confirmed.label}」，之后发送的消息将使用此模型。`);
  } catch (error) { report('dashboard-status', error); }
  finally { lockModels(false); }
}

async function deleteModel(model) {
  if (modelBusy || !window.confirm(`删除模型「${model.label}」及其服务器配置？此操作无法撤销。`)) return;
  lockModels(true);
  try {
    const result = await api(`/duet/models/${encodeURIComponent(model.id)}`, {method: 'DELETE'});
    if (!result?.deleted) throw new ApiError(200, 'invalid_response');
    if (editing === model.id) { lockModels(false); newModel(false); lockModels(true); }
    await readModels();
    announce('dashboard-status', '服务器已删除该模型。');
  } catch (error) { report('dashboard-status', error); }
  finally { lockModels(false); }
}

function timestamp(value) {
  if (!Number.isFinite(value)) return '—';
  const date = new Date(value * 1000);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString();
}

async function readDevices() {
  const request = ++deviceRead;
  const data = await api('/duet/auth/devices');
  if (!Array.isArray(data?.devices)) throw new ApiError(200, 'invalid_response');
  if (request !== deviceRead) return;
  const list = byId('devices-list');
  list.replaceChildren();
  byId('devices-empty').hidden = data.devices.length !== 0;
  for (const device of data.devices) {
    const item = element('li', 'device-item');
    const detail = element('div');
    detail.append(element('div', 'device-name', `${device.device_name}${device.current ? ' · 当前浏览器' : ''}`),
      element('p', 'device-detail', `登录：${timestamp(device.created_at)} · 最近活动：${timestamp(device.last_seen_at)}`));
    item.append(detail);
    if (device.active) item.append(action('撤销登录', () => revokeDevice(device), true));
    else item.append(element('span', 'hint', device.revoked_at ? '已撤销' : '已过期'));
    list.append(item);
  }
}

async function revokeDevice(device) {
  const question = device.current ? '撤销当前浏览器的登录？你将立即退出。' : `撤销「${device.device_name}」的登录？该设备需要重新登录。`;
  if (!window.confirm(question)) return;
  try {
    await api(`/duet/auth/devices/${encodeURIComponent(device.id)}`, {method: 'DELETE'});
    if (device.current) { signedOut('此浏览器的登录已被服务器撤销。'); byId('username').focus(); }
    else { await readDevices(); announce('dashboard-status', '服务器已撤销该设备的登录。'); }
  } catch (error) { report('dashboard-status', error); }
}

async function reloadAll() {
  await Promise.all([readModels(), readDevices(), readUsage().catch((error) => {
    if (session) announce('usage-status', error.status === 404 || error.status === 501 ? '服务器尚未提供用量记录接口。' : friendly(error), true);
  }), ...Object.keys(audioSettings).map((kind) => readAudio(kind).catch((error) => {
    if (session) announce(`${kind}-status`, friendly(error), true);
  })), readConnectors().catch((error) => {
    if (session) announce('connectors-status', error.status === 404 || error.status === 501 ? '服务器尚未接入 MCP 连接器接口。' : friendly(error), true);
  })]);
}

const connectorStates = {
  disabled: '未启用', configured: '已保存，尚未连接', connecting: '连接中', connected: '已连接',
  auth_required: '需要凭据或重新授权', oauth_unsupported: '服务器未启用浏览器 OAuth',
  authentication_required: '凭据无效或权限不足', request_timeout: '连接超时',
};

async function readConnectors() {
  const sequence = ++connectorRead;
  const data = await api('/duet/connectors');
  if (sequence !== connectorRead) return;
  if (!data || !data.catalog || !Array.isArray(data.connectors)) throw new ApiError(200, 'invalid_response');
  const list = byId('connectors-list');
  const focusId = list.contains(document.activeElement) ? document.activeElement.id : '';
  list.replaceChildren();
  const usedIds = new Set(data.connectors.map((item) => item.id));
  for (const [catalogId, entry] of Object.entries(data.catalog)) {
    const installations = data.connectors.filter((item) => item.catalog_id === catalogId);
    if (installations.length) {
      for (const saved of installations) renderConnector(list, saved.id, catalogId, entry, saved);
    } else {
      let id = catalogId;
      for (let index = 1; usedIds.has(id); index += 1) id = `${catalogId}_new${index}`;
      renderConnector(list, id, catalogId, entry);
    }
  }
  if (focusId) byId(focusId)?.focus();
}

function renderConnector(list, id, catalogId, entry, saved) {
  const form = element('form', 'connector-form');
  form.method = 'post';
  form.action = `/duet/connectors/${encodeURIComponent(id)}`;
  const title = saved ? `${entry.name} · ${saved.label ? `${saved.label} (${id})` : id}` : entry.name;
  const heading = element('h3', '', title);
  heading.id = `connector-${id}-heading`;
  form.setAttribute('aria-labelledby', heading.id);
  form.append(heading, element('p', 'hint connector-url', entry.url));
  const status = element('p', 'hint', saved ? `${connectorStates[saved.status] || '连接失败，请检查配置'} · ${saved.tool_count || 0} 个可用工具` : '未配置');
  form.append(status);
  const fields = element('fieldset');
  fields.append(element('legend', 'visually-hidden', `${title} 连接设置`));
  function input(name, label, type, value) {
    const field = element('div', 'field');
    const control = element('input');
    control.id = `connector-${id}-${name}`;
    control.name = name;
    control.type = type;
    const caption = element('label', '', label);
    caption.htmlFor = control.id;
    if (type === 'checkbox') {
      caption.className = 'check-row';
      control.checked = Boolean(value);
      caption.prepend(control);
      field.append(caption);
    } else {
      control.value = value || '';
      field.append(caption, control);
    }
    fields.append(field);
    return control;
  }
  const enabled = input('enabled', '启用此连接器', 'checkbox', saved?.enabled);
  const modes = element('fieldset', 'protocol-choice');
  modes.append(element('legend', '', '认证方式'));
  let selectedAuth = saved?.auth_type || entry.default_auth;
  const authModes = [...new Set([...entry.auth_modes, ...(saved?.auth_type === 'oauth' ? ['oauth'] : [])])];
  for (const mode of authModes) {
    const radio = element('input');
    radio.type = 'radio'; radio.name = 'auth_type'; radio.value = mode;
    radio.id = `connector-${id}-auth-${mode}`;
    radio.checked = mode === selectedAuth;
    const label = element('label', '', mode === 'oauth' ? '浏览器 OAuth 授权' : mode === 'none' ? '免密钥（服务限额）' : mode === 'api_key' ? 'API 密钥' : '个人访问令牌');
    label.htmlFor = radio.id;
    label.prepend(radio);
    modes.append(label);
    radio.addEventListener('change', () => { selectedAuth = mode; syncSecret(); });
  }
  fields.append(modes);
  const token = input('token', '服务凭据', 'password', '');
  token.autocomplete = 'new-password'; token.maxLength = 4096;
  token.spellcheck = false; token.autocapitalize = 'none';
  const help = element('p', 'hint');
  help.id = `connector-${id}-token-help`;
  token.setAttribute('aria-describedby', help.id);
  token.parentElement.append(help);
  const clear = input('clear_token', '清空服务器已保存的凭据', 'checkbox', false);
  clear.disabled = !saved?.token_set;
  const readonly = entry.readonly_supported ? input('readonly', '请求服务端只读工具集', 'checkbox', saved?.readonly ?? true) : null;
  const result = element('p', 'status');
  result.setAttribute('role', 'status'); result.setAttribute('aria-live', 'polite');
  const buttons = element('div', 'form-actions');
  const save = element('button', 'primary', '保存配置'); save.type = 'submit';
  save.id = `connector-${id}-save`;
  const saveHelp = element('p', 'hint', saved ? '保存会断开此安装的当前连接并重置工具授权；使用 OAuth 时还需重新登录。其它安装不受影响。' : '保存后新建此服务的连接配置；连接并读取工具需另行点击。');
  saveHelp.id = `connector-${id}-save-help`;
  save.setAttribute('aria-describedby', saveHelp.id);
  const path = `/duet/connectors/${encodeURIComponent(id)}`;
  const connectButton = action('连接并读取工具', () => perform(async () => {
    await api(`${path}/refresh`, {method: 'POST'});
    await readConnectors();
  }));
  connectButton.id = `connector-${id}-connect`;
  connectButton.disabled = !saved?.enabled;
  const disconnect = action('断开并清除凭据', () => perform(async () => {
    await api(`${path}/disconnect`, {method: 'POST'});
    await readConnectors();
  }));
  disconnect.id = `connector-${id}-disconnect`;
  disconnect.disabled = !saved;
  buttons.append(save, connectButton, disconnect);
  form.append(fields, saveHelp, result, buttons);
  list.append(form);
  function syncSecret() {
    const oauth = selectedAuth === 'oauth';
    token.disabled = selectedAuth === 'none' || oauth || clear.checked;
    clear.disabled = oauth || selectedAuth === 'none' || !saved?.token_set;
    help.textContent = oauth ? 'OAuth 凭据由浏览器授权保存；此处无需粘贴令牌。' : saved?.token_set ? '服务器已保存凭据；留空保留，勾选下项可清空。' : '不会回填或显示服务器凭据。';
    if (token.disabled) token.value = '';
  }
  clear.addEventListener('change', syncSecret);
  syncSecret();
  async function perform(callback) {
    const focusId = form.contains(document.activeElement) ? document.activeElement.id : '';
    fields.disabled = true;
    for (const button of buttons.children) button.disabled = true;
    result.textContent = '正在读取服务器响应…';
    try { await callback(); }
    catch (error) { if (session && error.code !== 'session_changed') { result.textContent = friendly(error); result.classList.add('error'); } }
    finally {
      fields.disabled = false; save.disabled = false;
      connectButton.disabled = !saved?.enabled; disconnect.disabled = !saved;
      syncSecret();
      if (focusId && document.activeElement === document.body) byId(focusId)?.focus();
    }
  }
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const body = {catalog_id: catalogId, enabled: enabled.checked, auth_type: selectedAuth, readonly: readonly?.checked ?? true};
    if (clear.checked || selectedAuth === 'none' || (selectedAuth === 'oauth' && saved?.auth_type !== 'oauth')) body.token = '';
    else if (token.value) body.token = token.value;
    perform(async () => {
      await api(path, {method: 'PUT', body});
      token.value = '';
      await readConnectors();
      announce('connectors-status', '配置已保存；点击连接后才会读取该服务的工具。');
    });
  });
}

byId('refresh-connectors').addEventListener('click', async () => {
  byId('refresh-connectors').disabled = true;
  try { await readConnectors(); announce('connectors-status', '已读取服务器当前连接状态。'); }
  catch (error) { report('connectors-status', error); }
  finally { byId('refresh-connectors').disabled = false; }
});

function tokenCount(value) {
  return Number.isSafeInteger(value) && value >= 0 ? `${value.toLocaleString()} tokens` : '未报告';
}

async function readUsage() {
  const request = ++usageRead;
  const data = await api('/duet/usage?limit=50');
  if (!Array.isArray(data?.requests) || !Number.isSafeInteger(data?.summary?.requests)) throw new ApiError(200, 'invalid_response');
  if (request !== usageRead) return;
  const summary = data.summary;
  byId('usage-summary').textContent = `最近 ${summary.requests} 次调用：${summary.usage_reported} 次报告用量，${summary.usage_missing} 次未报告。缓存读取 ${tokenCount(summary.cache_read_tokens)}（${summary.cache_read_reported} 次提供该字段）；缓存写入 ${tokenCount(summary.cache_write_tokens)}（${summary.cache_write_reported} 次提供该字段）。${summary.partial_usage ? `含 ${summary.partial_usage} 次尚未完整或已中断的统计。` : ''}`;
  const list = byId('usage-list');
  list.replaceChildren();
  byId('usage-empty').hidden = data.requests.length !== 0;
  const states = {streaming: '请求中', complete: '完成', error: '失败', stopped: '已停止', interrupted: '已中断', unknown: '状态未报告'};
  for (const record of data.requests) {
    const item = element('li', 'usage-item');
    item.append(element('h3', '', `${record.protocol === 'anthropic' ? 'Anthropic' : 'OpenAI'} · ${record.model}`),
      element('p', 'model-meta', `${timestamp(record.started_at)} · ${states[record.status] || states.unknown}`));
    const hinted = record.protocol === 'anthropic' ? record.cache_breakpoints > 0 : typeof record.cache_key === 'string';
    item.append(element('p', 'model-meta', `缓存提示：${hinted ? record.protocol === 'anthropic' ? `已发送 ${record.cache_breakpoints} 个断点` : '已发送稳定缓存键' : '未发送'}`));
    if (!record.usage_reported) item.append(element('p', 'hint', '提供方未报告用量，无法据此判断缓存是否命中。'));
    else {
      const metrics = element('dl', 'usage-metrics');
      for (const [key, label] of [['input_tokens', record.protocol === 'anthropic' ? '输入（非缓存）' : '输入（含缓存）'], ['output_tokens', '输出'], ['cache_read_tokens', '缓存读取'], ['cache_write_tokens', '缓存写入']]) {
        const pair = element('div');
        pair.append(element('dt', '', label), element('dd', '', tokenCount(record.metrics?.[key])));
        metrics.append(pair);
      }
      item.append(metrics);
      const writes = record.usage?.prompt_tokens_details?.cache_write_tokens_details;
      const reasoning = record.usage?.completion_tokens_details?.reasoning_tokens;
      if (writes && ['ttl_30m_tokens', 'ttl_12h_tokens'].some((key) => Number.isSafeInteger(writes[key]) && writes[key] >= 0)) {
        item.append(element('p', 'hint', `缓存写入有效期明细：30 分钟 ${tokenCount(writes.ttl_30m_tokens)} · 12 小时 ${tokenCount(writes.ttl_12h_tokens)}（包含在输入用量中）。`));
      }
      if (Number.isSafeInteger(reasoning) && reasoning >= 0) item.append(element('p', 'hint', `输出中的推理用量：${tokenCount(reasoning)}。`));
      if (record.partial_usage) item.append(element('p', 'hint', '部分统计：请求尚未完成或已中断。'));
    }
    list.append(item);
  }
}

function audioField(kind, field) {
  return byId(`${kind}-${field.replaceAll('_', '-')}`);
}

function lockAudio(kind) {
  const state = audioSettings[kind];
  byId(`${kind}-fields`).disabled = !state.ready || state.busy;
  byId(`save-${kind}`).disabled = !state.ready || state.busy;
  byId(`refresh-${kind}`).disabled = state.busy;
}

function audioRequired(kind) {
  for (const field of audioSettings[kind].fields) audioField(kind, field).required = audioField(kind, 'enabled').checked;
}

async function readAudio(kind) {
  const state = audioSettings[kind];
  const request = ++state.read;
  const data = await api(state.path);
  if (!data || typeof data.enabled !== 'boolean' || typeof data.api_key_set !== 'boolean' ||
      state.fields.some((field) => typeof data[field] !== 'string') ||
      !state.options.includes(data[state.choice]) ||
      (kind === 'tts' && typeof data.ready !== 'boolean')) throw new ApiError(200, 'invalid_response');
  if (request !== state.read) return;
  audioField(kind, 'enabled').checked = data.enabled;
  for (const field of state.fields) audioField(kind, field).value = data[field];
  byId(`${kind}-form`).elements.namedItem(state.choice).value = data[state.choice];
  audioField(kind, 'key').value = '';
  audioField(kind, 'key').disabled = false;
  audioField(kind, 'clear_key').checked = false;
  audioField(kind, 'clear_key').disabled = !data.api_key_set;
  state.ready = true;
  lockAudio(kind);
  audioRequired(kind);
  const unavailable = kind === 'tts' && data.enabled && !data.ready;
  announce(`${kind}-status`, unavailable ? '配置已保存，但服务器缺少朗读运行依赖，暂时无法播放；请检查部署说明。' :
    `服务器${data.enabled ? '已启用' : '当前未启用'}${state.label}。`, unavailable);
}

for (const [kind, state] of Object.entries(audioSettings)) {
  const form = byId(`${kind}-form`);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!state.ready || state.busy) return;
    const urlInput = audioField(kind, 'base_url');
    urlInput.setCustomValidity('');
    if (urlInput.value.trim()) {
      try { const url = new URL(urlInput.value.trim()); if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error(); }
      catch { urlInput.setCustomValidity('请输入不含账号、密钥、查询参数或片段的 http/https 地址。'); }
    }
    for (const field of state.fields.filter((name) => name !== 'base_url')) {
      const input = audioField(kind, field);
      input.setCustomValidity(input.required && !input.value.trim() ? '启用时请填写此项。' : '');
    }
    if (!form.reportValidity()) return;
    const body = {enabled: audioField(kind, 'enabled').checked};
    for (const field of state.fields) body[field] = audioField(kind, field).value.trim();
    body[state.choice] = form.elements.namedItem(state.choice).value;
    if (audioField(kind, 'clear_key').checked) body.api_key = '';
    else if (audioField(kind, 'key').value.trim()) body.api_key = audioField(kind, 'key').value.trim();
    state.busy = true;
    const epoch = authEpoch;
    lockAudio(kind);
    announce(`${kind}-status`, `正在保存${state.label}设置…`);
    let confirmed = false;
    try {
      await api(state.path, {method: 'PUT', body});
      confirmed = true;
      audioField(kind, 'key').value = '';
      await readAudio(kind);
    } catch (error) {
      if (authEpoch === epoch && session) announce(`${kind}-status`, confirmed ? '服务器已确认保存，但状态未能重新读取；请刷新核对。' : friendly(error), true);
    } finally {
      if (authEpoch === epoch) { state.busy = false; lockAudio(kind); }
    }
  });
  for (const field of state.fields) {
    const input = audioField(kind, field);
    input.setAttribute('aria-invalid', 'false');
    input.addEventListener('invalid', () => input.setAttribute('aria-invalid', 'true'));
    input.addEventListener('input', () => { input.setCustomValidity(''); input.setAttribute('aria-invalid', 'false'); });
  }
  audioField(kind, 'enabled').addEventListener('change', () => audioRequired(kind));
  audioField(kind, 'clear_key').addEventListener('change', () => {
    const key = audioField(kind, 'key');
    key.disabled = audioField(kind, 'clear_key').checked;
    if (key.disabled) key.value = '';
  });
  byId(`refresh-${kind}`).addEventListener('click', async () => {
    if (state.busy) return;
    state.busy = true;
    const epoch = authEpoch;
    lockAudio(kind);
    try { await readAudio(kind); }
    catch (error) { report(`${kind}-status`, error); }
    finally { if (authEpoch === epoch) { state.busy = false; lockAudio(kind); } }
  });
}

loginForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!loginForm.reportValidity()) return;
  const button = byId('login-button');
  if (button.disabled) return;
  button.disabled = true;
  announce('login-status', '正在登录服务器…');
  const epoch = authEpoch;
  try {
    const result = await rawFetch('/duet/auth/login', {method: 'POST', body: {
      username: byId('username').value.trim(), password: byId('password').value, device_name: 'duet 网页管理',
    }});
    if (epoch !== authEpoch) throw new ApiError(0, 'session_changed');
    session = pair(result);
    authEpoch += 1;
    byId('password').value = '';
    byId('password').type = 'password';
    byId('show-password').setAttribute('aria-pressed', 'false');
    byId('show-password').textContent = '显示密码';
    byId('login-panel').hidden = true;
    byId('dashboard').hidden = false;
    byId('account-tools').hidden = false;
    byId('account-name').textContent = session.user.display_name || session.user.username || 'duet';
    newModel(false);
    announce('dashboard-status', '正在读取服务器状态…');
    byId('dashboard-heading').focus();
    try { await reloadAll(); announce('dashboard-status'); }
    catch (error) { report('dashboard-status', error); }
  } catch (error) { report('login-status', error); }
  finally { button.disabled = false; }
});

byId('show-password').addEventListener('click', () => {
  const show = byId('password').type === 'password';
  byId('password').type = show ? 'text' : 'password';
  byId('show-password').setAttribute('aria-pressed', String(show));
  byId('show-password').textContent = show ? '隐藏密码' : '显示密码';
});

byId('logout').addEventListener('click', async () => {
  byId('logout').disabled = true;
  let confirmed = false;
  try { await api('/duet/auth/logout', {method: 'POST'}); confirmed = true; }
  catch { /* Local credentials still must be discarded if the server is offline. */ }
  finally {
    signedOut(confirmed ? '已退出登录，服务器已撤销此浏览器会话。' : '已清除本页登录信息；服务器未确认撤销，可稍后从设备列表检查。');
    byId('logout').disabled = false;
    byId('username').focus();
  }
});

modelForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (modelBusy) return;
  const urlInput = byId('base-url');
  urlInput.setCustomValidity('');
  try {
    const url = new URL(urlInput.value.trim());
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error();
  } catch { urlInput.setCustomValidity('请输入 http/https 基础地址，不含账号、密钥、查询参数或片段。'); }
  for (const id of ['model-label', 'model-name']) byId(id).setCustomValidity(byId(id).value.trim() ? '' : '请填写此项。');
  if (!modelForm.reportValidity()) return;
  const previous = editing;
  const body = {label: byId('model-label').value.trim(), protocol: modelForm.querySelector('input[name="protocol"]:checked').value,
    base_url: urlInput.value.trim(), model: byId('model-name').value.trim(),
    prompt_cache_enabled: byId('prompt-cache-enabled').checked, stream_usage_enabled: byId('stream-usage-enabled').checked};
  body.api_mode = body.protocol === 'openai' && byId('api-responses').checked ? 'responses' : 'chat_completions';
  body.reasoning_effort = body.protocol === 'openai' ? byId('reasoning-effort').value || null : null;
  body.prompt_cache_mode = body.api_mode === 'responses' ? byId('prompt-cache-mode').value || null : null;
  body.prompt_cache_ttl = body.api_mode === 'responses' ? byId('prompt-cache-ttl').value || null : null;
  if (byId('clear-key').checked) body.api_key = '';
  else if (byId('api-key').value.trim() || !previous) body.api_key = byId('api-key').value.trim();
  if (byId('max-tokens').value !== '') body.max_tokens = Number(byId('max-tokens').value);
  if (byId('clear-prompt').checked) body.system_prompt = '';
  else if (byId('system-prompt').value.trim() || !previous) body.system_prompt = byId('system-prompt').value;
  lockModels(true);
  announce('model-status', '正在保存到服务器…');
  let saved = false;
  try {
    const result = await api(previous ? `/duet/models/${encodeURIComponent(previous)}` : '/duet/models', {
      method: previous ? 'PUT' : 'POST', body,
    });
    if (!result || typeof result.id !== 'string') throw new ApiError(200, 'invalid_response');
    saved = true;
    byId('api-key').value = '';
    byId('system-prompt').value = '';
    await readModels();
    lockModels(false);
    const fresh = models.find((model) => model.id === result.id);
    if (fresh) editModel(fresh);
    announce('model-status', '服务器已保存。之后发送的消息会使用当前选用模型的最新配置。');
  } catch (error) {
    if (saved && session) announce('model-status', '服务器已确认保存，但列表刷新未完成。请刷新状态核对。', true);
    else report('model-status', error);
  } finally { lockModels(false); }
});

for (const id of ['base-url', 'model-label', 'model-name']) byId(id).addEventListener('input', () => byId(id).setCustomValidity(''));
byId('clear-key').addEventListener('change', () => { byId('api-key').disabled = byId('clear-key').checked; if (byId('clear-key').checked) byId('api-key').value = ''; });
byId('clear-prompt').addEventListener('change', () => { byId('system-prompt').disabled = byId('clear-prompt').checked; if (byId('clear-prompt').checked) byId('system-prompt').value = ''; });
byId('new-model').addEventListener('click', () => newModel());
for (const input of modelForm.querySelectorAll('input[name="protocol"], input[name="api_mode"]')) input.addEventListener('change', syncModelOptions);
byId('model-name').addEventListener('input', syncModelOptions);
byId('prompt-cache-enabled').addEventListener('change', syncModelOptions);
byId('cancel-edit').addEventListener('click', () => newModel());
byId('refresh-all').addEventListener('click', async () => {
  byId('refresh-all').disabled = true;
  announce('dashboard-status', '正在刷新服务器状态…');
  try { await reloadAll(); announce('dashboard-status', '服务器状态已刷新。'); }
  catch (error) { report('dashboard-status', error); }
  finally { byId('refresh-all').disabled = false; }
});
byId('refresh-devices').addEventListener('click', async () => {
  byId('refresh-devices').disabled = true;
  try { await readDevices(); announce('dashboard-status', '设备列表已刷新。'); }
  catch (error) { report('dashboard-status', error); }
  finally { byId('refresh-devices').disabled = false; }
});
byId('refresh-usage').addEventListener('click', async () => {
  byId('refresh-usage').disabled = true;
  try { await readUsage(); announce('usage-status', '已读取服务器实际调用记录。'); }
  catch (error) { report('usage-status', error); }
  finally { byId('refresh-usage').disabled = false; }
});

window.addEventListener('pagehide', () => { signedOut(); });
