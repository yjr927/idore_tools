'use strict';

const $ = selector => document.querySelector(selector);
const OPENROUTER = 'https://openrouter.ai';
const TIKWM = 'https://www.tikwm.com/api/';
const KAI_IMAGE = 'https://raw.githubusercontent.com/yjr927/idore_tools/bd19680c2f14b89be1a1025b7825d9e49bd4b433/reference-assets/kai-full.png';
const APPROVED_SOURCE = { author: 'bizzyboom', id: '7706633304702848276' };
const CLAY_MODEL = 'bytedance/seedance-2.5';
const JOURNAL_KEY = 'idore-create-journal-v1';
const OAUTH_KEY = 'idore-create-pkce-v1';
const LOCK_NAME = 'idore-openrouter-paid-video-v1';
const REMOTE_ID = /^[A-Za-z0-9_-]{1,160}$/;
const TERMINAL = new Set(['completed', 'failed', 'cancelled', 'expired']);
const STATUS_LABELS = { submitting: '正在提交', unknown: '提交结果待核对', pending: '等待生成', in_progress: '正在生成', completed: '生成完成', failed: '生成未完成', cancelled: '生成已取消', expired: '任务已过期' };
const MATERIALS = new Set(['male', 'female', 'mixed', 'unknown']);
const CASTS = new Set(['solo', 'group', 'unknown']);
const PRIVACY_REJECTION = '模型服务商拒绝了参考视频中的真人输入。此版本/路由需要其支持的授权人像流程；修改提示词或重新下载不能解决。';
let sessionKey = '', models = [], preparedReference = null, sourceVersion = 0, submitting = false, resolving = false, polling = false, oauthBusy = false, storageReady = false;
let journal = [], incomingClassification = null, incomingSource = '', pendingExtraGeneration = null;
let clayPreset = null, activeClayPreset = null, clayModelRequested = false;
const mediaResults = new Map(), jobBusy = new Set();

