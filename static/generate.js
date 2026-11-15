'use strict';

const $ = selector => document.querySelector(selector);
const apiRoot = '/api/video';
const imageLimit = 5 * 1024 * 1024;
const submissionStorage = 'idore-video-submission-v1';
const statusLabels = { submitting: '正在提交', pending: '等待生成', queued: '等待生成', in_progress: '正在生成', completed: '生成完成', failed: '生成失败', cancelled: '生成已取消', expired: '任务已过期', submission_uncertain: '提交结果待核对' };
let connected = false, models = [], jobs = [], submitting = false, preparingSubmission = false, connecting = false, jobSignature = '', pollTimer, lastConnectionCheck = Date.now();
let submissionRecord = null;
const files = new Map(), references = [], refreshing = new Set(), downloading = new Set(), sampling = new Set(), lastRemoteRefresh = new Map();
let openAIConnected = false, openAIConnecting = false, creativeModels = { image_models: [], text_models: [] }, creativeJobs = [], creativeSignature = '', styleReference = '', requiredStylePair = false;
const creativeBusy = { images: false, draft: false }, creativeRecords = {}, draftFrames = [];
const materialLabels = { male: '男生角色', female: '女生角色', mixed: '男女混合', unknown: '分组待确认' }, subjectLabels = { solo: '单人', group: '多人', unknown: '人数待确认' };
let replicationAssets = [], replicationReferences = [], selectedAssetId = '', selectedReferenceId = '', preparedReplication = null, replicationPreparing = false, replicationSubmitting = false, replicationRecord = null, requestedChoicesApplied = false, replicationSelectionVersion = 0, assetClassificationDirty = false, referenceClassificationDirty = false;
try { const saved = JSON.parse(localStorage.getItem(submissionStorage) || 'null'); if (saved && typeof saved.key === 'string' && typeof saved.fingerprint === 'string') submissionRecord = saved; } catch { /* The page also works when browser storage is unavailable. */ }
try { const saved = JSON.parse(localStorage.getItem('idore-replication-submission-v1') || 'null'); if (saved && typeof saved.key === 'string' && typeof saved.fingerprint === 'string') replicationRecord = saved; } catch { /* Keep paired request state in memory for this visit. */ }

