'use strict';

const $ = selector => document.querySelector(selector);
const MATERIAL_LABELS = { male: '男生', female: '女生', mixed: '男女混合', unknown: '男女分组未标记' };
const CAST_LABELS = { solo: '单人', group: '多人', unknown: '人数未标记' };
const FILTER_LABELS = { all: '全部作品', character: '角色作品', api_test: '接口测试' };
const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{0,119}$/;
let creations = [], selectedKind = 'all', manifestUpdatedAt = null, loading = false;

function element(tag, className, text) { const item = document.createElement(tag); if (className) item.className = className; if (text !== undefined) item.textContent = String(text); return item; }
function boundedText(value, maximum) { return typeof value === 'string' ? value.trim().slice(0, maximum) : ''; }
function timestamp(value) { if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T.+(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return null; const moment = new Date(value).getTime(); return Number.isFinite(moment) ? moment : null; }
function formatDate(value) { return timestamp(value) === null ? '时间未记录' : new Intl.DateTimeFormat('zh-CN', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(new Date(value)); }
function workflowUrl(value) { try { const url = new URL(value); return url.protocol === 'https:' && url.host === 'github.com' && !url.username && !url.password && !url.search && !url.hash && /^\/yjr927\/idore_tools\/actions\/runs\/[0-9]{1,25}$/.test(url.pathname) ? url.href : ''; } catch { return ''; } }
function normalizeCreation(value) {
  if (!value || typeof value.id !== 'string' || !ID_PATTERN.test(value.id) || !['character', 'api_test'].includes(value.kind) || value.video_path !== `generated/${value.id}.mp4` || typeof value.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(value.sha256)) return null;
  return { id: value.id, title: boundedText(value.title, 160) || (value.kind === 'api_test' ? '接口测试视频' : '已保存的角色作品'), character: boundedText(value.character, 80), kind: value.kind,
    material: Object.hasOwn(MATERIAL_LABELS, value.material_group) ? value.material_group : 'unknown', cast: Object.hasOwn(CAST_LABELS, value.cast_type) ? value.cast_type : 'unknown',
    createdAt: timestamp(value.created_at) === null ? null : value.created_at, moment: timestamp(value.created_at) ?? -Infinity,
    model: boundedText(value.model, 140) || '模型未记录', duration: typeof value.duration === 'number' && Number.isFinite(value.duration) && value.duration > 0 && value.duration <= 300 ? value.duration : null,
    resolution: typeof value.resolution === 'string' && /^(?:[0-9]{3,4}p|4k)$/.test(value.resolution) ? value.resolution : '', aspect: typeof value.aspect_ratio === 'string' && /^[1-9][0-9]?:[1-9][0-9]?$/.test(value.aspect_ratio) ? value.aspect_ratio : '',
    video: new URL(`./generated/${value.id}.mp4`, location.href).href, poster: value.poster_path === `generated/${value.id}.jpg` ? new URL(`./generated/${value.id}.jpg`, location.href).href : '', workflow: workflowUrl(value.workflow_url) };
}
function status(text = '', failed = false) { $('#gallery-status').textContent = text; $('#gallery-status').className = failed ? 'status error' : 'status'; }
function emptyState(filtered = false) {
  const empty = element('div', 'empty-state'); empty.append(element('span', '', '✳'), element('h3', '', filtered ? '这个分类，还在等第一条作品。' : '作品，会从这里开始。'), element('p', '', filtered ? '切换到全部作品，看看已经保存的视频。' : '已保存的成片会按时间排列在这里，随时播放和下载。'));
  const link = element('a', 'button', filtered ? '查看全部作品' : '开始角色创作 ↗');
  if (filtered) { link.href = '#collection-heading'; link.addEventListener('click', event => { event.preventDefault(); selectedKind = 'all'; render(); }); } else link.href = './create.html';
  empty.append(link); return empty;
}
function creationCard(creation) {
  const card = element('article', 'creation-card'), media = element('div', 'creation-media'), video = element('video');
  video.src = creation.video; if (creation.poster) video.poster = creation.poster; video.controls = true; video.playsInline = true; video.preload = 'metadata'; video.setAttribute('aria-label', creation.title + '，点击播放');
  const type = element('span', 'creation-type' + (creation.kind === 'api_test' ? ' api-test' : ''), creation.kind === 'api_test' ? '接口测试' : '角色作品'); media.append(video, type);
  const body = element('div', 'creation-body'), date = element('p', 'creation-date', formatDate(creation.createdAt) + (creation.createdAt ? ' · 纽约' : ''));
  body.append(date, element('h3', '', creation.title));
  const tags = element('div', 'creation-tags'); tags.append(element('span', 'character-tag', creation.character || '角色未标记'), element('span', '', MATERIAL_LABELS[creation.material]), element('span', '', CAST_LABELS[creation.cast])); body.append(tags);
  const note = element('p', 'playback-note', '视频暂时无法播放，可以尝试下载后观看。'); note.hidden = true; body.append(note);
  video.addEventListener('error', () => { note.hidden = false; });
  video.addEventListener('play', () => { for (const other of $('#gallery-grid').querySelectorAll('video')) if (other !== video) other.pause(); });
  const details = element('details', 'creation-info'); details.append(element('summary', '', '作品信息'), element('p', '', creation.model));
  const specs = [creation.duration === null ? '' : `${creation.duration} 秒`, creation.resolution, creation.aspect].filter(Boolean).join(' · '); if (specs) details.append(element('p', '', specs));
  if (creation.kind === 'api_test') details.append(element('p', '', '这是一条接口测试片，单独标记以便与角色作品区分。'));
  if (creation.workflow) { const source = element('a', '', '查看生成记录 ↗'); source.href = creation.workflow; source.target = '_blank'; source.rel = 'noopener noreferrer'; details.append(source); }
  body.append(details);
  const actions = element('div', 'creation-actions'), download = element('a', 'button', '下载 MP4'); download.href = creation.video; download.download = creation.id + '.mp4'; download.setAttribute('aria-label', '下载 ' + creation.title + ' MP4'); actions.append(download); body.append(actions); card.append(media, body); return card;
}
function render() {
  for (const video of $('#gallery-grid').querySelectorAll('video')) video.pause();
  const selected = selectedKind === 'all' ? creations : creations.filter(item => item.kind === selectedKind);
  $('#collection-count').textContent = `${selected.length} 件`;
  $('#gallery-updated').textContent = manifestUpdatedAt ? '更新于 ' + formatDate(manifestUpdatedAt) + ' · 纽约' : '';
  for (const button of $('#kind-filters').querySelectorAll('[data-kind]')) { const active = button.dataset.kind === selectedKind, count = button.dataset.kind === 'all' ? creations.length : creations.filter(item => item.kind === button.dataset.kind).length; button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active)); button.textContent = `${FILTER_LABELS[button.dataset.kind]} ${count}`; }
  $('#gallery-grid').replaceChildren(...(selected.length ? selected.map(creationCard) : [emptyState(selectedKind !== 'all')]));
}
async function loadGallery() {
  if (loading) return; loading = true; $('#reload-gallery').disabled = true; $('#gallery-grid').setAttribute('aria-busy', 'true'); status('正在读取已保存的作品…');
  try {
    const url = new URL('./data/creations.json', location.href), response = await fetch(url.href, { credentials: 'omit', cache: 'no-store', redirect: 'error' });
    if (response.status === 404) { creations = []; manifestUpdatedAt = null; render(); status(); return; }
    if (!response.ok || response.url && response.url !== url.href || Number(response.headers?.get('Content-Length')) > 2 * 1024 * 1024) throw new Error();
    const raw = await response.text(); if (raw.length > 2 * 1024 * 1024) throw new Error();
    const document = JSON.parse(raw); if (!document || document.schema_version !== 1 || !Array.isArray(document.items) || document.items.length > 1000) throw new Error();
    const seen = new Set(); creations = document.items.map(normalizeCreation).filter(item => { if (!item || seen.has(item.id)) return false; seen.add(item.id); return true; }).sort((a, b) => (b.moment - a.moment) || a.id.localeCompare(b.id));
    manifestUpdatedAt = timestamp(document.updated_at) === null ? null : document.updated_at; render(); status(creations.length < document.items.length ? '部分作品信息暂时无法显示，其余作品仍可播放与下载。' : '');
  } catch { if (!creations.length) { $('#collection-count').textContent = '暂不可读'; const empty = element('div', 'empty-state'); empty.append(element('span', '', '↻'), element('h3', '', '作品暂时无法读取。'), element('p', '', '稍后点击“刷新作品”再试，已保存的视频不会因此被移除。')); $('#gallery-grid').replaceChildren(empty); } status('暂时无法读取作品，请稍后刷新。', true); }
  finally { loading = false; $('#reload-gallery').disabled = false; $('#gallery-grid').setAttribute('aria-busy', 'false'); }
}
for (const button of $('#kind-filters').querySelectorAll('[data-kind]')) button.addEventListener('click', () => { selectedKind = button.dataset.kind; render(); });
$('#reload-gallery').addEventListener('click', loadGallery);
loadGallery();