function node(tag, className, content) { const element = document.createElement(tag); if (className) element.className = className; if (content !== undefined) element.textContent = String(content); return element; }
function message(selector, text = '', state = '') { const element = $(selector); element.textContent = text; element.className = `message${state ? ' ' + state : ''}`; }
function error(text, extras = {}) { return Object.assign(new Error(text), extras); }
function base64url(bytes) { return btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, ''); }
function randomToken() { const bytes = crypto.getRandomValues(new Uint8Array(32)); return base64url(bytes); }
async function hash(value) { const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(typeof value === 'string' ? value : JSON.stringify(value))); return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join(''); }
function date(value) { const moment = new Date(value); return Number.isFinite(moment.getTime()) ? new Intl.DateTimeFormat('zh-CN', { timeZone: 'America/New_York', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(moment) : '时间待确认'; }

function tikTokSource(value) {
  try {
    const url = new URL(value), match = url.pathname.match(/^\/@([A-Za-z0-9_.-]{1,100})\/video\/(\d{10,25})\/?$/);
    if (url.protocol !== 'https:' || !['www.tiktok.com', 'tiktok.com'].includes(url.hostname) || url.username || url.password || url.port && url.port !== '443' || !match) return null;
    return { author: match[1], id: match[2], url: `https://www.tiktok.com/@${match[1]}/video/${match[2]}` };
  } catch { return null; }
}
function publicHttps(value) {
  if (typeof value !== 'string' || value.length > 4096 || /[\s\x00-\x1f]/.test(value)) return '';
  try {
    const url = new URL(value), host = url.hostname.toLowerCase();
    if (url.protocol !== 'https:' || url.username || url.password || url.port && url.port !== '443' || url.hash || !host.includes('.') || host === 'localhost' || /\.(local|internal|localhost)$/.test(host) || /^\[/.test(host) || /^\d+(?:\.\d+){0,3}$/.test(host)) return '';
    return url.href;
  } catch { return ''; }
}
function videoFile(value, allowResolvedCdn = false) {
  const checked = publicHttps(value); if (!checked) return '';
  const url = new URL(checked);
  if (allowResolvedCdn && /^(v16m|v19)\.tiktokcdn-us\.com$/.test(url.hostname) && /^\/[a-f0-9]{32}\/[a-f0-9]{8}\/video\/tos\/alisg\/[A-Za-z0-9_-]+\/[A-Za-z0-9_-]+\/?$/.test(url.pathname) && url.searchParams.getAll('mime_type').length === 1 && url.searchParams.get('mime_type') === 'video_mp4') return checked;
  if (/(^|\.)(tiktok\.com|douyin\.com|iesdouyin\.com|tiktokcdn-us\.com)$/.test(url.hostname)) return '';
  return /\.(mp4|webm|mov)$/i.test(url.pathname) ? checked : '';
}
function selectedModel() { return models.find(model => model.id === $('#model').value); }
function eligibleModel(model) { return model && typeof model.id === 'string' && /^bytedance\/seedance-[2-9][0-9]*(?:\.[0-9]+)*(?:-[a-z0-9-]+)?$/.test(model.id); }
function values(model, field, type) { return Array.isArray(model?.[field]) ? [...new Set(model[field].filter(value => typeof value === type && (type !== 'number' || Number.isInteger(value) && value > 0)))] : []; }

function validateClayPreset(value) {
  if (!value || value.status !== 'reviewed' || value.character !== 'KAI' || value.source_id !== APPROVED_SOURCE.id || value.model !== CLAY_MODEL || value.duration !== 6 || typeof value.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(value.sha256) || typeof value.media_url !== 'string') return null;
  try {
    const url = new URL(value.media_url);
    if (url.protocol !== 'https:' || url.host !== 'raw.githubusercontent.com' || url.username || url.password || url.search || url.hash || !/^\/yjr927\/idore_tools\/[a-f0-9]{40}\/reference-assets\/clay-bizzyboom\.mp4$/.test(url.pathname)) return null;
    return { status: 'reviewed', character: 'KAI', source_id: APPROVED_SOURCE.id, model: CLAY_MODEL, duration: 6, media_url: url.href, sha256: value.sha256 };
  } catch { return null; }
}
async function readClayPreset() {
  try {
    const url = new URL('./clay-reference.json', location.href);
    const response = await fetch(url.href, { credentials: 'omit', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer' });
    if (!response.ok || response.url && response.url !== url.href || Number(response.headers?.get('Content-Length')) > 8192) return null;
    const text = await response.text(); if (text.length > 8192) return null;
    return validateClayPreset(JSON.parse(text));
  } catch { return null; }
}
async function loadClayPreset() { clayPreset = await readClayPreset(); $('#clay-preset').hidden = !clayPreset; return clayPreset; }
function clayPresetProblem() {
  if (!activeClayPreset) return '';
  if ($('#reference-input').value.trim() !== activeClayPreset.media_url) return '动作参考已变化，请重新选择白模并预览。';
  if ($('#model').value !== CLAY_MODEL || Number($('#duration').value) !== 6 || $('#resolution').value !== '720p' || $('#aspect').value !== '9:16') return '这套白模方案请使用 Seedance 2.5、6 秒、720p、9:16；不会自动改用其他版本。';
  return '';
}
function applyClayParameters() {
  if (!activeClayPreset) return;
  $('#model').value = models.some(model => model.id === CLAY_MODEL) ? CLAY_MODEL : ''; configureModel();
  const model = selectedModel();
  for (const [selector, field, type, wanted] of [['#duration', 'supported_durations', 'number', 6], ['#resolution', 'supported_resolutions', 'string', '720p'], ['#aspect', 'supported_aspect_ratios', 'string', '9:16']]) $(selector).value = values(model, field, type).includes(wanted) ? String(wanted) : '';
  clayModelRequested = false; updateControls();
}
async function useClayPreset() {
  if (!clayPreset || submitting || resolving) return;
  invalidateReference(); activeClayPreset = { ...clayPreset }; clayModelRequested = true;
  $('#reference-input').value = activeClayPreset.media_url; classifySource();
  if (models.length) applyClayParameters();
  await prepareReference({ preventDefault() {} });
}
async function resolveClayReference() {
  const selected = activeClayPreset, current = await readClayPreset();
  if (!selected || !current || JSON.stringify(current) !== JSON.stringify(selected)) throw error('白模动作已更新或暂不可用，请重新选择并预览。');
  let response;
  try { response = await fetch(current.media_url, { credentials: 'omit', cache: 'force-cache', redirect: 'error', referrerPolicy: 'no-referrer' }); }
  catch { throw error('暂时无法读取白模动作，请稍后重新预览。'); }
  if (!response.ok || response.url && response.url !== current.media_url || Number(response.headers?.get('Content-Length')) > 50 * 1024 * 1024 || !response.body?.getReader) throw error('白模动作暂时无法核对，没有提交生成。');
  const reader = response.body.getReader(), chunks = []; let length = 0;
  try {
    while (true) { const part = await reader.read(); if (part.done) break; length += part.value.byteLength; if (length > 50 * 1024 * 1024) throw error('白模文件超过支持大小，没有提交生成。'); chunks.push(part.value); }
  } finally { try { await reader.cancel(); } catch { /* A completed stream may already be closed. */ } }
  const bytes = new Uint8Array(length); let offset = 0; for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  const digest = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), byte => byte.toString(16).padStart(2, '0')).join('');
  if (length < 12 || String.fromCharCode(...bytes.subarray(4, 8)) !== 'ftyp' || digest !== current.sha256) throw error('白模动作与已审核版本不一致，已停止生成。');
  const duration = await directVideoMetadata(current.media_url);
  if (Math.abs(duration - current.duration) > .12) throw error('白模动作时长与审核版本不一致，已停止生成。');
  return { kind: 'clay', sourceKey: `clay:${current.source_id}:${current.sha256}`, source: null, url: current.media_url, duration, title: '已审核的白模动作 · KAI 单人跟拍', resolvedAt: Date.now() };
}

function readJournal() {
  try {
    const saved = JSON.parse(localStorage.getItem(JOURNAL_KEY) || '[]');
    if (!Array.isArray(saved)) throw new Error();
    journal = saved.filter(item => item && typeof item.localId === 'string' && /^[a-f0-9]{64}$/.test(item.requestHash) && typeof item.requestToken === 'string' && Object.hasOwn(STATUS_LABELS, item.status)).map(item => ({ localId: item.localId, requestHash: item.requestHash, requestToken: item.requestToken, status: item.status === 'submitting' ? 'unknown' : item.status, remoteId: typeof item.remoteId === 'string' && REMOTE_ID.test(item.remoteId) && !item.remoteId.startsWith('sk-') ? item.remoteId : null, model: typeof item.model === 'string' && eligibleModel({ id: item.model }) ? item.model : '', duration: item.duration, resolution: item.resolution, aspect: item.aspect, createdAt: item.createdAt, updatedAt: item.updatedAt, lastCheckedAt: item.lastCheckedAt || 0, cost: typeof item.cost === 'number' && Number.isFinite(item.cost) ? item.cost : null, sourceHash: typeof item.sourceHash === 'string' && /^[a-f0-9]{64}$/.test(item.sourceHash) ? item.sourceHash : '', policyCode: item.policyCode === 'real_person_video' ? item.policyCode : '', note: typeof item.note === 'string' && ['当前授权无法读取这条记录，请核对原来的 OpenRouter 账号。', '暂时无法查询进度，稍后会继续查询。'].includes(item.note) ? item.note : '' }));
    localStorage.setItem(JOURNAL_KEY, JSON.stringify(journal)); storageReady = true;
  } catch { journal = []; storageReady = false; }
}
function saveJournal() {
  try { localStorage.setItem(JOURNAL_KEY, JSON.stringify(journal)); storageReady = true; }
  catch { storageReady = false; throw error('无法保存任务记录。为避免重复扣费，请先允许本站使用浏览器存储。'); }
}
function updateEntry(localId, changes) { const entry = journal.find(item => item.localId === localId); if (!entry) return null; Object.assign(entry, changes, { updatedAt: new Date().toISOString() }); saveJournal(); renderJobs(); return entry; }
function pruneJournal() { if (journal.length < 150) return; const keep = journal.filter(item => !TERMINAL.has(item.status)); const finished = journal.filter(item => TERMINAL.has(item.status)).sort((a, b) => new Date(b.createdAt) - new Date(a.createdAt)).slice(0, Math.max(0, 149 - keep.length)); journal = [...keep, ...finished]; if (journal.length >= 150) throw error('待核对的任务较多，请先处理生成记录，再创建新的付费任务。'); }

function apiPathAllowed(path, method) {
  return method === 'POST' && ['/auth/keys', '/videos'].includes(path) || method === 'GET' && (['/key', '/videos/models'].includes(path) || /^\/videos\/[A-Za-z0-9_-]{1,160}(?:\/content\?index=0)?$/.test(path));
}
async function boundedErrorText(response) {
  if (!response.body?.getReader) return '';
  const reader = response.body.getReader(), decoder = new TextDecoder(); let count = 0, text = '';
  try { while (count < 65536) { const part = await reader.read(); if (part.done) break; const chunk = part.value.subarray(0, 65536 - count); count += chunk.byteLength; text += decoder.decode(chunk, { stream: true }); if (part.value.byteLength > chunk.byteLength) break; } text += decoder.decode(); }
  catch { return ''; }
  finally { try { await reader.cancel(); } catch { /* The body may already be closed. */ } }
  return text;
}
function realPersonRejection(text) { return text.includes('InputVideoSensitiveContentDetected.PrivacyInformation') || /may contain (?:a )?real person/i.test(text); }
async function router(path, { method = 'GET', body, binary = false, authenticated = true } = {}) {
  if (!apiPathAllowed(path, method)) throw error('这项服务操作不受支持。');
  if (authenticated && !sessionKey) throw error('请先连接 OpenRouter。', { status: 401 });
  const headers = { Accept: binary ? 'video/mp4' : 'application/json' };
  if (authenticated) headers.Authorization = 'Bearer ' + sessionKey;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  let response;
  try { response = await fetch(OPENROUTER + '/api/v1' + path, { method, headers, ...(body === undefined ? {} : { body: JSON.stringify(body) }), mode: 'cors', credentials: 'omit', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer' }); }
  catch { throw error('无法取得 OpenRouter 的响应。', { ambiguous: method === 'POST' && path === '/videos' }); }
  if (!response.ok) {
    if ([400, 422].includes(response.status) && realPersonRejection(await boundedErrorText(response))) throw error(PRIVACY_REJECTION, { status: response.status, policyCode: 'real_person_video' });
    const messages = { 400: '模型或素材参数不被支持，请检查输入。', 401: '授权已失效，请重新连接 OpenRouter。', 402: 'OpenRouter 余额或本次授权额度不足。', 403: '当前授权没有这项操作的权限。', 404: '当前模型或任务无法读取。', 409: '服务暂时无法接受这项请求。', 422: '素材或生成参数未通过检查。', 429: '请求较多，请稍后再操作。' };
    throw error(messages[response.status] || 'OpenRouter 暂未返回可确认的结果。', { status: response.status, ambiguous: method === 'POST' && path === '/videos' && response.status >= 500 });
  }
  if (binary) {
    const size = Number(response.headers.get('Content-Length')); if (Number.isFinite(size) && size > 128 * 1024 * 1024) throw error('视频文件较大，请在 OpenRouter 打开或下载。');
    const blob = await response.blob(); if (!blob.size || blob.size > 128 * 1024 * 1024) throw error('返回的视频文件无法在本页读取。'); return blob;
  }
  try { return await response.json(); } catch { throw error('服务没有返回可确认的任务信息。', { ambiguous: method === 'POST' && path === '/videos' }); }
}

function paintConnection() {
  $('#connect-button').hidden = Boolean(sessionKey); $('#disconnect-button').hidden = !sessionKey;
  $('#connection-status').textContent = sessionKey ? '已连接 · 本页使用你自己的 OpenRouter 余额' : '尚未连接 · 使用你自己的 OpenRouter 余额'; updateControls(); renderJobs();
}
async function connect() {
  if (oauthBusy || submitting) return; oauthBusy = true; $('#connect-button').disabled = true;
  try {
    const verifier = randomToken(), state = randomToken(), challenge = base64url(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))));
    const callback = new URL(location.origin + location.pathname), source = tikTokSource($('#reference-input').value.trim());
    if (activeClayPreset) callback.searchParams.set('clay', activeClayPreset.sha256);
    else if (source) { callback.searchParams.set('reference', source.url); callback.searchParams.set('material', $('#reference-material').value); callback.searchParams.set('cast', $('#reference-cast').value); }
    sessionStorage.setItem(OAUTH_KEY, JSON.stringify({ verifier, state, issuedAt: Date.now(), callback: callback.href }));
    const target = new URL('/auth', OPENROUTER); target.search = new URLSearchParams({ callback_url: callback.href, code_challenge: challenge, code_challenge_method: 'S256', state, key_label: 'idore character video' }).toString();
    location.assign(target.href);
  } catch { message('#auth-message', '无法开始授权。请允许本站使用当前标签页存储，并使用 HTTPS 页面。', 'error'); oauthBusy = false; $('#connect-button').disabled = false; }
}
async function handleOAuth() {
  const params = new URLSearchParams(location.search), codes = params.getAll('code'), states = params.getAll('state');
  if (!codes.length && !params.has('error')) return;
  const denied = params.has('error');
  let pending = null; try { pending = JSON.parse(sessionStorage.getItem(OAUTH_KEY) || 'null'); } catch { /* Reject invalid pending state below. */ }
  sessionStorage.removeItem(OAUTH_KEY); params.delete('code'); params.delete('state'); params.delete('error'); params.delete('error_description');
  const clean = location.pathname + (params.toString() ? '?' + params.toString() : '') + location.hash; history.replaceState(null, '', clean);
  if (denied || codes.length !== 1 || states.length !== 1 || !pending || typeof pending.verifier !== 'string' || typeof pending.state !== 'string' || states[0] !== pending.state || !Number.isFinite(pending.issuedAt) || Date.now() - pending.issuedAt < 0 || Date.now() - pending.issuedAt > 10 * 60 * 1000) { message('#auth-message', '授权已过期或未对应本标签页，请重新连接。', 'error'); return; }
  try {
    const expected = new URL(pending.callback); if (expected.origin !== location.origin || expected.pathname !== location.pathname) throw new Error();
    message('#auth-message', '正在完成 OpenRouter 授权…');
    const result = await router('/auth/keys', { method: 'POST', authenticated: false, body: { code: codes[0], code_verifier: pending.verifier, code_challenge_method: 'S256' } });
    if (typeof result.key !== 'string' || result.key.length < 8 || result.key.length > 512 || !/^[\x21-\x7e]+$/.test(result.key)) throw new Error();
    sessionKey = result.key; message('#auth-message', '连接成功，尚未提交任何付费生成。', 'success'); paintConnection();
  } catch { sessionKey = ''; message('#auth-message', '未完成授权，请重新连接 OpenRouter。本站不会保存 API 密钥。', 'error'); paintConnection(); }
}
function disconnect() { if (submitting) return; sessionKey = ''; message('#auth-message', '已断开本页连接。生成记录仍保存在这个浏览器。'); paintConnection(); }

function incoming() {
  const params = new URLSearchParams(location.search), supplied = params.get('reference');
  $('#reference-input').value = supplied && supplied.length <= 4096 ? supplied : `https://www.tiktok.com/@${APPROVED_SOURCE.author}/video/${APPROVED_SOURCE.id}`;
  const source = tikTokSource($('#reference-input').value.trim()); incomingSource = source?.url || '';
  incomingClassification = { material: MATERIALS.has(params.get('material')) ? params.get('material') : 'unknown', cast: CASTS.has(params.get('cast')) ? params.get('cast') : 'unknown' };
  classifySource();
}
function classifySource() {
  if (activeClayPreset && $('#reference-input').value.trim() === activeClayPreset.media_url) {
    $('#reference-material').value = 'male'; $('#reference-cast').value = 'solo'; $('#reference-material').disabled = true; $('#reference-cast').disabled = true;
    $('#classification-note').textContent = '已审核的白模动作：男生、单人。白模只提供动作，形象使用 KAI。'; return;
  }
  const source = tikTokSource($('#reference-input').value.trim()), approved = source && source.id === APPROVED_SOURCE.id && source.author.toLowerCase() === APPROVED_SOURCE.author;
  const originalContext = source && source.url === incomingSource ? incomingClassification : null;
  const material = originalContext && originalContext.material !== 'unknown' ? originalContext.material : approved ? 'male' : 'unknown', cast = originalContext && originalContext.cast !== 'unknown' ? originalContext.cast : approved ? 'solo' : 'unknown';
  $('#reference-material').value = material; $('#reference-cast').value = cast;
  $('#reference-material').disabled = approved || ['male', 'female', 'mixed'].includes(material); $('#reference-cast').disabled = approved || ['solo', 'group'].includes(cast);
  $('#classification-note').textContent = approved && material === 'male' && cast === 'solo' ? '这条指定的 Bizzyboom 原片已确认：男生、单人。' : originalContext && (material !== 'unknown' || cast !== 'unknown') ? '沿用素材库已有标记；女生、混合或多人参考不能用于这次 KAI 单人跟拍。' : '分类尚未确认。请先看原片，再明确选择男生与单人，不会自动猜测。';
}
function classificationProblem() { if ($('#reference-material').value !== 'male') return $('#reference-material').value === 'unknown' ? '请先确认参考素材分组；不会自动把未知素材判为男生。' : '这条参考与 KAI 的男生角色分组不匹配，请选择男生单人参考。'; if ($('#reference-cast').value !== 'solo') return '这次只生成 KAI 单人，请选择并确认单人参考。'; return ''; }
function invalidateReference() { sourceVersion++; preparedReference = null; activeClayPreset = null; clayModelRequested = false; pendingExtraGeneration = null; $('#new-generation-button').hidden = true; $('#source-consent').checked = false; $('#paid-consent').checked = false; $('#reference-preview').hidden = true; $('#source-video').removeAttribute('src'); $('#source-video').load(); message('#reference-message'); message('#generation-message'); message('#clay-preset-message'); updateControls(); }

async function resolveTikTok(source) {
  let response;
  try { response = await fetch(TIKWM, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8', Accept: 'application/json' }, body: new URLSearchParams({ url: source.url, hd: '0' }), credentials: 'omit', mode: 'cors', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer' }); }
  catch { throw error('暂时无法读取这条公开原片。请稍后再试，或使用可读取的视频文件直链。'); }
  if (!response.ok) throw error('原片解析服务暂不可用，没有提交视频生成。');
  let result; try { result = await response.json(); } catch { throw error('原片没有返回可核对的视频信息。'); }
  const data = result?.data, author = data?.author?.unique_id;
  if (result?.code !== 0 || !data || typeof data.id !== 'string' || data.id !== source.id || typeof author !== 'string' || author.toLowerCase() !== source.author.toLowerCase()) throw error('解析结果与所选原片的编号或作者不一致，已停止。');
  const url = videoFile(data.play, true), duration = data.duration;
  if (!url || typeof duration !== 'number' || !Number.isFinite(duration) || duration < 2 || duration > 15.05) throw error('原片未返回支持的完整动作视频；本页需要约 2–15 秒的短视频。');
  return { sourceKey: `tiktok:${source.id}`, source, url, duration, title: typeof data.title === 'string' ? data.title.slice(0, 300) : `@${author} 的动作参考`, resolvedAt: Date.now() };
}
function directVideoMetadata(url) {
  return new Promise((resolve, reject) => {
    const video = node('video'); video.preload = 'metadata'; video.muted = true; video.playsInline = true;
    const timer = setTimeout(() => finish(error('浏览器暂时无法读取这个视频文件，请检查链接。')), 20000);
    function finish(failure) { clearTimeout(timer); video.onloadedmetadata = null; video.onerror = null; const duration = video.duration; video.removeAttribute('src'); video.load(); if (failure) reject(failure); else resolve(duration); }
    video.onloadedmetadata = () => { if (!Number.isFinite(video.duration) || video.duration < 2 || video.duration > 15.05 || !video.videoWidth || !video.videoHeight) finish(error('请使用可播放的约 2–15 秒单人动作视频。')); else finish(); };
    video.onerror = () => finish(error('这个视频文件无法在浏览器读取，请换用可公开读取的 HTTPS 文件直链。')); video.src = url; video.load();
  });
}
async function resolveReference() {
  const problem = classificationProblem(); if (problem) throw error(problem);
  if (activeClayPreset) {
    if ($('#reference-input').value.trim() !== activeClayPreset.media_url) throw error('动作参考已变化，请重新选择白模并预览。');
    return resolveClayReference();
  }
  const value = $('#reference-input').value.trim(), source = tikTokSource(value);
  if (source) return resolveTikTok(source);
  const url = videoFile(value); if (!url) throw error('请使用完整的公开 TikTok 原片链接，或 HTTPS MP4 / WebM / MOV 文件直链；不支持登录、私密视频或短链接。');
  return { sourceKey: 'direct:' + await hash(url), source: null, url, duration: await directVideoMetadata(url), title: '已选定的动作视频文件', resolvedAt: Date.now() };
}
function paintReference(reference) {
  $('#reference-preview').hidden = false; $('#reference-title').textContent = reference.title; $('#reference-original').href = reference.source?.url || reference.url;
  $('#reference-original').textContent = reference.kind === 'clay' ? '打开白模视频 ↗' : '打开原片 ↗';
  $('#source-video').src = reference.url; $('#source-video').load(); $('#reference-facts').textContent = `${reference.source ? '@' + reference.source.author + ' · ' : ''}${reference.duration} 秒 · 男生 · 单人 · ${reference.kind === 'clay' ? '白模只提供动作，成片使用 KAI 形象' : '视频地址仅在当前页面使用'}`;
}
async function prepareReference(event) {
  event.preventDefault(); if (resolving || submitting) return; resolving = true; const version = sourceVersion; $('#prepare-reference').disabled = true; message('#reference-message', activeClayPreset ? '正在核对白模动作，不消耗 OpenRouter 余额…' : '正在读取原片与动作文件，不消耗 OpenRouter 余额…'); updateControls();
  try { const reference = await resolveReference(); reference.sourceHash = await hash(reference.sourceKey); if (version !== sourceVersion) throw error('原片或分类已变化，请重新读取。'); preparedReference = reference; paintReference(reference); message('#reference-message', reference.kind === 'clay' ? '白模动作已准备。请预览并确认，再决定是否生成 KAI 视频。' : '动作参考已准备。生成前会再刷新一次文件地址。', 'success'); }
  catch (failure) { preparedReference = null; message('#reference-message', failure.message, 'error'); }
  finally { resolving = false; $('#prepare-reference').disabled = false; updateControls(); }
}

function chooseOptions(selector, options, preferred, label = value => value) {
  const select = $(selector), previous = select.value; select.replaceChildren();
  if (!options.length) { const option = node('option', '', '暂未取得支持范围'); option.value = ''; select.append(option); return; }
  for (const value of options) { const option = node('option', '', label(value)); option.value = String(value); select.append(option); }
  select.value = String(options.find(value => String(value) === previous) ?? (options.includes(preferred) ? preferred : options[0]));
}
function configureModel() {
  const model = selectedModel(); chooseOptions('#duration', values(model, 'supported_durations', 'number'), 6, value => `${value} 秒`); chooseOptions('#resolution', values(model, 'supported_resolutions', 'string'), '720p'); chooseOptions('#aspect', values(model, 'supported_aspect_ratios', 'string'), '9:16');
  $('#model-note').textContent = model ? `${model.id} · 参数范围来自 OpenRouter 当前模型列表，不会自动更换版本。` : '请明确选择这次使用的 Seedance 模型版本。'; pendingExtraGeneration = null; $('#new-generation-button').hidden = true; updateControls();
}
async function loadModels({ fresh = false } = {}) {
  $('#reload-models').disabled = true;
  try {
    const result = await router('/videos/models', { authenticated: Boolean(sessionKey) }), next = (Array.isArray(result.data) ? result.data : []).filter(eligibleModel);
    if (fresh) { models = next; return; }
    const previous = $('#model').value; models = next; const placeholder = node('option', '', next.length ? '选择本次使用的模型版本' : '暂无可用 Seedance 2+ 模型'); placeholder.value = ''; $('#model').replaceChildren(placeholder);
    for (const model of next) { const option = node('option', '', model.name || model.id); option.value = model.id; $('#model').append(option); }
    $('#model').value = next.some(model => model.id === previous) ? previous : ''; if (clayModelRequested && activeClayPreset) applyClayParameters(); else configureModel();
  } catch (failure) { if (fresh) throw failure; message('#generation-message', failure.message, 'error'); }
  finally { $('#reload-models').disabled = false; updateControls(); }
}
function motionPrompt() {
  const duration = Number($('#duration').value) || 6, aspect = $('#aspect').value || '9:16', extra = $('#extra-prompt').value.trim();
  if (activeClayPreset) return ['Task: new reference-to-video generation using a motion-only video reference. Create a new 6-second, 9:16 video of one fictional male character, KAI.', "Image 1 is the sole source of KAI's identity and appearance. Preserve the face, facial proportions, hair, natural skin, body proportions, complete clothing and butterfly accessories shown in Image 1. Retain the sheer black fabric and light-colored trousers. KAI's identity and outfit remain consistent throughout.", 'Video 1 is the single reviewed faceless clay motion study. Use Video 1 only for visible body and finger movements, their order, timing, rhythm, orientation, framing and camera movement. Follow the actual visible sequence without invented choreography or transformations.', 'Set the new video in a bright room with white walls and a warm wood floor, lit by soft daylight.', 'Render every frame as KAI from Image 1. The clay figure supplies motion only. Keep its faceless head, white plastic material, simplified body and background out of the appearance of KAI. Preserve coherent anatomy, natural stable hands with five distinct fingers, believable joints and continuous contact and occlusion.', extra ? 'Additional preferences, subordinate to the reference roles: ' + extra : '', 'Exactly one KAI character. No morphing into the clay figure. No subtitles. No audio.'].filter(Boolean).join('\n\n');
  return [`Create a ${duration}-second video in ${aspect} format with one male character, KAI. Use the two supplied references for distinct roles.`, "IMAGE reference defines character identity and wardrobe only. Preserve the same face, facial structure, hairstyle, body proportions, gender presentation, clothing fit, colors and accessories throughout. Retain the butterfly accessories, black sheer fabric and light-colored trousers visible in the supplied image. Do not copy the image's still pose as the animation or freeze the character in that pose.", 'VIDEO reference supplies the actual motion, timing, rhythm, pose changes, framing and camera movement. Animate KAI through the visible gesture/dance sequence, following the observed order, directions, movement size, hand shapes, wrist angles, arm paths and pauses. Transfer only what is visible. Do not invent a second-by-second action schedule, extra choreography, hidden gestures or new camera moves.', `Use the opening ${duration} seconds of the video reference, retaining its visible order and pacing rather than looping gestures or adding a closing pose.`, "Keep KAI's identity and outfit. Do not copy the video performer's face, hair, clothing or accessories. Follow the video's camera behavior without importing player controls, captions or watermarks.", 'Hands and fingers must stay anatomically plausible with natural wrist and knuckle articulation, stable finger shapes, believable contact and occlusion. Avoid extra, missing or fused fingers, warped arms, duplicate hands, flicker and motion trails.', extra ? 'Additional preferences, subordinate to the reference roles: ' + extra : '', 'Keep exactly one character. No additional people, duplicate bodies, outfit or makeup transformation, costume reveal, identity morph, hairstyle change or new accessories. Keep the source-image look throughout. No added text or logos. Do not generate audio.'].filter(Boolean).join('\n\n');
}
function updateControls() {
  const model = selectedModel(), supported = model && values(model, 'supported_durations', 'number').includes(Number($('#duration').value)) && values(model, 'supported_resolutions', 'string').includes($('#resolution').value) && values(model, 'supported_aspect_ratios', 'string').includes($('#aspect').value);
  const blocked = knownSourceRejection();
  const clayProblem = clayPresetProblem();
  $('#generate-button').disabled = submitting || resolving || !sessionKey || !preparedReference || Boolean(classificationProblem()) || Boolean(blocked) || Boolean(clayProblem) || !supported || !storageReady || !navigator.locks?.request;
  $('#connect-button').disabled = submitting || oauthBusy; $('#disconnect-button').disabled = submitting;
  $('#use-clay-reference').disabled = submitting || resolving;
  message('#clay-preset-message', activeClayPreset ? clayProblem || '已选定白模方案。预览与连接不会开始付费生成。' : '', clayProblem ? 'error' : 'success');
  message('#provider-policy-note', blocked, blocked ? 'error' : '');
  $('#prompt-preview').textContent = motionPrompt();
}
function knownSourceRejection() {
  const source = tikTokSource($('#reference-input').value.trim());
  if (source?.id === APPROVED_SOURCE.id && source.author.toLowerCase() === APPROVED_SOURCE.author) return PRIVACY_REJECTION + ' 这条指定原片已确认被拒绝，本站已阻止再次付费尝试；仍可读取预览。';
  if (preparedReference?.sourceHash && journal.some(item => item.sourceHash === preparedReference.sourceHash && item.policyCode === 'real_person_video')) return PRIVACY_REJECTION;
  return '';
}
function intentSnapshot() {
  const model = selectedModel(), problem = classificationProblem(); if (problem) throw error(problem);
  if (knownSourceRejection()) throw error(knownSourceRejection());
  if (clayPresetProblem()) throw error(clayPresetProblem());
  if (!sessionKey) throw error('请先连接 OpenRouter。'); if (!preparedReference) throw error('请先读取原片。');
  if (!eligibleModel(model)) throw error('请选择明确的 Seedance 2 或更新模型。');
  const duration = Number($('#duration').value), resolution = $('#resolution').value, aspect = $('#aspect').value;
  if (!values(model, 'supported_durations', 'number').includes(duration) || !values(model, 'supported_resolutions', 'string').includes(resolution) || !values(model, 'supported_aspect_ratios', 'string').includes(aspect)) throw error('时长、分辨率或画幅未获得模型支持。');
  if (!$('#source-consent').checked || !$('#paid-consent').checked) throw error('请确认素材传输与本次生成费用。');
  return { model: model.id, duration, resolution, aspect_ratio: aspect, generate_audio: false, image: KAI_IMAGE, source: preparedReference.sourceKey, prompt: motionPrompt(), provider: { only: ['seed', 'byteplus'] } };
}

async function submit(event, extraIntent = null) {
  event?.preventDefault(); if (submitting) return; submitting = true; updateControls();
  try {
    if (!navigator.locks?.request) throw error('这个浏览器暂不支持安全任务锁，请使用最新版 Chrome 或 Safari。');
    await navigator.locks.request(LOCK_NAME, { mode: 'exclusive', ifAvailable: true }, async lock => {
      if (!lock) throw error('另一个标签页正在提交，请先查看生成记录。');
      const intent = intentSnapshot(), version = sourceVersion, requestHash = await hash(intent), originalInput = $('#reference-input').value.trim();
      readJournal(); if (!storageReady) throw error('请先允许本站保存任务记录，再生成视频。');
      const matching = journal.filter(item => item.requestHash === requestHash), prior = matching.sort((a, b) => new Date(b.createdAt) - new Date(a.createdAt))[0];
      if (matching.some(item => ['unknown', 'submitting'].includes(item.status))) throw error('这组方案上次的提交结果还不明确，已阻止重复付费。请先核对 OpenRouter 记录。');
      if (prior && !extraIntent) { message('#generation-message', '这个方案已有任务，未重复提交。请查看下方记录；需要新结果时可明确再生成。', 'success'); if (TERMINAL.has(prior.status)) { pendingExtraGeneration = requestHash; $('#new-generation-button').hidden = false; } renderJobs(); return; }
      if (extraIntent && (extraIntent !== requestHash || !prior || !TERMINAL.has(prior.status))) throw error('当前方案或原任务状态已变化，请先重新检查。');
      message('#generation-message', '正在刷新动作视频地址与模型能力，尚未提交付费生成…');
      const refreshed = await resolveReference(); await loadModels({ fresh: true });
      if (version !== sourceVersion || originalInput !== $('#reference-input').value.trim() || refreshed.sourceKey !== intent.source || JSON.stringify(intentSnapshot()) !== JSON.stringify(intent)) throw error('素材或参数已变化，尚未提交生成，请重新检查。');
      const current = models.find(model => model.id === intent.model);
      if (!current || !values(current, 'supported_durations', 'number').includes(intent.duration) || !values(current, 'supported_resolutions', 'string').includes(intent.resolution) || !values(current, 'supported_aspect_ratios', 'string').includes(intent.aspect_ratio)) throw error('模型当前能力已变化，请刷新模型后重新选择。');
      refreshed.sourceHash = await hash(refreshed.sourceKey); preparedReference = refreshed; paintReference(refreshed); pruneJournal();
      const entry = { localId: crypto.randomUUID(), requestToken: crypto.randomUUID(), requestHash, status: 'submitting', remoteId: null, model: intent.model, duration: intent.duration, resolution: intent.resolution, aspect: intent.aspect_ratio, createdAt: new Date().toISOString(), updatedAt: new Date().toISOString(), lastCheckedAt: 0, cost: null, sourceHash: refreshed.sourceHash, policyCode: '', note: '' };
      journal.push(entry); saveJournal(); renderJobs();
      const body = { model: intent.model, prompt: intent.prompt, duration: intent.duration, resolution: intent.resolution, aspect_ratio: intent.aspect_ratio, generate_audio: false, provider: intent.provider, input_references: [{ type: 'image_url', image_url: { url: KAI_IMAGE } }, { type: 'video_url', video_url: { url: refreshed.url } }] };
      message('#generation-message', '正在提交 KAI 图片与原片动作视频，请勿重复点击。');
      try {
        const submittedKey = sessionKey;
        const result = await router('/videos', { method: 'POST', body });
        if (typeof result.id !== 'string' || !REMOTE_ID.test(result.id) || result.id.startsWith('sk-') || submittedKey && result.id.includes(submittedKey) || !['pending', 'in_progress', 'completed', 'failed', 'cancelled', 'expired'].includes(result.status)) throw error('没有取得可核对的任务编号或状态。', { ambiguous: true });
        updateEntry(entry.localId, { remoteId: result.id, status: result.status, lastCheckedAt: Date.now(), cost: typeof result.usage?.cost === 'number' && Number.isFinite(result.usage.cost) ? result.usage.cost : null }); rememberResults(result.id, result);
        pendingExtraGeneration = null; $('#new-generation-button').hidden = true; message('#generation-message', '任务已提交。视频由云端模型生成，可在下方查看进度。', 'success');
      } catch (failure) {
        updateEntry(entry.localId, { status: failure.ambiguous || !failure.status || failure.status >= 500 ? 'unknown' : 'failed', policyCode: failure.policyCode === 'real_person_video' ? 'real_person_video' : '' });
        throw error(failure.ambiguous || !failure.status || failure.status >= 500 ? '提交结果还不明确，可能已产生费用。已阻止相同方案重复提交，请先核对 OpenRouter 记录。' : failure.message);
      }
    });
  } catch (failure) { message('#generation-message', failure.message || '生成未能完成，请查看任务记录。', 'error'); }
  finally { submitting = false; updateControls(); }
}

function resultUrl(value) { return publicHttps(value); }
function rememberResults(remoteId, result) { const signed = Array.isArray(result.urls) ? result.urls.map(resultUrl).filter(Boolean) : []; if (signed.length) mediaResults.set(remoteId, { ...(mediaResults.get(remoteId) || {}), signed: signed[0], expiresAt: result.expires_at || null }); renderJobs(); }
async function refreshEntry(localId, { automatic = false } = {}) {
  const entry = journal.find(item => item.localId === localId); if (!entry?.remoteId || !sessionKey || jobBusy.has(localId)) return;
  if (Date.now() - (entry.lastCheckedAt || 0) < 30000) { if (!automatic) message('#jobs-message', '刚刚查询过这条任务，30 秒后会继续查询。'); return; }
  jobBusy.add(localId); updateEntry(localId, { lastCheckedAt: Date.now() });
  try {
    const result = await router('/videos/' + entry.remoteId);
    if (result.id !== entry.remoteId || !['pending', 'in_progress', 'completed', 'failed', 'cancelled', 'expired'].includes(result.status)) throw error('返回的任务信息无法核对。');
    updateEntry(localId, { status: result.status, note: '', cost: typeof result.usage?.cost === 'number' && Number.isFinite(result.usage.cost) ? result.usage.cost : entry.cost }); rememberResults(entry.remoteId, result);
  } catch (failure) { updateEntry(localId, { note: [401, 403, 404].includes(failure.status) ? '当前授权无法读取这条记录，请核对原来的 OpenRouter 账号。' : '暂时无法查询进度，稍后会继续查询。' }); }
  finally { jobBusy.delete(localId); renderJobs(); }
}
async function downloadEntry(localId) {
  const entry = journal.find(item => item.localId === localId); if (!entry?.remoteId || entry.status !== 'completed' || !sessionKey || jobBusy.has(localId)) return;
  jobBusy.add(localId); renderJobs();
  try { const blob = await router('/videos/' + entry.remoteId + '/content?index=0', { binary: true }); const prior = mediaResults.get(entry.remoteId); if (prior?.blob) URL.revokeObjectURL(prior.blob); mediaResults.set(entry.remoteId, { ...prior, blob: URL.createObjectURL(blob) }); message('#jobs-message', '视频已在当前页面准备好，可以预览并下载。', 'success'); }
  catch (failure) { message('#jobs-message', failure.message || '暂时无法读取成片，请在 OpenRouter 查看。', 'error'); }
  finally { jobBusy.delete(localId); renderJobs(); }
}
function activityLink() { const link = node('a', 'button small', '核对 OpenRouter 记录 ↗'); link.href = 'https://openrouter.ai/activity'; link.target = '_blank'; link.rel = 'noopener noreferrer'; return link; }
function renderJobs() {
  const list = $('#job-list'); if (!journal.length) { list.replaceChildren(node('p', 'empty-state', '第一条 KAI 作品，会出现在这里。')); return; }
  list.replaceChildren(...[...journal].sort((a, b) => new Date(b.createdAt) - new Date(a.createdAt)).map(entry => {
    const card = node('article', 'job-card'), top = node('div', 'job-top'); top.append(node('span', 'job-status ' + entry.status, STATUS_LABELS[entry.status]), node('span', 'job-time', date(entry.createdAt) + ' · 纽约')); card.append(top, node('h3', '', 'KAI · ' + entry.model), node('p', 'job-meta', `${entry.duration} 秒 · ${entry.resolution} · ${entry.aspect}${typeof entry.cost === 'number' ? ' · $' + entry.cost.toFixed(4) : ''}`));
    if (entry.remoteId) card.append(node('p', 'job-meta', '任务编号：' + entry.remoteId));
    if (entry.note) card.append(node('p', 'job-note', entry.note));
    if (entry.policyCode === 'real_person_video') card.append(node('p', 'job-note', PRIVACY_REJECTION));
    const actions = node('div', 'job-actions'), media = entry.remoteId ? mediaResults.get(entry.remoteId) : null;
    if (entry.status === 'unknown') { card.append(node('p', 'job-note', '提交结果不明确，已阻止自动重试。请先核对是否已有任务或扣费。')); actions.append(activityLink()); }
    if (entry.remoteId && ['pending', 'in_progress', 'unknown'].includes(entry.status)) { const button = node('button', 'button small', jobBusy.has(entry.localId) ? '正在查询…' : '查看进度'); button.type = 'button'; button.disabled = !sessionKey || jobBusy.has(entry.localId); button.addEventListener('click', () => refreshEntry(entry.localId)); actions.append(button); }
    if (entry.status === 'completed') {
      const videoSource = media?.blob || media?.signed; if (videoSource) { const video = node('video'); video.src = videoSource; video.controls = true; video.playsInline = true; video.preload = 'metadata'; video.setAttribute('aria-label', '生成的 KAI 视频'); card.append(video); }
      if (media?.blob) { const download = node('a', 'button small', '下载 MP4'); download.href = media.blob; download.download = `KAI-${entry.remoteId}.mp4`; actions.append(download); }
      else { const button = node('button', 'button small', jobBusy.has(entry.localId) ? '正在读取…' : '准备预览与下载'); button.type = 'button'; button.disabled = !sessionKey || jobBusy.has(entry.localId); button.addEventListener('click', () => downloadEntry(entry.localId)); actions.append(button); }
    }
    if (['failed', 'cancelled', 'expired'].includes(entry.status)) { card.append(node('p', 'job-note', '这次任务未取得可下载的视频。请在 OpenRouter 查看实际用量；本站不会自动重发。')); actions.append(activityLink()); }
    if (actions.children.length) card.append(actions); return card;
  }));
}
async function pollJobs(manual = false) {
  if (polling || !sessionKey) { if (manual && !sessionKey) message('#jobs-message', '请先重新连接 OpenRouter，再查看之前保存的任务。'); return; }
  polling = true;
  try { const candidates = journal.filter(entry => entry.remoteId && (!TERMINAL.has(entry.status) || entry.status === 'completed' && !mediaResults.has(entry.remoteId))).slice(0, 10); for (const entry of candidates) await refreshEntry(entry.localId, { automatic: !manual }); }
  finally { polling = false; }
}

async function init() {
  $('#character-image').src = KAI_IMAGE; incoming(); readJournal(); paintConnection();
  if (!storageReady) message('#generation-message', '请允许本站保存任务记录后再生成，防止中断时重复扣费。', 'error');
  else if (!navigator.locks?.request) message('#generation-message', '当前浏览器缺少安全任务锁，请使用最新版 Chrome 或 Safari。', 'error');
  await loadClayPreset();
  const requestedClay = new URLSearchParams(location.search).get('clay');
  if (requestedClay && clayPreset?.sha256 === requestedClay) await useClayPreset();
  else if (requestedClay) message('#reference-message', '之前选择的白模暂不可用，请重新选择动作参考。', 'error');
  await handleOAuth(); await loadModels(); renderJobs(); if (sessionKey) await pollJobs(); setInterval(() => pollJobs(), 30000);
}
$('#connect-button').addEventListener('click', connect); $('#disconnect-button').addEventListener('click', disconnect); $('#reference-form').addEventListener('submit', prepareReference); $('#generation-form').addEventListener('submit', event => submit(event));
$('#use-clay-reference').addEventListener('click', useClayPreset);
$('#reference-input').addEventListener('input', () => { invalidateReference(); classifySource(); updateControls(); });
for (const selector of ['#reference-material', '#reference-cast']) $(selector).addEventListener('change', invalidateReference);
$('#model').addEventListener('change', () => { clayModelRequested = false; configureModel(); }); $('#reload-models').addEventListener('click', () => loadModels());
for (const selector of ['#duration', '#resolution', '#aspect', '#extra-prompt']) $(selector).addEventListener('input', () => { pendingExtraGeneration = null; $('#new-generation-button').hidden = true; updateControls(); });
$('#new-generation-button').addEventListener('click', () => { if (!pendingExtraGeneration) return; submit(null, pendingExtraGeneration); }); $('#refresh-jobs').addEventListener('click', () => pollJobs(true));
window.addEventListener('storage', event => { if (event.key === JOURNAL_KEY && !submitting) { readJournal(); renderJobs(); updateControls(); } });
window.addEventListener('pagehide', () => { sessionKey = ''; for (const result of mediaResults.values()) if (result.blob) URL.revokeObjectURL(result.blob); mediaResults.clear(); });
window.addEventListener('pageshow', event => { if (event.persisted) { oauthBusy = false; paintConnection(); } });
init();