function node(tag, className, content) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (content !== undefined) element.textContent = String(content);
  return element;
}
function message(selector, text = '', state = '') { const element = $(selector); element.textContent = text; element.className = `form-message${state ? ' ' + state : ''}`; }
async function api(path, method = 'GET', body, root = apiRoot) {
  let response;
  try {
    response = await fetch(root + path, { method, headers: body === undefined ? { Accept: 'application/json' } : { Accept: 'application/json', 'Content-Type': 'application/json' }, credentials: 'same-origin', cache: 'no-store', ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
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
function stylePairReady(caps = capabilities(currentModel())) {
  return $('#mode').value === 'frames' && caps.first && caps.last && Boolean(files.get('first') || $('#first-frame-url').value.trim()) && Boolean(files.get('last') || $('#last-frame-url').value.trim());
}
function updateGenerateButton() { const caps = capabilities(currentModel()); $('#generate-button').disabled = !connected || submitting || preparingSubmission || !currentModel() || !caps.durations.length || !caps.resolutions.length || !caps.aspects.length || requiredStylePair && !stylePairReady(caps); updateReplicationButton(); }
function configureModel() {
  const model = currentModel(), caps = capabilities(model);
  fillOptions($('#duration'), caps.durations, $('#duration').value, undefined, value => `${value} 秒`);
  fillOptions($('#resolution'), caps.resolutions, $('#resolution').value, '720p');
  fillOptions($('#aspect-ratio'), caps.aspects, $('#aspect-ratio').value, '9:16');
  for (const option of $('#mode').options) option.disabled = option.value === 'frames' ? !caps.first : option.value === 'references' ? !caps.refs : false;
  if (requiredStylePair && caps.first && caps.last) $('#mode').value = 'frames';
  if ($('#mode').selectedOptions[0]?.disabled) $('#mode').value = 'text';
  $('#generate-audio').disabled = !caps.audio;
  if (!caps.audio) $('#generate-audio').checked = false;
  $('#audio-note').textContent = caps.audio ? '所选模型支持声音生成' : '所选模型未声明支持声音生成';
  $('#model-note').textContent = model ? `模型 ID：${model.id}。${!caps.durations.length || !caps.resolutions.length || !caps.aspects.length ? '未取得完整支持范围，暂不能提交。' : '参数来自 OpenRouter 当前返回的模型能力。'}` : '请选择模型；不会自动替换你选择的版本。';
  configureMode(); updateGenerateButton();
  if (requiredStylePair) message('#generation-message', stylePairReady(caps) ? '前后造型已准备，所选模型支持首尾帧。检查描述后再生成视频。' : '已准备前后两张造型图，请选择同时支持首帧和尾帧的模型。若要改为文字或参考生成，请明确切换生成方式；也可清除一张造型图解除双图联动。', stylePairReady(caps) ? 'success' : 'error');
}
function configureMode() {
  const mode = $('#mode').value, caps = capabilities(currentModel());
  $('#frames-fields').hidden = mode !== 'frames'; $('#references-fields').hidden = mode !== 'references'; $('#last-frame-field').hidden = !caps.last;
  $('#mode-note').textContent = mode === 'frames' ? (caps.last ? '首帧确定起始造型；尾帧可确定结束造型。' : '所选模型支持首帧；未声明尾帧支持。') : mode === 'references' ? '用角色图片控制形象，用视频参考动作。请在描述中说明各份素材的用途。' : '输入画面、动作与镜头描述，不附加图片或视频。';
  if (mode === 'references' && !references.length) addReference();
}
function changeGenerationMode() {
  const mode = $('#mode').value;
  if (mode !== 'frames' && (requiredStylePair || files.has('first') || files.has('last') || $('#first-frame-url').value.trim() || $('#last-frame-url').value.trim())) {
    requiredStylePair = false;
    message('#generation-message', `已明确切换为${mode === 'text' ? '文字生成' : '参考素材生成'}；准备的首尾帧仍保留在本机，本次不会发送。`, 'success');
    $('#style-handoff-note').hidden = false; $('#style-handoff-note').textContent = '本次已切换生成方式，首尾帧未用于本次生成。需要双图变装时，请重新选择首尾帧方式与兼容模型。';
  }
  configureMode(); updateGenerateButton();
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
  for (const model of models) { const option = node('option', '', (model.name || model.id) + (replicationModelSupported(model) ? '' : ' · 不用于角色与视频共同跟拍')); option.value = model.id; $('#model').append(option); }
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
  finally { connecting = false; $('#connect-button').disabled = false; $('#connect-button').textContent = '验证并连接 OpenRouter'; }
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
  try {
    const url = new URL(value), host = url.hostname.toLowerCase().replace(/\.$/, '');
    const officialPlayerFile = /^v[0-9]+(?:-[a-z0-9]+)*\.(?:[a-z]{2}\.)?tiktok\.com$/.test(host) && /^\/video\/tos\/[A-Za-z0-9/_-]+\/?$/.test(decodeURIComponent(url.pathname)) && url.searchParams.getAll('mime_type').length === 1 && url.searchParams.get('mime_type') === 'video_mp4';
    if (url.protocol === 'https:' && !url.username && !url.password && !url.hash && (!url.port || url.port === '443') && (officialPlayerFile || /\.(mp4|webm|mov)$/i.test(url.pathname) && !/(^|\.)(tiktok\.com|douyin\.com|iesdouyin\.com)$/.test(host))) return { type: 'video_url', url: url.href };
  } catch { /* Explain the expected input below. */ }
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
  if (requiredStylePair && !stylePairReady(caps)) throw new Error('前后造型图尚未能一起送入模型。请选择支持首尾帧的模型并保留两张图；改用其他方式需明确切换生成方式或清除一张造型图。');
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
  event.preventDefault(); if (submitting || preparingSubmission) return;
  preparingSubmission = true; updateGenerateButton();
  let payload, hash;
  try { payload = generationPayload(); hash = await fingerprint(payload); } catch (error) { preparingSubmission = false; updateGenerateButton(); message('#generation-message', error.message, 'error'); return; }
  if (submissionRecord?.fingerprint === hash && submissionRecord.outcome === 'uncertain') { preparingSubmission = false; updateGenerateButton(); message('#generation-message', '上一次相同请求的提交结果还不明确，已阻止重复提交。请先刷新本机记录，并到 OpenRouter 核对任务或额度记录。', 'error'); await loadJobs(false); return; }
  if (submissionRecord?.fingerprint === hash && submissionRecord.outcome === 'accepted') { preparingSubmission = false; updateGenerateButton(); message('#generation-message', '这个方案已经提交，已保留原任务，没有再次扣费。需要另一个生成结果时，请点击「同一方案再生成一条」。', 'success'); $('#new-generation-button').hidden = false; await loadJobs(false); return; }
  const key = submissionRecord?.fingerprint === hash && submissionRecord.outcome === 'retryable' ? submissionRecord.key : crypto.randomUUID();
  persistSubmission({ key, fingerprint: hash, outcome: 'uncertain' });
  submitting = true; preparingSubmission = false; updateGenerateButton(); $('#generate-button').textContent = '正在提交，请勿重复点击…'; message('#generation-message', '正在把本次创作提交给 OpenRouter…');
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
    if (job.source_asset_id && job.source_reference_id) { const asset = replicationAssets.find(item => item.id === job.source_asset_id), reference = replicationReferences.find(item => item.id === job.source_reference_id); card.append(node('p', 'job-meta paired-job-meta', `角色：${asset?.character || asset?.name || job.source_asset_id} · 动作参考：${reference?.title || job.source_reference_id}`)); }
    if (job.error) card.append(node('p', 'job-error', typeof job.error === 'string' ? job.error : '任务未完成，请检查接口配置或素材。'));
    const local = localVideo(job.result_url), actions = node('div', 'job-actions');
    if (local) { const video = node('video'); video.src = local; video.controls = true; video.preload = 'metadata'; video.setAttribute('aria-label', '生成结果预览'); card.append(video); const download = node('a', 'button small', '下载 MP4'); download.href = local; download.download = `idore-${job.id}.mp4`; actions.append(download); const draftButton = node('button', 'button small', sampling.has(job.id) ? '正在选取成片关键帧…' : '用这条成片生成文案'); draftButton.type = 'button'; draftButton.disabled = sampling.has(job.id); draftButton.addEventListener('click', () => useVideoForDraft(job)); actions.append(draftButton); }
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
    if (replicationRecord?.outcome === 'uncertain') { const recovered = jobs.find(job => job.idempotency_key === replicationRecord.key || job.id === replicationRecord.job_id); if (recovered && recovered.status !== 'submission_uncertain') persistReplication({ ...replicationRecord, outcome: 'accepted', job_id: recovered.id }); }
    const signature = JSON.stringify(jobs) + String(connected); if (signature !== jobSignature) { jobSignature = signature; renderJobs(); }
    if (remote && connected) {
      const now = Date.now();
      for (const job of jobs) if (!lastRemoteRefresh.has(job.id)) lastRemoteRefresh.set(job.id, now);
      const due = jobs.filter(job => ['pending', 'queued', 'in_progress'].includes(job.status) && !refreshing.has(job.id) && now - lastRemoteRefresh.get(job.id) >= 30000).slice(0, 3);
      await Promise.all(due.map(job => refreshJob(job.id)));
    }
  } catch (error) { message('#jobs-message', error.message, 'error'); }
}
async function refreshConnectionStates() {
  if (Date.now() - lastConnectionCheck < 30000) return; lastConnectionCheck = Date.now();
  await Promise.all([
    api('/connection').then(result => { if (connected !== (result.connected === true)) { connected = result.connected === true; paintConnection(); } }).catch(() => {}),
    creativeApi('/connection').then(result => { if (openAIConnected !== (result.connected === true)) { openAIConnected = result.connected === true; paintOpenAIConnection(); } }).catch(() => {})
  ]);
}
async function poll() { clearTimeout(pollTimer); await Promise.all([loadJobs(), loadCreativeJobs(), refreshConnectionStates()]); updateReplicationButton(); pollTimer = setTimeout(poll, 5000); }

const replicationApi = (path, method = 'GET', body) => api(path, method, body, '/api/replication');
function selectedAsset() { return replicationAssets.find(asset => asset.id === selectedAssetId); }
function selectedReference() { return replicationReferences.find(reference => reference.id === selectedReferenceId); }
function materialLabel(value) { return Object.hasOwn(materialLabels, value) ? materialLabels[value] : materialLabels.unknown; }
function subjectLabel(value) { return Object.hasOwn(subjectLabels, value) ? subjectLabels[value] : subjectLabels.unknown; }
function localAssetImage(value) {
  if (typeof value !== 'string') return '';
  try { const url = new URL(value, location.origin); return url.origin === location.origin && /^\/media\/asset-[a-f0-9]{16}\.(png|jpe?g|webp|gif)$/.test(url.pathname) ? url.pathname : ''; } catch { return ''; }
}
function replicationModelSupported(model) { const generation = String(model?.id || '').match(/^bytedance\/seedance-(\d+)(?:\.|-|$)/); return Boolean(generation && Number(generation[1]) >= 2 && capabilities(model).refs); }
function matchingBlockers(reference, asset = selectedAsset()) {
  const blockers = [];
  if (!asset) blockers.push('先选择我们的角色照片。');
  else {
    blockers.push(...(Array.isArray(asset.blockers) ? asset.blockers.filter(value => typeof value === 'string') : []));
    if (!['male', 'female'].includes(asset.material_group)) blockers.push('请先标记所选角色为男生或女生。');
    if (asset.subject_group !== 'solo') blockers.push('请先确认所选角色图片为单人。');
    if (asset.ready === false && !blockers.length) blockers.push('所选角色图片尚未准备好。');
  }
  blockers.push(...(Array.isArray(reference?.blockers) ? reference.blockers.filter(value => typeof value === 'string') : []));
  if (!reference || reference.original_watched !== true) blockers.push('参考视频尚未确认已观看原片。');
  if (!['male', 'female'].includes(reference?.material_group)) blockers.push('参考视频的男生／女生分组待确认。');
  if (reference?.subject_group !== 'solo') blockers.push('本流程需要已确认单人的参考。');
  if (asset && ['male', 'female'].includes(asset.material_group) && ['male', 'female'].includes(reference?.material_group) && asset.material_group !== reference.material_group) blockers.push('与所选角色的男生／女生分组不一致。');
  if (reference?.ready === false && !blockers.length) blockers.push('参考视频尚未准备好。');
  return [...new Set(blockers)];
}
function invalidateReplication(reason = '') {
  replicationSelectionVersion++;
  preparedReplication = null; $('#replication-consent').checked = false; $('#new-replication-button').hidden = true;
  $('#replication-pair-review').replaceChildren(node('p', 'empty-inline', reason || '选择角色与参考，准备后在这里确认两份输入。'));
  message('#replication-message'); updateReplicationButton();
}
function replicationPlanProblem() {
  const plan = preparedReplication;
  if (assetClassificationDirty) return '角色标记有未保存的更改，请先保存，再重新准备。';
  if (referenceClassificationDirty) return '参考标记有未保存的更改，请先保存，再重新准备。';
  if (!plan || plan.can_submit !== true || typeof plan.plan_id !== 'string') return '请先准备并确认角色图片与动作参考。';
  if (plan.asset?.id !== selectedAssetId || plan.reference?.id !== selectedReferenceId) return '素材选择已经变化，请重新准备。';
  if (!Array.isArray(plan.required_inputs) || !['character_image', 'reference_video'].every(input => plan.required_inputs.includes(input)) || plan.has_reference_video !== true) return '角色图片或动作视频尚未共同准备好，请重新准备。';
  if (plan.expires_at && (!Number.isFinite(new Date(plan.expires_at).getTime()) || new Date(plan.expires_at).getTime() <= Date.now())) return '准备方案已过期，请重新准备这组素材。';
  const blockers = matchingBlockers(selectedReference()); if (blockers.length) return blockers.join(' ');
  if (!connected) return '请连接 OpenRouter，再生成这组跟拍。';
  const model = currentModel(), caps = capabilities(model);
  if (!model || !replicationModelSupported(model)) return '请选择支持角色图片与动作视频参考的 Seedance 2 或更新版本。';
  if (!caps.durations.includes(Number($('#duration').value)) || !caps.resolutions.includes($('#resolution').value) || !caps.aspects.includes($('#aspect-ratio').value)) return '请选择模型支持的时长、分辨率与画幅。';
  return '';
}
function updateReplicationButton() {
  const problem = replicationPlanProblem(); $('#replication-button').disabled = replicationSubmitting || Boolean(problem);
  $('#replication-prepare-button').disabled = replicationPreparing || assetClassificationDirty || referenceClassificationDirty || !selectedAsset() || !selectedReference() || matchingBlockers(selectedReference()).length > 0;
  $('#replication-model-note').textContent = problem || `已准备：${preparedReplication?.asset?.character || preparedReplication?.asset?.name || '所选角色'}的图片 + 所选参考的动作视频。${currentModel()?.name || currentModel()?.id} 会同时使用这两份输入，不生成音轨。`;
}
function populateReplicationFilters() {
  for (const [key, selector, label] of [['character', '#replication-character', '全部角色'], ['stage', '#replication-stage', '全部阶段']]) {
    const previous = $(selector).value, option = node('option', '', label); option.value = ''; $(selector).replaceChildren(option);
    const values = [...new Set(replicationAssets.map(asset => asset[key]).filter(value => typeof value === 'string' && value))];
    for (const value of values) { const item = node('option', '', value); item.value = value; $(selector).append(item); }
    $(selector).value = values.includes(previous) ? previous : '';
  }
}
function updateAssetEditor() {
  assetClassificationDirty = false;
  const asset = selectedAsset(), form = $('#replication-classification-form'); form.hidden = !asset;
  if (!asset) return;
  $('#replication-classification-heading').textContent = `确认：${asset.character || asset.name} · ${asset.name}`;
  $('#replication-material').value = Object.hasOwn(materialLabels, asset.material_group) ? asset.material_group : 'unknown';
  $('#replication-subject').value = Object.hasOwn(subjectLabels, asset.subject_group) ? asset.subject_group : 'unknown';
  $('#replication-apply-character').checked = false; $('#replication-apply-character').disabled = !asset.character;
}
function updateReferenceEditor() {
  referenceClassificationDirty = false; const reference = selectedReference(); $('#replication-reference-classification-form').hidden = !reference;
  if (!reference) return;
  $('#replication-reference-classification-heading').textContent = `人工复核：${reference.title || reference.id}`;
  $('#replication-reference-material').value = Object.hasOwn(materialLabels, reference.material_group) ? reference.material_group : 'unknown';
  $('#replication-reference-subject').value = Object.hasOwn(subjectLabels, reference.subject_group) ? reference.subject_group : 'unknown';
}
function renderReplicationAssets() {
  const list = replicationAssets.filter(asset => (!$('#replication-character').value || asset.character === $('#replication-character').value) && (!$('#replication-stage').value || asset.stage === $('#replication-stage').value));
  $('#replication-asset-count').textContent = `${list.length} 张`;
  $('#replication-assets').replaceChildren(...(list.length ? list.map(asset => {
    const button = node('button', `replication-asset-card${asset.id === selectedAssetId ? ' selected' : ''}`), path = localAssetImage(asset.url); button.type = 'button'; button.setAttribute('aria-pressed', String(asset.id === selectedAssetId)); button.setAttribute('aria-label', `选择角色照片 ${asset.character || ''} ${asset.name}`);
    if (path) { const image = node('img'); image.src = path; image.alt = asset.name || asset.character || '素材库角色图片'; image.loading = 'lazy'; button.append(image); } else button.append(node('span', 'asset-missing', '图片不可预览'));
    button.append(node('strong', '', asset.character || asset.name), node('small', '', asset.name), node('span', 'replication-card-tags', `${materialLabel(asset.material_group)} · ${subjectLabel(asset.subject_group)}${asset.stage ? ' · ' + asset.stage : ''}`));
    button.addEventListener('click', () => { if (selectedAssetId === asset.id) return; selectedAssetId = asset.id; invalidateReplication('角色图片已变化，请检查匹配参考并重新准备。'); updateAssetEditor(); renderReplicationAssets(); renderReplicationReferences(); if (!$('#draft-character').value.trim() && asset.character) $('#draft-character').value = asset.character; }); return button;
  }) : [node('p', 'empty-inline', replicationAssets.length ? '没有符合筛选的角色照片。' : '素材库中暂无已批准的自有角色图片。请先到「我的素材」导入。')]));
}
function referenceCard(reference, blockers = []) {
  const card = node('article', `replication-reference-card${reference.id === selectedReferenceId ? ' selected' : ''}${blockers.length ? ' blocked' : ''}`);
  card.append(node('h4', '', reference.title || 'TikTok 原片参考'), node('p', 'reference-meta', `${materialLabel(reference.material_group)} · ${subjectLabel(reference.subject_group)} · ${reference.capture_date || date(reference.first_seen_at)} 抓到`));
  if (typeof reference.motion_summary === 'string' && reference.motion_summary) card.append(node('p', 'reference-motion', reference.motion_summary));
  if (blockers.length) card.append(node('p', 'reference-blockers', blockers.join(' ')));
  const actions = node('div', 'job-actions'), choose = node('button', 'button small', reference.id === selectedReferenceId ? '已选择这条参考' : blockers.length ? '选择并复核分类' : '选择这条参考'); choose.type = 'button'; choose.addEventListener('click', () => { if (selectedReferenceId === reference.id) return; selectedReferenceId = reference.id; invalidateReplication('参考视频已变化，请核对动作文件并重新准备。'); $('#replication-video-confirmed').checked = false; updateReferenceEditor(); renderReplicationReferences(); }); actions.append(choose);
  const url = safeSource(reference.url); if (url) { const source = node('a', 'text-button', '查看参考原片 ↗'); source.href = url; source.target = '_blank'; source.rel = 'noopener noreferrer'; actions.append(source); }
  card.append(actions); return card;
}
function renderReplicationReferences() {
  const sorted = [...replicationReferences].sort((a, b) => (new Date(b.first_seen_at || b.last_seen_at).getTime() || 0) - (new Date(a.first_seen_at || a.last_seen_at).getTime() || 0));
  const matching = [], blocked = [];
  for (const reference of sorted) { const blockers = matchingBlockers(reference); (blockers.length ? blocked : matching).push({ reference, blockers }); }
  $('#replication-reference-count').textContent = `${matching.length} 条可匹配`;
  const asset = selectedAsset(); $('#replication-match-note').textContent = !asset ? '先选择我们的角色照片。参考列表会按抓取日期由新到旧显示。' : asset.ready !== true ? '先在所选照片下方确认男女与人数标记，或处理图片提示，再匹配参考。' : `正在为 ${asset.character || asset.name} 匹配${materialLabel(asset.material_group)}的单人参考。未确认分类和不匹配的记录保留在下方。`;
  $('#replication-references').replaceChildren(...(matching.length ? matching.map(item => referenceCard(item.reference)) : [node('p', 'empty-inline', !asset ? '选择一张角色照片，开始匹配。' : '暂无已确认同分组的单人参考。可同步最新参考，或先完成素材分类复核。')]));
  $('#replication-blocked-summary').textContent = `待分类或不匹配的参考 · ${blocked.length} 条`;
  $('#replication-blocked').hidden = !blocked.length; $('#replication-blocked-list').replaceChildren(...blocked.map(item => referenceCard(item.reference, item.blockers)));
  if (blocked.some(item => item.reference.id === selectedReferenceId)) $('#replication-blocked').open = true;
  updateReplicationButton();
}
async function loadReplicationCatalog(invalidate = false) {
  try {
    const result = await replicationApi('/catalog'); if (invalidate) invalidateReplication('目录已更新，请重新准备并确认素材。');
    replicationAssets = (Array.isArray(result.assets) ? result.assets : []).filter(asset => asset && /^asset-[a-f0-9]{16}$/.test(asset.id) && asset.is_demo !== true);
    replicationReferences = (Array.isArray(result.references) ? result.references : []).filter(reference => reference && /^tiktok-\d{10,25}$/.test(reference.id));
    let requestedNote = '';
    if (!requestedChoicesApplied) {
      try {
        const params = new URLSearchParams(location.search || ''), requestedAsset = params.get('asset_id'), requestedReference = params.get('reference');
        if (requestedAsset && replicationAssets.some(asset => asset.id === requestedAsset)) selectedAssetId = requestedAsset;
        if (requestedReference && replicationReferences.some(reference => reference.id === requestedReference)) { selectedReferenceId = requestedReference; requestedNote = '已带入你选择的参考原片。请继续选择我们的角色；不满足分类条件时会显示具体原因。'; }
        else if (requestedReference) requestedNote = '这条参考尚未同步到本机。点击「同步最新参考」后再匹配。';
        requestedChoicesApplied = !requestedReference || Boolean(selectedReferenceId);
      } catch { requestedChoicesApplied = true; }
    }
    if (selectedAssetId && !selectedAsset()) selectedAssetId = ''; if (selectedReferenceId && !selectedReference()) selectedReferenceId = '';
    populateReplicationFilters(); updateAssetEditor(); updateReferenceEditor(); renderReplicationAssets(); renderReplicationReferences();
    const blockers = Array.isArray(result.blockers) ? result.blockers.filter(value => typeof value === 'string') : [];
    message('#replication-catalog-message', requestedNote || blockers.join(' '), blockers.length ? 'error' : requestedNote ? 'success' : '');
  } catch (error) { message('#replication-catalog-message', error.message, 'error'); }
}
async function syncReplication() {
  $('#replication-sync').disabled = true; $('#replication-sync').textContent = '正在同步…';
  try { await replicationApi('/sync', 'POST', {}); await loadReplicationCatalog(true); }
  catch (error) { message('#replication-catalog-message', error.message, 'error'); }
  finally { $('#replication-sync').disabled = false; $('#replication-sync').textContent = '同步最新参考'; }
}
async function saveReplicationClassification(event) {
  event.preventDefault(); const asset = selectedAsset(); if (!asset) return;
  $('#replication-save-classification').disabled = true;
  try {
    await replicationApi(`/assets/${encodeURIComponent(asset.id)}`, 'PATCH', { material_group: $('#replication-material').value, subject_group: $('#replication-subject').value, apply_character: $('#replication-apply-character').checked });
    await loadReplicationCatalog(true); message('#replication-classification-message', '已保存你确认的角色标记。请重新选择或核对同分组参考。', 'success');
  } catch (error) { message('#replication-classification-message', error.message, 'error'); }
  finally { $('#replication-save-classification').disabled = false; }
}
async function saveReferenceClassification(event) {
  event.preventDefault(); const reference = selectedReference(); if (!reference) return;
  $('#replication-save-reference-classification').disabled = true;
  try {
    await replicationApi(`/references/${encodeURIComponent(reference.id)}`, 'PATCH', { material_group: $('#replication-reference-material').value, subject_group: $('#replication-reference-subject').value });
    await loadReplicationCatalog(true); message('#replication-reference-classification-message', '已在本机保存这条参考的人工标记，未更改公开采集记录。请重新准备这组素材。', 'success');
  } catch (error) { message('#replication-reference-classification-message', error.message, 'error'); }
  finally { $('#replication-save-reference-classification').disabled = false; }
}
function renderReplicationReview() {
  const plan = preparedReplication, review = $('#replication-pair-review'); if (!plan) return;
  const pair = node('div', 'replication-review-pair'), asset = plan.asset, reference = plan.reference;
  if (asset) { const card = node('div', 'review-input'), path = localAssetImage(asset.url); if (path) { const image = node('img'); image.src = path; image.alt = asset.name || '所选角色图片'; card.append(image); } card.append(node('strong', '', `角色图片：${asset.character || asset.name}`), node('p', '', `${asset.name} · ${materialLabel(asset.material_group)} · ${subjectLabel(asset.subject_group)}`)); pair.append(card); }
  if (reference) { const card = node('div', 'review-input review-reference'); card.append(node('span', 'input-type', '动作参考视频'), node('strong', '', reference.title || '所选 TikTok 原片'), node('p', '', `${materialLabel(reference.material_group)} · ${subjectLabel(reference.subject_group)} · ${reference.capture_date || date(reference.first_seen_at)}`)); const source = safeSource(reference.url); if (source) { const link = node('a', 'text-button', '核对原片 ↗'); link.href = source; link.target = '_blank'; link.rel = 'noopener noreferrer'; card.append(link); } card.append(node('p', '', plan.has_reference_video === false ? '动作视频文件尚未准备好' : '已确认对应的 HTTPS MP4 动作文件')); pair.append(card); }
  review.replaceChildren(pair);
  const blockers = Array.isArray(plan.blockers) ? plan.blockers.filter(value => typeof value === 'string') : [];
  if (blockers.length) review.append(node('p', 'job-error', blockers.join('\n')));
  if (typeof plan.note === 'string') review.append(node('p', 'field-note', plan.note));
  if (typeof plan.prompt === 'string') { const details = node('details', 'prepared-prompt'); details.append(node('summary', '', '查看这次角色与动作的生成说明'), node('p', '', plan.prompt)); review.append(details); }
  if (plan.expires_at) review.append(node('p', 'field-note', `本次准备有效至 ${date(plan.expires_at)}（纽约时间）；超时后需重新准备。`));
  updateReplicationButton();
}
async function prepareReplication(event) {
  event.preventDefault(); if (replicationPreparing) return; replicationPreparing = true; updateReplicationButton();
  try {
    if (assetClassificationDirty || referenceClassificationDirty) throw new Error('请先保存角色与参考的男女和人数标记，再准备这组素材。');
    const asset = selectedAsset(), reference = selectedReference(); if (!asset || !reference) throw new Error('请选择我们的角色照片和一条同分组参考。');
    const blockers = matchingBlockers(reference); if (blockers.length) throw new Error(blockers.join(' '));
    const media = referenceValue({ fileData: '', type: { value: 'video_url' }, url: $('#replication-video-url') }).url;
    if (!$('#replication-video-confirmed').checked) throw new Error('请确认这个视频文件对应所选原片，并可用于本次生成。');
    const selection = { asset_id: asset.id, reference_id: reference.id, reference_video_url: media, reference_video_confirmed: true, prompt: $('#replication-prompt').value.trim() };
    invalidateReplication(); const selectionVersion = replicationSelectionVersion; message('#replication-prepare-message', '正在检查这组角色图和动作参考，不调用付费生成。');
    const plan = await replicationApi('/prepare', 'POST', selection);
    const currentMedia = referenceValue({ fileData: '', type: { value: 'video_url' }, url: $('#replication-video-url') }).url;
    if (replicationSelectionVersion !== selectionVersion || selectedAssetId !== selection.asset_id || selectedReferenceId !== selection.reference_id || currentMedia !== selection.reference_video_url || !$('#replication-video-confirmed').checked || $('#replication-prompt').value.trim() !== selection.prompt) throw new Error('准备过程中选择、分类或描述已变化，请重新准备。');
    preparedReplication = plan; renderReplicationReview();
    message('#replication-prepare-message', plan.can_submit === true ? '角色图与动作参考已准备，请在下方确认模型和生成费用。' : '还有条件需要处理，请查看下方说明。', plan.can_submit === true ? 'success' : 'error');
    $('#replication-review').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (error) { preparedReplication = null; message('#replication-prepare-message', error.message, 'error'); }
  finally { replicationPreparing = false; updateReplicationButton(); }
}
function persistReplication(record) { replicationRecord = { key: record.key, fingerprint: record.fingerprint, outcome: record.outcome }; try { localStorage.setItem('idore-replication-submission-v1', JSON.stringify(replicationRecord)); } catch { /* No character images, video links, prompts or provider keys are stored. */ } }
function replicationPayload() {
  const problem = replicationPlanProblem(); if (problem) throw new Error(problem);
  if (!$('#replication-consent').checked) throw new Error('请确认这组角色图片和动作视频的传输与 OpenRouter 额度使用。');
  return { plan_id: preparedReplication.plan_id, model: currentModel().id, duration: Number($('#duration').value), resolution: $('#resolution').value, aspect_ratio: $('#aspect-ratio').value, generate_audio: false, consent: true };
}
function replicationIntent(payload) {
  const parameters = { model: payload.model, duration: payload.duration, resolution: payload.resolution, aspect_ratio: payload.aspect_ratio, generate_audio: false };
  const content = preparedReplication?.content_fingerprint || preparedReplication?.plan_fingerprint;
  if (typeof content === 'string' && /^[a-f0-9]{64}$/.test(content)) return { content_fingerprint: content, ...parameters };
  const media = referenceValue({ fileData: '', type: { value: 'video_url' }, url: $('#replication-video-url') }).url;
  return { source_asset_id: preparedReplication.asset.id, source_reference_id: preparedReplication.reference.id, reference_video_url: media, canonical_prompt: typeof preparedReplication.prompt === 'string' ? preparedReplication.prompt : $('#replication-prompt').value.trim(), ...parameters };
}
async function submitReplication(event) {
  event.preventDefault(); if (replicationSubmitting) return; replicationSubmitting = true; updateReplicationButton(); let key, hash;
  try {
    const payload = replicationPayload(), selectionVersion = replicationSelectionVersion; hash = await fingerprint(replicationIntent(payload));
    if (replicationSelectionVersion !== selectionVersion || JSON.stringify(replicationPayload()) !== JSON.stringify(payload)) throw new Error('跟拍输入或模型参数已经变化，尚未提交生成，请重新检查。');
    if (replicationRecord?.fingerprint === hash && replicationRecord.outcome === 'uncertain') { message('#replication-message', '上次这组跟拍的提交结果还不明确，已阻止重复提交。请先核对本机任务与 OpenRouter 记录。', 'error'); await loadJobs(false); return; }
    if (replicationRecord?.fingerprint === hash && replicationRecord.outcome === 'accepted') { message('#replication-message', '这组跟拍已经提交，未再次扣费。生成记录在下方。', 'success'); $('#new-replication-button').hidden = false; await loadJobs(false); return; }
    key = replicationRecord?.fingerprint === hash && replicationRecord.outcome === 'retryable' ? replicationRecord.key : crypto.randomUUID(); persistReplication({ key, fingerprint: hash, outcome: 'uncertain' });
    message('#replication-message', '正在同时提交所选角色图和动作参考视频，请勿重复点击。'); $('#replication-button').textContent = '正在提交这组角色与视频…';
    const result = await replicationApi('/jobs', 'POST', { ...payload, idempotency_key: key }), job = result.job || result;
    persistReplication({ key, fingerprint: hash, outcome: job.status === 'submission_uncertain' ? 'uncertain' : 'accepted', job_id: job.id });
    $('#new-replication-button').hidden = job.status === 'submission_uncertain';
    message('#replication-message', job.status === 'submission_uncertain' ? '提交结果待核对，未自动重试。请先查看 OpenRouter 任务记录。' : '这组角色图片与动作视频已提交。可在生成记录中查看进度。', job.status === 'submission_uncertain' ? 'error' : 'success'); await loadJobs(false);
  } catch (error) {
    if (key) persistReplication({ key, fingerprint: hash, outcome: error.transport || error.status >= 500 ? 'uncertain' : 'retryable' });
    if (error.status === 409) invalidateReplication('准备方案或素材已变化，请重新准备这组素材。');
    message('#replication-message', `${error.message}${key && (error.transport || error.status >= 500) ? '\n已阻止相同请求重复提交，请先核对任务记录。' : ''}`, 'error'); if (key) await loadJobs(false);
  } finally { replicationSubmitting = false; $('#replication-button').textContent = '用这张角色图跟拍参考视频（消耗 OpenRouter 额度）'; updateReplicationButton(); }
}
async function useSelectedAssetForStyle() {
  const asset = selectedAsset(), path = localAssetImage(asset?.url); if (!asset || !path) { message('#replication-classification-message', '请先选择可读取的角色照片。', 'error'); return; }
  try {
    const response = await fetch(path, { credentials: 'same-origin', cache: 'no-store' }); if (!response.ok) throw new Error('无法读取所选角色照片。'); const blob = await response.blob();
    styleReference = blob.size > imageLimit || !['image/png', 'image/jpeg', 'image/webp'].includes(blob.type) ? await compressImageBlob(blob) : await readImage(blob);
    $('#style-reference-url').value = ''; $('#style-reference-file').value = ''; $('#style-reference-status').textContent = `来自我们的素材库：${asset.character || ''} · ${asset.name}`;
    message('#style-message', '已选用这张真实角色照片。填写前后造型并确认费用后，才会调用 OpenAI。', 'success'); $('#style-step').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (error) { message('#replication-classification-message', error.message, 'error'); }
}

const creativeApi = (path, method = 'GET', body) => api(path, method, body, '/api/creative');
for (const kind of ['images', 'draft']) {
  try { const record = JSON.parse(localStorage.getItem(`idore-creative-${kind}-v1`) || 'null'); if (record && typeof record.key === 'string' && typeof record.fingerprint === 'string') creativeRecords[kind] = record; } catch { /* Keep creation request state in memory for this visit. */ }
}
function persistCreative(kind, record) { creativeRecords[kind] = record; try { localStorage.setItem(`idore-creative-${kind}-v1`, JSON.stringify(record)); } catch { /* No prompts, media or provider keys are stored here. */ } }
function creativeSelector(kind) { return kind === 'images' ? 'style' : 'draft'; }
function updateCreativeButtons() {
  $('#style-button').disabled = !openAIConnected || creativeBusy.images || !creativeModels.image_models.some(model => model.id === $('#image-model').value);
  $('#draft-button').disabled = !openAIConnected || creativeBusy.draft || !creativeModels.text_models.some(model => model.id === $('#text-model').value);
  if (!creativeBusy.draft) $('#draft-button').textContent = $('#search-trends').checked ? '查询近期趋势并生成文案（消耗 OpenAI 额度）' : '生成英文文案与标签（消耗 OpenAI 额度）';
}
function paintOpenAIConnection() {
  $('#openai-status').textContent = openAIConnected ? '已连接 · 密钥仅保存在本机服务内存' : '尚未连接';
  $('#openai-status').classList.toggle('connected', openAIConnected);
  $('#openai-connection-form').hidden = openAIConnected; $('#openai-disconnect-button').hidden = !openAIConnected; updateCreativeButtons();
}
async function loadCreativeModels() {
  const result = await creativeApi('/models');
  for (const [kind, selector] of [['image_models', '#image-model'], ['text_models', '#text-model']]) {
    const previous = $(selector).value;
    creativeModels[kind] = (Array.isArray(result[kind]) ? result[kind] : []).filter(model => model && typeof model.id === 'string');
    const placeholder = node('option', '', creativeModels[kind].length ? '选择本次使用的 OpenAI 模型' : '账号未返回适用的模型'); placeholder.value = ''; $(selector).replaceChildren(placeholder);
    for (const model of creativeModels[kind]) { const option = node('option', '', model.name || model.id); option.value = model.id; $(selector).append(option); }
    $(selector).value = creativeModels[kind].some(model => model.id === previous) ? previous : '';
  }
  updateCreativeButtons();
}
async function loadCreativeConnection() { const result = await creativeApi('/connection'); openAIConnected = result.connected === true; paintOpenAIConnection(); if (openAIConnected) await loadCreativeModels(); }
async function connectOpenAI(event) {
  event.preventDefault(); if (openAIConnecting) return;
  const secret = $('#openai-key').value.trim(); $('#openai-key').value = '';
  if (!secret) { message('#openai-message', '请输入 OpenAI API Key。', 'error'); return; }
  openAIConnecting = true; $('#openai-connect-button').disabled = true; $('#openai-connect-button').textContent = '正在验证…'; message('#openai-message');
  try { const result = await creativeApi('/connection', 'POST', { api_key: secret }); openAIConnected = result.connected === true; paintOpenAIConnection(); if (!openAIConnected) throw new Error('连接尚未建立，请重新验证。'); await loadCreativeModels(); message('#openai-message', '连接成功。可以选择图片与文案模型，尚未调用生成。', 'success'); }
  catch (error) { message('#openai-message', error.message, 'error'); }
  finally { openAIConnecting = false; $('#openai-connect-button').disabled = false; $('#openai-connect-button').textContent = '验证并连接 OpenAI'; }
}
async function disconnectOpenAI() {
  $('#openai-disconnect-button').disabled = true;
  try { await creativeApi('/connection', 'DELETE', {}); openAIConnected = false; paintOpenAIConnection(); message('#openai-message', '已清除本机服务中的 OpenAI 密钥。'); }
  catch (error) { message('#openai-message', error.message, 'error'); }
  finally { $('#openai-disconnect-button').disabled = false; }
}
function stylePayload() {
  const model = $('#image-model').value, before = $('#before-look').value.trim(), after = $('#after-look').value.trim();
  if (!openAIConnected || !creativeModels.image_models.some(item => item.id === model)) throw new Error('请连接 OpenAI 并选择图片模型。');
  if (!before || !after) throw new Error('请分别描述变装前和变装后的造型。');
  if (!$('#style-consent').checked) throw new Error('请确认图片传输和 OpenAI 额度使用。');
  return { model, reference_image: imageValue(styleReference || $('#style-reference-url').value.trim(), '角色参考图'), before_prompt: before, after_prompt: after, consent: true };
}
function draftPayload() {
  const model = $('#text-model').value, description = $('#draft-description').value.trim();
  if (!openAIConnected || !creativeModels.text_models.some(item => item.id === model)) throw new Error('请连接 OpenAI 并选择文案模型。');
  if (!description) throw new Error('请描述这条作品的内容。');
  if (!$('#draft-consent').checked) throw new Error('请确认内容传输和 OpenAI 额度使用。');
  return { model, description, character: $('#draft-character').value.trim(), frames: draftFrames.map(frame => frame.data), trend_context: $('#trend-context').value.trim(), search_trends: $('#search-trends').checked, consent: true };
}
async function submitCreative(kind, event) {
  event.preventDefault(); if (creativeBusy[kind]) return;
  creativeBusy[kind] = true; updateCreativeButtons();
  const prefix = creativeSelector(kind); let hash, key;
  try {
    const payload = kind === 'images' ? stylePayload() : draftPayload(); hash = await fingerprint(payload);
    const record = creativeRecords[kind];
    if (record?.fingerprint === hash && record.outcome === 'uncertain') { message(`#${prefix}-message`, '上次相同请求的结果还不明确，已阻止重复提交。请先核对生成记录与 OpenAI 用量记录。', 'error'); await loadCreativeJobs(); return; }
    if (record?.fingerprint === hash && record.outcome === 'accepted') { message(`#${prefix}-message`, '这个方案已提交，没有重复扣费。原结果保留在下方；需要另一组时可明确新建。', 'success'); $(`#new-${prefix}-button`).hidden = false; await loadCreativeJobs(); return; }
    key = record?.fingerprint === hash && record.outcome === 'retryable' ? record.key : crypto.randomUUID();
    persistCreative(kind, { key, fingerprint: hash, outcome: 'uncertain' });
    message(`#${prefix}-message`, kind === 'images' ? '正在生成前后造型图，可能需要几分钟。请保留页面，勿重复提交。' : '正在整理英文文案与标签。请保留页面，勿重复提交。');
    $(`#${prefix}-button`).textContent = kind === 'images' ? '造型图请求处理中…' : '文案请求处理中…';
    const job = await creativeApi(kind === 'images' ? '/images' : '/drafts', 'POST', { ...payload, idempotency_key: key });
    persistCreative(kind, { key, fingerprint: hash, outcome: job.status === 'submission_uncertain' ? 'uncertain' : 'accepted', job_id: job.id });
    const uncertain = job.status === 'submission_uncertain'; $(`#new-${prefix}-button`).hidden = uncertain;
    message(`#${prefix}-message`, uncertain ? '提交结果待核对，未自动重试。请先核对任务与 OpenAI 用量。' : job.status === 'partial' ? '已保留部分造型图。请查看具体结果；不会自动重试或重新收费。' : kind === 'images' ? '任务已保存。两张图完成后，可放入视频首尾帧。' : '任务已保存。英文文案与标签会显示在右侧。', uncertain || job.status === 'failed' ? 'error' : 'success');
    await loadCreativeJobs();
  } catch (error) {
    if (key) persistCreative(kind, { key, fingerprint: hash, outcome: error.transport || error.status >= 500 ? 'uncertain' : 'retryable' });
    message(`#${prefix}-message`, `${error.message}${key && (error.transport || error.status >= 500) ? '\n提交结果还不明确，已阻止相同请求重复提交，请先核对记录。' : ''}`, 'error');
    if (key) await loadCreativeJobs();
  } finally { creativeBusy[kind] = false; $('#style-button').textContent = '生成两张造型图（消耗 OpenAI 额度）'; updateCreativeButtons(); }
}
function localImage(value) {
  if (typeof value !== 'string') return '';
  try { const url = new URL(value, location.origin); return url.origin === location.origin && /^\/generated\/style-[a-f0-9]{32}-(before|after)\.png$/.test(url.pathname) ? url.pathname : ''; } catch { return ''; }
}
function safeSource(value) { try { const url = new URL(value); return url.protocol === 'https:' && !url.username && !url.password ? url.href : ''; } catch { return ''; } }
async function compressImageBlob(blob, maxEdge = 1536) {
  const bitmap = await createImageBitmap(blob);
  try {
    for (const edge of [maxEdge, Math.min(maxEdge, 1024), Math.min(maxEdge, 768)]) {
      const scale = Math.min(1, edge / Math.max(bitmap.width, bitmap.height)), canvas = document.createElement('canvas');
      canvas.width = Math.max(1, Math.round(bitmap.width * scale)); canvas.height = Math.max(1, Math.round(bitmap.height * scale));
      const context = canvas.getContext('2d'); if (!context) throw new Error('当前浏览器无法准备图片。');
      context.fillStyle = '#ffffff'; context.fillRect(0, 0, canvas.width, canvas.height); context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      const data = canvas.toDataURL('image/jpeg', 0.84); if (data.length * 0.75 <= imageLimit) return data;
    }
    throw new Error('图片仍然过大，请缩小后重新选择。');
  } finally { bitmap.close(); }
}
async function generatedImageData(value) {
  const path = localImage(value); if (!path) throw new Error('尚未取得本机保存的造型图。');
  const response = await fetch(path, { credentials: 'same-origin', cache: 'no-store' }); if (!response.ok) throw new Error('无法读取已保存的造型图，请刷新记录后重试。');
  const blob = await response.blob(); if (!['image/png', 'image/jpeg', 'image/webp'].includes(blob.type)) throw new Error('造型图格式无法读取。');
  if (blob.size > 24 * 1024 * 1024) throw new Error('造型图过大，请先下载并缩小图片。');
  return blob.size > imageLimit ? compressImageBlob(blob) : readImage(blob);
}
function fillVideoFrame(frame, data, label) {
  files.set(frame, data); $(`#${frame}-frame-url`).value = ''; $(`#${frame}-frame-file`).value = ''; $(`#${frame}-frame-status`).textContent = label;
}
async function useStyleImages(job, target = 'pair') {
  try {
    const values = target === 'pair' ? await Promise.all([generatedImageData(job.before_url), generatedImageData(job.after_url)]) : [await generatedImageData(target === 'first' ? job.before_url : job.after_url)];
    if (target === 'pair') { fillVideoFrame('first', values[0], '已选：生成的变装前造型'); fillVideoFrame('last', values[1], '已选：生成的变装后造型'); requiredStylePair = true; }
    else fillVideoFrame(target, values[0], target === 'first' ? '已选：生成的变装前造型' : '已选：生成的变装后造型');
    const caps = capabilities(currentModel());
    if (target === 'pair' && caps.first && caps.last) $('#mode').value = 'frames';
    else if (target !== 'pair' && caps.first) $('#mode').value = 'frames';
    configureMode();
    if (!$('#prompt').value.trim()) $('#prompt').value = 'Keep the same character identity and camera framing. Transition naturally from the first look to the final look, with consistent face, hairstyle, body proportions and smooth motion.';
    $('#style-handoff-note').hidden = false; $('#style-handoff-note').textContent = target === 'pair' ? '前后造型已放入首尾帧。选择支持首尾帧的 Seedance 模型，检查动作描述，再点击视频生成。' : '造型图已放入视频输入。请检查其余素材和动作描述，再点击视频生成。';
    updateGenerateButton();
    message('#generation-message', requiredStylePair && !stylePairReady(caps) ? '两张造型图已保留。请选择同时支持首帧和尾帧的模型；确认两张图都能使用后才可生成。' : '图片已准备，尚未提交视频生成。请检查模型与动作描述。', requiredStylePair && !stylePairReady(caps) ? 'error' : 'success'); $('#video-step').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (error) { message('#style-message', error.message, 'error'); }
}
function renderDraftFrames() {
  const list = $('#draft-frame-list'); list.replaceChildren();
  for (const frame of draftFrames) {
    const item = node('div', 'draft-frame'), image = node('img'), remove = node('button', 'text-button', '移除'); image.src = frame.data; image.alt = frame.name; remove.type = 'button';
    remove.addEventListener('click', () => { draftFrames.splice(draftFrames.indexOf(frame), 1); renderDraftFrames(); }); item.append(image, node('small', '', frame.name), remove); list.append(item);
  }
  $('#draft-input-note').textContent = draftFrames.some(frame => frame.sampled) ? `已选 ${draftFrames.length} 张成片关键帧；匹配依据为这些抽帧画面和描述，并非逐帧分析完整视频。` : draftFrames.length ? `已选 ${draftFrames.length} 张图片，将与文字描述一起用于匹配文案。` : '可添加成片关键帧；不添加时会根据你的文字描述创作。';
}
function waitVideo(video, event, action, timeout = 20000) {
  return new Promise((resolve, reject) => {
    const clean = () => { clearTimeout(timer); video.removeEventListener(event, complete); video.removeEventListener('error', failed); };
    const complete = () => { clean(); resolve(); }, failed = () => { clean(); reject(new Error('浏览器无法解码这条视频，请手动上传成片关键帧。')); };
    const timer = setTimeout(() => { clean(); reject(new Error('读取视频关键帧超时，请手动上传成片截图。')); }, timeout);
    video.addEventListener(event, complete, { once: true }); video.addEventListener('error', failed, { once: true });
    try { action(); } catch (error) { clean(); reject(error); }
  });
}
async function extractVideoFrames(path) {
  const video = node('video'); video.muted = true; video.playsInline = true; video.preload = 'auto';
  try {
    await waitVideo(video, 'loadedmetadata', () => { video.src = path; video.load(); });
    if (!Number.isFinite(video.duration) || video.duration <= 0 || !video.videoWidth || !video.videoHeight) throw new Error('无法读取视频画面，请手动上传成片关键帧。');
    const canvas = document.createElement('canvas'), scale = Math.min(1, 768 / Math.max(video.videoWidth, video.videoHeight)); canvas.width = Math.max(1, Math.round(video.videoWidth * scale)); canvas.height = Math.max(1, Math.round(video.videoHeight * scale));
    const context = canvas.getContext('2d'); if (!context) throw new Error('当前浏览器无法选取画面，请手动上传成片截图。');
    const result = [];
    for (const fraction of [0.1, 0.5, 0.9]) {
      const target = Math.max(0, Math.min(video.duration - 0.04, video.duration * fraction));
      await waitVideo(video, 'seeked', () => { video.currentTime = target; });
      context.drawImage(video, 0, 0, canvas.width, canvas.height); result.push({ name: `成片 ${Math.round(fraction * 100)}% 处`, data: canvas.toDataURL('image/jpeg', 0.84), sampled: true });
    }
    return result;
  } finally { video.removeAttribute('src'); video.load(); }
}
async function useVideoForDraft(job) {
  const path = localVideo(job.result_url); if (!path || sampling.has(job.id)) return;
  sampling.add(job.id); renderJobs(); message('#draft-message', '正在本机选取成片关键帧，尚未调用 OpenAI。');
  try {
    const frames = await extractVideoFrames(path); draftFrames.splice(0, draftFrames.length, ...frames); renderDraftFrames();
    $('#draft-description').value = typeof job.prompt === 'string' ? job.prompt : ''; $('#draft-input-note').textContent = '已从这条成片选取 3 张关键帧；匹配依据为这些抽帧画面和描述，并非逐帧分析完整视频。';
    message('#draft-message', '成片关键帧已准备。检查内容、角色与趋势选项，再点击生成文案。', 'success'); $('#draft-step').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (error) { message('#draft-message', error.message, 'error'); $('#draft-step').scrollIntoView({ behavior: 'smooth', block: 'start' }); }
  finally { sampling.delete(job.id); renderJobs(); }
}
async function copyText(text, button) {
  try { await navigator.clipboard.writeText(text); const original = button.textContent; button.textContent = '已复制'; setTimeout(() => { button.textContent = original; }, 1800); }
  catch { message('#draft-message', '当前浏览器未允许复制，请直接选中文本复制。', 'error'); }
}
function imageResult(job) {
  const result = node('article', 'creative-job'), top = node('div', 'creative-job-heading');
  const labels = { running: '正在生成', completed: '造型图完成', partial: '部分造型图已保留', failed: '生成未完成', submission_uncertain: '提交结果待核对' };
  top.append(node('strong', '', labels[job.status] || '状态待确认'), node('small', '', `${date(job.created_at)} · 纽约`)); result.append(top);
  const pair = node('div', 'style-pair');
  for (const [key, label, target] of [['before_url', '变装前', 'first'], ['after_url', '变装后', 'last']]) {
    const figure = node('figure', 'style-figure'), path = localImage(job[key]); figure.append(node('figcaption', '', label));
    if (path) {
      const image = node('img'); image.src = path; image.alt = `${label}造型图`; image.loading = 'lazy'; figure.append(image);
      const actions = node('div', 'job-actions'), use = node('button', 'button small', `用作${target === 'first' ? '首帧' : '尾帧'}`), download = node('a', 'text-button', '下载图片'); use.type = 'button'; use.addEventListener('click', () => useStyleImages(job, target)); download.href = path; download.download = `idore-${target}.png`; actions.append(use, download); figure.append(actions);
    } else figure.append(node('p', 'image-placeholder', job.status === 'running' ? '图片生成中…' : '尚未取得这张图片'));
    pair.append(figure);
  }
  result.append(pair);
  if (localImage(job.before_url) && localImage(job.after_url)) { const next = node('button', 'button primary full', '下一步：将两张图放入视频首尾帧'); next.type = 'button'; next.addEventListener('click', () => useStyleImages(job)); result.append(next); }
  if (job.error) result.append(node('p', 'job-error', typeof job.error === 'string' ? job.error : '任务未完成。'));
  if (job.status === 'submission_uncertain') result.append(node('p', 'job-uncertain', '提交结果待核对，未自动重试。请先查看 OpenAI 用量记录。'));
  return result;
}
function draftResult(job) {
  const result = node('article', 'creative-job');
  if (!job.draft || typeof job.draft !== 'object') { result.append(node('p', 'empty-inline', job.status === 'running' ? '正在生成英文文案与标签…' : job.status === 'submission_uncertain' ? '提交结果待核对，未自动重试。请先查看 OpenAI 用量记录。' : '本次没有取得可用的文案结果。')); if (job.error) result.append(node('p', 'job-error', typeof job.error === 'string' ? job.error : '文案生成未完成。')); return result; }
  result.append(node('p', 'draft-result-meta', `TikTok · 美国 · 英文 · ${date(job.created_at)}（纽约）`));
  const captions = Array.isArray(job.draft.captions) ? job.draft.captions.filter(value => typeof value === 'string').slice(0, 3) : [];
  for (const [index, text] of captions.entries()) { const card = node('div', 'caption-option'), top = node('div', 'caption-top'), copy = node('button', 'text-button', '复制文案'); copy.type = 'button'; copy.addEventListener('click', () => copyText(text, copy)); top.append(node('strong', '', `文案 ${index + 1}`), copy); card.append(top, node('p', '', text)); result.append(card); }
  const hashtags = Array.isArray(job.draft.hashtags) ? job.draft.hashtags.filter(tag => tag && typeof tag.tag === 'string') : [];
  if (hashtags.length) {
    const heading = node('div', 'caption-top'), copy = node('button', 'text-button', '复制全部 hashtags'); copy.type = 'button'; copy.addEventListener('click', () => copyText(hashtags.map(tag => tag.tag.startsWith('#') ? tag.tag : '#' + tag.tag).join(' '), copy)); heading.append(node('h4', '', '推荐 hashtags'), copy); result.append(heading);
    for (const item of hashtags) {
      const row = node('div', 'hashtag-row'), source = safeSource(item.source_url), verifiedTrend = item.category === 'trend' && source;
      row.append(node('strong', '', item.tag.startsWith('#') ? item.tag : '#' + item.tag), node('span', '', item.category === 'brand' ? '角色 / 品牌' : verifiedTrend ? '趋势候选 · 有公开来源' : '内容相关'));
      if (typeof item.reason === 'string') row.append(node('p', '', item.reason));
      if (source) { const link = node('a', '', '查看标签依据 ↗'); link.href = source; link.target = '_blank'; link.rel = 'noopener noreferrer'; row.append(link); if (item.observed_at && Number.isFinite(new Date(item.observed_at).getTime())) row.append(node('small', 'tag-observed-at', `本次检索 ${date(item.observed_at)}（纽约）`)); }
      result.append(row);
    }
  }
  if (typeof job.draft.trend_note === 'string') result.append(node('p', 'trend-note', job.draft.trend_note));
  const sources = (Array.isArray(job.sources) ? job.sources : []).filter(source => source && safeSource(typeof source === 'string' ? source : source.url));
  if (sources.length) { const details = node('details', 'draft-sources'); details.append(node('summary', '', `本次检索来源 · ${sources.length} 条`)); for (const source of sources) { const url = safeSource(typeof source === 'string' ? source : source.url), link = node('a', '', typeof source.title === 'string' ? source.title : new URL(url).hostname); link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer'; details.append(link); if (source.observed_at && Number.isFinite(new Date(source.observed_at).getTime())) details.append(node('small', 'tag-observed-at', `本次检索 ${date(source.observed_at)}（纽约）`)); } result.append(details); }
  return result;
}
async function loadCreativeJobs() {
  try {
    const result = await creativeApi('/jobs'); creativeJobs = (Array.isArray(result.jobs) ? result.jobs : []).filter(job => job && typeof job.id === 'string').sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
    for (const kind of ['images', 'draft']) { const record = creativeRecords[kind]; if (record?.outcome === 'uncertain') { const recovered = creativeJobs.find(job => job.kind === kind && (job.id === record.job_id || job.idempotency_key === record.key)); if (recovered && recovered.status !== 'submission_uncertain') persistCreative(kind, { ...record, outcome: 'accepted', job_id: recovered.id }); } }
    const signature = JSON.stringify(creativeJobs); if (signature === creativeSignature) return; creativeSignature = signature;
    const images = creativeJobs.filter(job => job.kind === 'images').slice(0, 5), drafts = creativeJobs.filter(job => job.kind === 'draft').slice(0, 5);
    $('#style-results').replaceChildren(...(images.length ? images.map(imageResult) : [node('p', 'empty-inline', '造型图会显示在这里。')]));
    $('#draft-results').replaceChildren(...(drafts.length ? drafts.map(draftResult) : [node('p', 'empty-inline', '英文文案与标签会显示在这里。')]));
  } catch (error) { if (!creativeBusy.images && !creativeBusy.draft) message('#draft-message', error.message, 'error'); }
}

$('#connection-form').addEventListener('submit', connect); $('#disconnect-button').addEventListener('click', disconnect);
$('#replication-character').addEventListener('change', renderReplicationAssets); $('#replication-stage').addEventListener('change', renderReplicationAssets);
$('#replication-sync').addEventListener('click', syncReplication); $('#replication-classification-form').addEventListener('submit', saveReplicationClassification); $('#replication-use-style').addEventListener('click', useSelectedAssetForStyle);
$('#replication-reference-classification-form').addEventListener('submit', saveReferenceClassification);
for (const selector of ['#replication-reference-material', '#replication-reference-subject']) $(selector).addEventListener('change', () => { referenceClassificationDirty = true; invalidateReplication('参考分类正在调整，请保存本机标记后重新准备。'); });
$('#replication-prepare-form').addEventListener('submit', prepareReplication); $('#replication-submit-form').addEventListener('submit', submitReplication);
for (const selector of ['#replication-material', '#replication-subject', '#replication-apply-character']) $(selector).addEventListener('change', () => { assetClassificationDirty = true; invalidateReplication('角色分类正在调整，请保存标记后重新准备。'); });
for (const selector of ['#replication-video-url', '#replication-prompt']) $(selector).addEventListener('input', () => invalidateReplication('动作文件或描述已变化，请重新准备这组素材。'));
$('#replication-video-confirmed').addEventListener('change', () => { if (!$('#replication-video-confirmed').checked) invalidateReplication('请重新确认动作文件来源，并重新准备。'); });
for (const selector of ['#duration', '#resolution', '#aspect-ratio']) $(selector).addEventListener('change', updateReplicationButton);
$('#new-replication-button').addEventListener('click', () => { if (replicationSubmitting || !$('#replication-submit-form').reportValidity()) return; if (replicationRecord?.outcome !== 'accepted') { message('#replication-message', '上次提交还未确认，不能新建同一组付费请求。请先核对任务记录。', 'error'); return; } replicationRecord = null; try { localStorage.removeItem('idore-replication-submission-v1'); } catch { /* In-memory paired request state is also cleared. */ } $('#new-replication-button').hidden = true; $('#replication-submit-form').requestSubmit(); });
$('#generation-form').addEventListener('submit', generate); $('#model').addEventListener('change', configureModel); $('#mode').addEventListener('change', changeGenerationMode); $('#add-reference').addEventListener('click', addReference);
$('#generation-form').addEventListener('input', () => { $('#new-generation-button').hidden = true; });
$('#new-generation-button').addEventListener('click', () => { if (submitting || !$('#generation-form').reportValidity()) return; submissionRecord = null; try { localStorage.removeItem(submissionStorage); } catch { /* In-memory request state is also cleared. */ } $('#new-generation-button').hidden = true; $('#generation-form').requestSubmit(); });
$('#reload-jobs').addEventListener('click', () => loadJobs(false));
$('#openai-connection-form').addEventListener('submit', connectOpenAI); $('#openai-disconnect-button').addEventListener('click', disconnectOpenAI);
$('#style-form').addEventListener('submit', event => submitCreative('images', event)); $('#draft-form').addEventListener('submit', event => submitCreative('draft', event));
$('#image-model').addEventListener('change', updateCreativeButtons); $('#text-model').addEventListener('change', updateCreativeButtons); $('#search-trends').addEventListener('change', updateCreativeButtons);
$('#reload-creative').addEventListener('click', loadCreativeJobs);
for (const [kind, prefix] of [['images', 'style'], ['draft', 'draft']]) {
  $(`#${prefix}-form`).addEventListener('input', () => { $(`#new-${prefix}-button`).hidden = true; });
  $(`#new-${prefix}-button`).addEventListener('click', () => { if (creativeBusy[kind] || !$(`#${prefix}-form`).reportValidity()) return; delete creativeRecords[kind]; try { localStorage.removeItem(`idore-creative-${kind}-v1`); } catch { /* In-memory creation request state is also cleared. */ } $(`#new-${prefix}-button`).hidden = true; $(`#${prefix}-form`).requestSubmit(); });
}
$('#style-reference-file').addEventListener('change', async () => {
  const input = $('#style-reference-file');
  try { const file = input.files[0]; styleReference = await readImage(file); $('#style-reference-url').value = ''; $('#style-reference-status').textContent = `已选：${file.name}`; message('#style-message'); }
  catch (error) { styleReference = ''; input.value = ''; $('#style-reference-status').textContent = ''; message('#style-message', error.message, 'error'); }
});
$('#style-reference-url').addEventListener('input', () => { styleReference = ''; $('#style-reference-file').value = ''; $('#style-reference-status').textContent = ''; });
$('#clear-style-reference').addEventListener('click', () => { styleReference = ''; $('#style-reference-file').value = ''; $('#style-reference-url').value = ''; $('#style-reference-status').textContent = ''; });
$('#draft-frames').addEventListener('change', async () => {
  const input = $('#draft-frames'), selected = [...input.files];
  try { if (selected.length > 4) throw new Error('最多选择 4 张关键帧。'); const frames = await Promise.all(selected.map(async file => ({ name: file.name, data: await readImage(file) }))); draftFrames.splice(0, draftFrames.length, ...frames); renderDraftFrames(); message('#draft-message'); }
  catch (error) { message('#draft-message', error.message, 'error'); }
  finally { input.value = ''; }
});
$('#use-video-description').addEventListener('click', () => { const prompt = $('#prompt').value.trim(); if (!prompt) { message('#draft-message', '请先填写视频动作描述，或直接在这里描述成片内容。', 'error'); return; } $('#draft-description').value = prompt; message('#draft-message', '已填入视频动作描述。请检查成片实际表现，并可添加关键帧。'); });
for (const frame of ['first', 'last']) {
  $(`#${frame}-frame-file`).addEventListener('change', async () => { const input = $(`#${frame}-frame-file`); try { const file = input.files[0]; files.set(frame, await readImage(file)); $(`#${frame}-frame-url`).value = ''; $(`#${frame}-frame-status`).textContent = `已选：${file.name}`; message('#generation-message'); } catch (error) { files.delete(frame); input.value = ''; $(`#${frame}-frame-status`).textContent = ''; message('#generation-message', error.message, 'error'); } finally { updateGenerateButton(); } });
  $(`#${frame}-frame-url`).addEventListener('input', () => { files.delete(frame); $(`#${frame}-frame-file`).value = ''; $(`#${frame}-frame-status`).textContent = ''; updateGenerateButton(); });
  $(`#clear-${frame}-frame`).addEventListener('click', () => { files.delete(frame); $(`#${frame}-frame-url`).value = ''; $(`#${frame}-frame-file`).value = ''; $(`#${frame}-frame-status`).textContent = ''; if (requiredStylePair) { requiredStylePair = false; message('#generation-message', '已清除一张造型图并解除双图联动。请检查当前生成方式与剩余素材。'); } updateGenerateButton(); });
}
loadConnection().catch(error => { message('#connection-message', error.message, 'error'); paintConnection(); });
loadCreativeConnection().catch(error => { message('#openai-message', error.message, 'error'); paintOpenAIConnection(); });
loadReplicationCatalog();
poll();
