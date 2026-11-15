'use strict';

const $ = selector => document.querySelector(selector);
const apiRoot = '/api/video';
const imageLimit = 5 * 1024 * 1024;
const submissionStorage = 'idore-video-submission-v1';
const statusLabels = { submitting: '正在提交', pending: '等待生成', queued: '等待生成', in_progress: '正在生成', completed: '生成完成', failed: '生成失败', cancelled: '生成已取消', expired: '任务已过期', submission_uncertain: '提交结果待核对' };
let connected = false, models = [], jobs = [], submitting = false, connecting = false, jobSignature = '', pollTimer;
let submissionRecord = null;
const files = new Map(), references = [], refreshing = new Set(), downloading = new Set(), lastRemoteRefresh = new Map();
try { const saved = JSON.parse(localStorage.getItem(submissionStorage) || 'null'); if (saved && typeof saved.key === 'string' && typeof saved.fingerprint === 'string') submissionRecord = saved; } catch { /* The page also works when browser storage is unavailable. */ }

function node(tag, className, content) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (content !== undefined) element.textContent = String(content);
  return element;
}
function message(selector, text = '', state = '') { const element = $(selector); element.textContent = text; element.className = `form-message${state ? ' ' + state : ''}`; }
async function api(path, method = 'GET', body) {
  let response;
  try {
    response = await fetch(apiRoot + path, { method, headers: body === undefined ? { Accept: 'application/json' } : { Accept: 'application/json', 'Content-Type': 'application/json' }, credentials: 'same-origin', cache: 'no-store', ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  } catch { const error = new Error('无法连接本机服务。请确认工作台仍在运行。'); error.transport = true; throw error; }
  let result;
  try { result = await response.json(); } catch { const error = new Error('本机服务没有返回有效结果。'); error.transport = true; throw error; }
  if (!response.ok) { const error = new Error(typeof result.error === 'string' ? result.error : '操作未完成，请稍后重试。'); error.status = response.status; throw error; }
  return result;
}
function persistSubmission(record) { submissionRecord = record; try { localStorage.setItem(submissionStorage, JSON.stringify(record)); } catch { /* Keep the request key in memory for this visit. */ } }
function currentModel() { return models.find(model => model.id === $('#model').value); }
function supported(model, parameter) {
  return Array.isArray(model?.supported_parameters) && model.supported_parameters.includes(parameter);
}
function capabilities(model) {
  const options = (key, type) => Array.isArray(model?.[key]) ? [...new Set(model[key].filter(value => typeof value === type && (type !== 'number' || Number.isFinite(value) && value > 0)))] : [];
  const frames = Array.isArray(model?.supported_frame_images) ? model.supported_frame_images : [];
  const generation = String(model?.id || '').match(/^bytedance\/seedance-(\d+)(?:\.|-|$)/);
  return { durations: options('supported_durations', 'number'), resolutions: options('supported_resolutions', 'string'), aspects: options('supported_aspect_ratios', 'string'), first: frames.includes('first_frame') || supported(model, 'first_frame') || model?.supports_first_frame === true, last: frames.includes('last_frame') || supported(model, 'last_frame') || model?.supports_last_frame === true, refs: supported(model, 'references') || model?.supports_references === true || Boolean(generation && Number(generation[1]) >= 2), audio: model?.generate_audio === true || supported(model, 'generate_audio') || model?.supports_audio === true };
}
function fillOptions(select, values, previous, preferred, label = value => value) {
  select.replaceChildren();
  if (!values.length) { const option = node('option', '', '模型未提供支持范围'); option.value = ''; select.append(option); return; }
  for (const value of values) { const option = node('option', '', label(value)); option.value = String(value); select.append(option); }
  const requested = values.find(value => String(value) === previous);
  select.value = String(requested ?? (values.includes(preferred) ? preferred : values[0]));
}
function updateGenerateButton() { const caps = capabilities(currentModel()); $('#generate-button').disabled = !connected || submitting || !currentModel() || !caps.durations.length || !caps.resolutions.length || !caps.aspects.length; }
function configureModel() {
  const model = currentModel(), caps = capabilities(model);
  fillOptions($('#duration'), caps.durations, $('#duration').value, undefined, value => `${value} 秒`);
  fillOptions($('#resolution'), caps.resolutions, $('#resolution').value, '720p');
  fillOptions($('#aspect-ratio'), caps.aspects, $('#aspect-ratio').value, '9:16');
  for (const option of $('#mode').options) option.disabled = option.value === 'frames' ? !caps.first : option.value === 'references' ? !caps.refs : false;
  if ($('#mode').selectedOptions[0]?.disabled) $('#mode').value = 'text';
  $('#generate-audio').disabled = !caps.audio;
  if (!caps.audio) $('#generate-audio').checked = false;
  $('#audio-note').textContent = caps.audio ? '所选模型支持声音生成' : '所选模型未声明支持声音生成';
  $('#model-note').textContent = model ? `模型 ID：${model.id}。${!caps.durations.length || !caps.resolutions.length || !caps.aspects.length ? '未取得完整支持范围，暂不能提交。' : '参数来自 OpenRouter 当前返回的模型能力。'}` : '请选择模型；不会自动替换你选择的版本。';
  configureMode(); updateGenerateButton();
}
function configureMode() {
  const mode = $('#mode').value, caps = capabilities(currentModel());
  $('#frames-fields').hidden = mode !== 'frames'; $('#references-fields').hidden = mode !== 'references'; $('#last-frame-field').hidden = !caps.last;
  $('#mode-note').textContent = mode === 'frames' ? (caps.last ? '首帧确定起始造型；尾帧可确定结束造型。' : '所选模型支持首帧；未声明尾帧支持。') : mode === 'references' ? '用角色图片控制形象，用视频参考动作。请在描述中说明各份素材的用途。' : '输入画面、动作与镜头描述，不附加图片或视频。';
  if (mode === 'references' && !references.length) addReference();
}
function paintConnection() {
  $('#connection-status').textContent = connected ? '已连接 · 密钥仅保存在本机服务内存' : '尚未连接';
  $('#connection-status').classList.toggle('connected', connected);
  $('#connection-form').hidden = connected; $('#disconnect-button').hidden = !connected;
  updateGenerateButton();
}
async function loadModels() {
  const result = await api('/models');
  const previous = $('#model').value;
  models = (Array.isArray(result.models) ? result.models : []).filter(model => model && typeof model.id === 'string' && model.id.startsWith('bytedance/seedance'));
  const placeholder = node('option', '', models.length ? '选择这次使用的模型版本' : '暂无可用的 Seedance 模型'); placeholder.value = '';
  $('#model').replaceChildren(placeholder);
  for (const model of models) { const option = node('option', '', model.name || model.id); option.value = model.id; $('#model').append(option); }
  $('#model').value = models.some(model => model.id === previous) ? previous : '';
  configureModel();
}
async function loadConnection() {
  const result = await api('/connection'); connected = result.connected === true; paintConnection();
  if (connected) await loadModels();
}
async function connect(event) {
  event.preventDefault(); if (connecting) return;
  const secret = $('#api-key').value.trim(); $('#api-key').value = '';
  if (!secret) { message('#connection-message', '请输入 OpenRouter API Key。', 'error'); return; }
  connecting = true; $('#connect-button').disabled = true; $('#connect-button').textContent = '正在验证…'; message('#connection-message');
  try { const result = await api('/connection', 'POST', { api_key: secret }); connected = result.connected === true; paintConnection(); if (!connected) throw new Error('连接尚未建立，请重新验证。'); await loadModels(); message('#connection-message', '连接成功。尚未提交生成任务。', 'success'); }
  catch (error) { message('#connection-message', error.message, 'error'); }
  finally { connecting = false; $('#connect-button').disabled = false; $('#connect-button').textContent = '验证并连接'; }
}
async function disconnect() {
  $('#disconnect-button').disabled = true;
  try { await api('/connection', 'DELETE', {}); connected = false; paintConnection(); message('#connection-message', '已清除本机服务中的密钥。'); }
  catch (error) { message('#connection-message', error.message, 'error'); }
  finally { $('#disconnect-button').disabled = false; }
}
async function readImage(file) {
  if (!file || !['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) throw new Error('请选择 PNG、JPEG 或 WebP 图片。');
  if (file.size > imageLimit) throw new Error('单张图片不能超过 5 MB。');
  return await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = () => reject(new Error('无法读取这张图片，请重新选择。')); reader.readAsDataURL(file); });
}
function imageValue(value, label) {
  if (/^data:image\/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$/.test(value)) return value;
  try { const url = new URL(value); if (url.protocol === 'https:' && !url.username && !url.password) return url.href; } catch { /* Explain the expected input below. */ }
  throw new Error(`${label}需使用本机图片或 HTTPS 图片文件直链。`);
}
function referenceValue(item) {
  const value = item.fileData || item.url.value.trim();
  if (item.type.value === 'image_url') return { type: 'image_url', url: imageValue(value, '参考图片') };
  try { const url = new URL(value); if (url.protocol === 'https:' && !url.username && !url.password && /\.mp4$/i.test(url.pathname) && !/(^|\.)(tiktok\.com|douyin\.com)$/.test(url.hostname)) return { type: 'video_url', url: url.href }; } catch { /* Explain the expected input below. */ }
  throw new Error('参考视频需为 HTTPS 的 MP4 文件直链，不能使用 TikTok / 抖音分享链接。');
}
function addReference() {
  if (references.length >= 4) return;
  const row = node('div', 'reference-row'), top = node('div', 'reference-row-top'), type = node('select'), url = node('input'), status = node('span', 'file-status');
  type.setAttribute('aria-label', '参考素材类型');
  for (const [value, label] of [['image_url', '角色 / 造型图片'], ['video_url', '动作参考视频（MP4）']]) { const option = node('option', '', label); option.value = value; type.append(option); }
  const remove = node('button', 'text-button', '移除'); remove.type = 'button'; top.append(type, remove);
  url.type = 'url'; url.placeholder = 'HTTPS 图片文件直链，或选择本机图片'; url.setAttribute('aria-label', '参考素材文件直链');
  const fileLabel = node('label', 'file-label', '选择参考图片'), fileInput = node('input'); fileInput.type = 'file'; fileInput.accept = 'image/png,image/jpeg,image/webp'; fileLabel.append(fileInput);
  const item = { row, type, url, fileInput, fileData: '' }; references.push(item); row.append(top, url, fileLabel, status); $('#reference-list').append(row);
  remove.addEventListener('click', () => { references.splice(references.indexOf(item), 1); row.remove(); $('#add-reference').disabled = references.length >= 4; });
  type.addEventListener('change', () => { item.fileData = ''; fileInput.value = ''; status.textContent = ''; fileLabel.hidden = type.value !== 'image_url'; url.placeholder = type.value === 'image_url' ? 'HTTPS 图片文件直链，或选择本机图片' : 'https://你的文件存储/动作参考.mp4'; });
  url.addEventListener('input', () => { item.fileData = ''; fileInput.value = ''; status.textContent = ''; });
  fileInput.addEventListener('change', async () => { try { item.fileData = await readImage(fileInput.files[0]); url.value = ''; status.textContent = `已选：${fileInput.files[0].name}`; message('#generation-message'); } catch (error) { item.fileData = ''; fileInput.value = ''; status.textContent = ''; message('#generation-message', error.message, 'error'); } });
  $('#add-reference').disabled = references.length >= 4;
}
function generationPayload() {
  const model = currentModel(), caps = capabilities(model), mode = $('#mode').value;
  if (!connected || !model) throw new Error('请先连接并选择模型。');
  if (!caps.durations.includes(Number($('#duration').value)) || !caps.resolutions.includes($('#resolution').value) || !caps.aspects.includes($('#aspect-ratio').value)) throw new Error('请选择模型支持的时长、分辨率和画幅。');
  const prompt = $('#prompt').value.trim(); if (!prompt) throw new Error('请填写画面和动作描述。');
  if (!$('#consent').checked) throw new Error('请确认素材传输和额度使用。');
  const payload = { model: model.id, prompt, mode, duration: Number($('#duration').value), resolution: $('#resolution').value, aspect_ratio: $('#aspect-ratio').value, generate_audio: caps.audio && $('#generate-audio').checked, consent: true };
  if (mode === 'frames') {
    if (!caps.first) throw new Error('所选模型未声明支持首帧。');
    payload.first_frame = imageValue(files.get('first') || $('#first-frame-url').value.trim(), '首帧');
    const last = files.get('last') || $('#last-frame-url').value.trim(); if (last && caps.last) payload.last_frame = imageValue(last, '尾帧');
  } else if (mode === 'references') {
    if (!caps.refs || !references.length) throw new Error('请选择支持参考素材的模型，并添加至少一份参考。');
    payload.references = references.map(referenceValue);
  }
  return payload;
}
async function fingerprint(payload) { const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify(payload))); return Array.from(new Uint8Array(bytes), value => value.toString(16).padStart(2, '0')).join(''); }
async function generate(event) {
  event.preventDefault(); if (submitting) return;
  let payload, hash;
  try { payload = generationPayload(); hash = await fingerprint(payload); } catch (error) { message('#generation-message', error.message, 'error'); return; }
  if (submissionRecord?.fingerprint === hash && submissionRecord.outcome === 'uncertain') { message('#generation-message', '上一次相同请求的提交结果还不明确，已阻止重复提交。请先刷新本机记录，并到 OpenRouter 核对任务或额度记录。', 'error'); await loadJobs(false); return; }
  if (submissionRecord?.fingerprint === hash && submissionRecord.outcome === 'accepted') { message('#generation-message', '这个方案已经提交，已保留原任务，没有再次扣费。需要另一个生成结果时，请点击「同一方案再生成一条」。', 'success'); $('#new-generation-button').hidden = false; await loadJobs(false); return; }
  const key = submissionRecord?.fingerprint === hash && submissionRecord.outcome === 'retryable' ? submissionRecord.key : crypto.randomUUID();
  persistSubmission({ key, fingerprint: hash, outcome: 'uncertain' });
  submitting = true; updateGenerateButton(); $('#generate-button').textContent = '正在提交，请勿重复点击…'; message('#generation-message', '正在把本次创作提交给 OpenRouter…');
  try {
    const job = await api('/jobs', 'POST', { ...payload, idempotency_key: key });
    persistSubmission({ key, fingerprint: hash, outcome: job.status === 'submission_uncertain' ? 'uncertain' : 'accepted', job_id: job.id });
    message('#generation-message', job.status === 'submission_uncertain' ? '提交结果待核对，未自动重试。请查看下方任务，并到 OpenRouter 核对。' : '任务已保存。生成进度会显示在下方记录中。', job.status === 'submission_uncertain' ? 'error' : 'success');
    $('#new-generation-button').hidden = job.status === 'submission_uncertain';
    await loadJobs(false);
  } catch (error) {
    persistSubmission({ key, fingerprint: hash, outcome: error.transport || error.status >= 500 ? 'uncertain' : 'retryable' });
    message('#generation-message', `${error.message}${error.transport || error.status >= 500 ? '\n提交是否成功尚不明确，已阻止相同请求重复提交。请先核对本机记录与 OpenRouter。' : ''}`, 'error');
    await loadJobs(false);
  } finally { submitting = false; $('#generate-button').textContent = '开始生成（消耗 OpenRouter 额度）'; updateGenerateButton(); }
}
function localVideo(value) {
  if (typeof value !== 'string') return '';
  try { const url = new URL(value, location.origin); return url.origin === location.origin && /^\/generated\/[A-Za-z0-9_.-]+\.mp4$/.test(url.pathname) ? url.pathname : ''; } catch { return ''; }
}
function date(value) { const instant = new Date(value); return Number.isFinite(instant.getTime()) ? new Intl.DateTimeFormat('zh-CN', { timeZone: 'America/New_York', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(instant) : '时间待确认'; }
function renderJobs() {
  const list = $('#jobs-list');
  if (!jobs.length) { list.replaceChildren(node('p', 'empty-state', '还没有生成任务。连接后，选择模型和素材，开始第一条创作。')); return; }
  list.replaceChildren(...jobs.map(job => {
    const card = node('article', 'job-card'), top = node('div', 'job-card-top');
    const status = Object.hasOwn(statusLabels, job.status) ? job.status : 'unknown';
    top.append(node('span', `status-pill status-${status}`, statusLabels[status] || '状态待确认'), node('span', 'job-time', `${date(job.created_at)} · 纽约`)); card.append(top, node('h3', '', job.model || 'Seedance'), node('p', 'job-prompt', job.prompt || ''));
    const info = [job.duration ? `${job.duration} 秒` : '', job.resolution || '', job.aspect_ratio || '', typeof job.cost === 'number' && Number.isFinite(job.cost) ? `费用 $${job.cost.toFixed(4)}` : ''].filter(Boolean); if (info.length) card.append(node('p', 'job-meta', info.join(' · ')));
    if (job.error) card.append(node('p', 'job-error', typeof job.error === 'string' ? job.error : '任务未完成，请检查接口配置或素材。'));
    const local = localVideo(job.result_url), actions = node('div', 'job-actions');
    if (local) { const video = node('video'); video.src = local; video.controls = true; video.preload = 'metadata'; video.setAttribute('aria-label', '生成结果预览'); card.append(video); const download = node('a', 'button small', '下载 MP4'); download.href = local; download.download = `idore-${job.id}.mp4`; actions.append(download); }
    else if (job.status === 'completed') { const button = node('button', 'button primary small', downloading.has(job.id) ? '正在保存…' : '保存到本机并预览'); button.type = 'button'; button.disabled = !connected || downloading.has(job.id); button.addEventListener('click', () => downloadJob(job.id)); actions.append(button); }
    if (['submitting', 'pending', 'queued', 'in_progress'].includes(job.status)) { const button = node('button', 'button small', refreshing.has(job.id) ? '正在查询…' : '查询进度'); button.type = 'button'; button.disabled = !connected || refreshing.has(job.id); button.addEventListener('click', () => refreshJob(job.id)); actions.append(button); }
    if (job.status === 'submission_uncertain') { const note = node('p', 'job-uncertain', '提交结果不明确，请先核对 OpenRouter，避免重复付费。'); const link = node('a', '', '打开 OpenRouter 记录 ↗'); link.href = 'https://openrouter.ai/activity'; link.target = '_blank'; link.rel = 'noopener noreferrer'; note.append(' ', link); card.append(note); }
    if (actions.children.length) card.append(actions); return card;
  }));
}
async function refreshJob(id) {
  if (refreshing.has(id) || !connected) return;
  const last = lastRemoteRefresh.get(id) || 0; if (Date.now() - last < 30000) { message('#jobs-message', '刚刚查询过这个任务，30 秒后会再次查询。'); return; }
  refreshing.add(id); lastRemoteRefresh.set(id, Date.now()); renderJobs();
  try { await api(`/jobs/${encodeURIComponent(id)}/refresh`, 'POST', {}); message('#jobs-message'); }
  catch (error) { message('#jobs-message', error.message, 'error'); }
  finally { refreshing.delete(id); jobSignature = ''; await loadJobs(false); }
}
async function downloadJob(id) {
  if (downloading.has(id)) return; downloading.add(id); renderJobs();
  try { await api(`/jobs/${encodeURIComponent(id)}/download`, 'POST', {}); message('#jobs-message', '视频已保存到本机，可以预览或下载。', 'success'); }
  catch (error) { message('#jobs-message', error.message, 'error'); }
  finally { downloading.delete(id); jobSignature = ''; await loadJobs(false); }
}
async function loadJobs(remote = true) {
  try {
    const result = await api('/jobs'); const next = (Array.isArray(result.jobs) ? result.jobs : []).filter(job => job && typeof job.id === 'string').sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
    jobs = next;
    if (submissionRecord?.outcome === 'uncertain') { const recovered = jobs.find(job => job.idempotency_key === submissionRecord.key || job.id === submissionRecord.job_id); if (recovered && recovered.status !== 'submission_uncertain') persistSubmission({ ...submissionRecord, outcome: 'accepted', job_id: recovered.id }); }
    const signature = JSON.stringify(jobs) + String(connected); if (signature !== jobSignature) { jobSignature = signature; renderJobs(); }
    if (remote && connected) {
      const now = Date.now();
      for (const job of jobs) if (!lastRemoteRefresh.has(job.id)) lastRemoteRefresh.set(job.id, now);
      const due = jobs.filter(job => ['pending', 'queued', 'in_progress'].includes(job.status) && !refreshing.has(job.id) && now - lastRemoteRefresh.get(job.id) >= 30000).slice(0, 3);
      await Promise.all(due.map(job => refreshJob(job.id)));
    }
  } catch (error) { message('#jobs-message', error.message, 'error'); }
}
async function poll() { clearTimeout(pollTimer); await loadJobs(); pollTimer = setTimeout(poll, 5000); }

$('#connection-form').addEventListener('submit', connect); $('#disconnect-button').addEventListener('click', disconnect);
$('#generation-form').addEventListener('submit', generate); $('#model').addEventListener('change', configureModel); $('#mode').addEventListener('change', configureMode); $('#add-reference').addEventListener('click', addReference);
$('#generation-form').addEventListener('input', () => { $('#new-generation-button').hidden = true; });
$('#new-generation-button').addEventListener('click', () => { if (submitting || !$('#generation-form').reportValidity()) return; submissionRecord = null; try { localStorage.removeItem(submissionStorage); } catch { /* In-memory request state is also cleared. */ } $('#new-generation-button').hidden = true; $('#generation-form').requestSubmit(); });
$('#reload-jobs').addEventListener('click', () => loadJobs(false));
for (const frame of ['first', 'last']) {
  $(`#${frame}-frame-file`).addEventListener('change', async () => { const input = $(`#${frame}-frame-file`); try { const file = input.files[0]; files.set(frame, await readImage(file)); $(`#${frame}-frame-url`).value = ''; $(`#${frame}-frame-status`).textContent = `已选：${file.name}`; message('#generation-message'); } catch (error) { files.delete(frame); input.value = ''; $(`#${frame}-frame-status`).textContent = ''; message('#generation-message', error.message, 'error'); } });
  $(`#${frame}-frame-url`).addEventListener('input', () => { files.delete(frame); $(`#${frame}-frame-file`).value = ''; $(`#${frame}-frame-status`).textContent = ''; });
  $(`#clear-${frame}-frame`).addEventListener('click', () => { files.delete(frame); $(`#${frame}-frame-url`).value = ''; $(`#${frame}-frame-file`).value = ''; $(`#${frame}-frame-status`).textContent = ''; });
}
loadConnection().catch(error => { message('#connection-message', error.message, 'error'); paintConnection(); });
poll();
