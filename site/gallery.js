'use strict';

const workerOrigin = 'https://idore-tools.yjr927.workers.dev';
const onWorker = location.origin === workerOrigin;
const pagesRoot = 'https://yjr927.github.io/idore_tools/';
const assetBase = onWorker ? pagesRoot : new URL('./', location.href).href;
const $ = selector => document.querySelector(selector);
const shaPattern = /^[a-f0-9]{64}$/;
const dateFormatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
});
const receiptLabels = {
  reserved: ['等待发送', 'busy'], processing: ['TikTok 处理中', 'busy'],
  delivered: ['已送达 TikTok 收件箱', 'done'], published: ['已在 TikTok 发布', 'done'],
  failed: ['发送失败', 'issue'], rejected: ['TikTok 拒绝了上传', 'issue'], uncertain: ['投递结果待核实', 'issue']
};
const queueLabels = {
  waiting_classification: '等待素材分类', waiting_material: '等待可用角色图', queued: '已排队',
  preparing_motion: '提取动作中', rendering_clay: '渲染动作中', generating_styles: '准备造型中',
  rendering_sequence: '正在串剪角色照片', generating_video: '生成影片中', generating_copy: '生成文案中', needs_attention: '制作需要检查',
  rejected: '已退回制作'
};
const problemLabels = {
  reconnect_required: '请重新授权 TikTok。', catalog_unavailable: '暂时无法读取成片库。',
  media_unavailable: '暂时无法读取视频文件。', delivery_uncertain: '有一条投递需要核实。',
  quota_limited: '已达到 TikTok 上传限额。', provider_rejected: 'TikTok 拒绝了上传。',
  temporary_failure: '沙盒暂时不可用。'
};

let library = null;
let queue = null;
let feed = null;
let loading = false;
let changing = false;
const cardCache = new Map();
const warnings = new Set();

function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
}

function dateValue(raw) {
  if (typeof raw !== 'string' || !/^\d{4}-\d\d-\d\dT.+(?:Z|[+-]\d\d:\d\d)$/.test(raw)) return null;
  const time = Date.parse(raw);
  return Number.isFinite(time) ? time : null;
}

function when(raw) {
  const date = dateValue(raw);
  if (date === null) return node('small', '', '时间未记录');
  const result = node('time', '', dateFormatter.format(date));
  result.dateTime = raw;
  return result;
}

async function readJson(path, options = {}) {
  const response = await fetch(path, { cache: 'no-store', credentials: 'same-origin', redirect: 'error', ...options });
  if (!response.ok) throw Error('http ' + response.status);
  if (Number(response.headers.get('Content-Length')) > 3000000) throw Error('too large');
  const text = await response.text();
  if (text.length > 3000000) throw Error('too large');
  return JSON.parse(text);
}

async function readManifest(name) {
  const data = await readJson(new URL('data/' + name + '.json', assetBase), { credentials: 'omit' });
  if (data?.schema_version !== 1 || !Array.isArray(data.items)) throw Error('invalid manifest');
  return data.items;
}

function normalizeFilm(raw) {
  if (!raw || !shaPattern.test(raw.sha256) || !['character', 'api_test'].includes(raw.kind)) return null;
  const id = 'video-' + raw.sha256.slice(0, 24);
  if (raw.id !== id || raw.video_path !== 'generated/' + id + '.mp4') return null;
  return {
    id, sha: raw.sha256, kind: raw.kind, video: new URL(raw.video_path, assetBase).href,
    poster: raw.poster_path === 'generated/' + id + '.jpg' ? new URL(raw.poster_path, assetBase).href : '',
    title: typeof raw.title === 'string' && raw.title.trim() ? raw.title.trim().slice(0, 180) : '未命名成片',
    created: dateValue(raw.generated_at) !== null ? raw.generated_at : raw.created_at,
    archivedOnly: raw.timestamp_basis === 'workflow_artifact', sequence: raw.generation_method === 'photo_sequence',
    ensemble: raw.sequence_type === 'ip_cast',
    photoCount: Array.isArray(raw.source_photo_ids) ? new Set(raw.source_photo_ids.filter(id => typeof id === 'string')).size : 0
  };
}

function validFilms(rows) {
  const films = new Map();
  for (const raw of rows) {
    const film = normalizeFilm(raw);
    if (film && !films.has(film.sha)) films.set(film.sha, film);
  }
  return [...films.values()].sort((a, b) => (dateValue(b.created) ?? 0) - (dateValue(a.created) ?? 0) || a.id.localeCompare(b.id));
}

async function readFeed(films) {
  const ids = films.filter(film => film.kind === 'character').map(film => film.sha);
  let result = null;
  for (let i = 0; i < Math.max(ids.length, 1); i += 20) {
    const response = await readJson('/sandbox/tiktok/feed?clips=' + ids.slice(i, i + 20).join(','));
    if (typeof response?.connected !== 'boolean') throw Error('invalid feed');
    if (!response.connected) return { connected: false, receipts: {} };
    if (!result) result = { ...response, receipts: {} };
    if (response.csrf !== result.csrf) throw Error('session changed');
    for (const sha of ids.slice(i, i + 20)) {
      const row = response.receipts?.[sha];
      if (row && Object.hasOwn(receiptLabels, row.state)) result.receipts[sha] = row;
    }
  }
  return result;
}

function connection() {
  const link = $('#connection-link'), button = $('#automation-button'), msg = $('#connection-message');
  const connected = feed?.connected === true;
  $('#connection-light').classList.toggle('on', connected);
  $('#account').textContent = connected && typeof feed.display === 'string' ? feed.display : '';
  button.hidden = true;
  link.hidden = false;
  if (!onWorker) {
    link.href = workerOrigin + '/gallery.html';
    link.textContent = '打开沙盒工作台 ↗';
    msg.textContent = '进入 JJ TEST 工作台即可查看账号回执和自动投递设置。';
    return;
  }
  link.href = '/auth/tiktok/start?return=gallery';
  if (!feed) {
    link.hidden = true;
    msg.textContent = '暂时无法读取账号状态，可以刷新重试。';
    return;
  }
  if (!connected) {
    link.textContent = '连接 TikTok 沙盒';
    msg.textContent = '连接测试账号后即可查看 TikTok 回执，并选择是否自动投递新成片。';
    return;
  }
  const enabled = feed.automation?.enabled === true;
  const problem = problemLabels[feed.automation?.problem] || '';
  msg.textContent = (enabled
    ? '自动投递已开启。之后生成的照片串剪会送到此账号的 TikTok 收件箱，可在 TikTok 内完成发布。'
    : '自动投递已关闭。开启后，之后生成的照片串剪会自动送到此账号的 TikTok 收件箱。') + (problem ? ' ' + problem : '');
  const reauthorize = (!enabled && feed.canEnable === false) || feed.automation?.problem === 'reconnect_required';
  link.textContent = '重新授权 TikTok';
  link.hidden = !reauthorize;
  button.hidden = (!enabled && reauthorize) || !feed.csrf;
  button.disabled = changing || loading;
  button.textContent = enabled ? '暂停自动投递' : '开启新成片自动投递';
}

function receiptFor(film) { return feed?.connected ? feed.receipts[film.sha] : null; }

function filmStatus(film) {
  const receipt = receiptFor(film);
  if (receipt) return receiptLabels[receipt.state];
  if (film.kind === 'api_test') return ['测试片 · 仅预览', ''];
  if (!feed?.connected) return ['已生成 · 回执需连接沙盒', ''];
  if (film.sequence && feed.automation?.enabled && dateValue(film.created) > (dateValue(feed.automation.startedAt) ?? Infinity)) {
    return ['等待沙盒投递', 'busy'];
  }
  return ['已生成 · 暂无投递回执', ''];
}

function timelineRow(label, raw) {
  const item = node('div');
  const time = node('dd');
  time.append(when(raw));
  item.append(node('dt', '', label), time);
  return item;
}

function cardFor(film) {
  let saved = cardCache.get(film.sha);
  if (!saved) {
    const card = node('article', 'film'), player = node('div', 'player'), video = node('video');
    video.src = film.video;
    if (film.poster) video.poster = film.poster;
    video.controls = true;
    video.playsInline = true;
    video.preload = 'none';
    let kindLabel = film.kind === 'api_test' ? '接口测试' : '角色成片';
    if (film.sequence) {
      kindLabel = film.ensemble ? '系列角色混剪' : '单角色串剪';
      if (film.photoCount) kindLabel += ' · ' + film.photoCount + ' 张照片';
    }
    const kind = node('span', 'kind', kindLabel);
    player.append(video, kind);
    const body = node('div', 'film-body'), heading = node('div', 'film-heading'), title = node('h3');
    const download = node('a', '', onWorker ? '打开 MP4 ↗' : '下载 MP4');
    if (onWorker) { download.target = '_blank'; download.rel = 'noopener noreferrer'; }
    download.href = film.video;
    if (!onWorker) download.download = film.id + '.mp4';
    heading.append(title, download);
    const state = node('span', 'state'), times = node('dl', 'timestamps');
    const error = node('p', 'play-error', '当前无法播放，可以下载 MP4 查看。');
    error.hidden = true;
    video.addEventListener('error', () => { error.hidden = false; });
    video.addEventListener('play', () => {
      for (const other of $('#gallery-grid').querySelectorAll('video')) if (other !== video) other.pause();
    });
    body.append(heading, state, times, error);
    card.append(player, body);
    saved = { card, video, title, download, state, times };
    cardCache.set(film.sha, saved);
  }
  saved.video.setAttribute('aria-label', film.title);
  saved.title.textContent = film.title;
  saved.download.setAttribute('aria-label', (onWorker ? '打开 ' : '下载 ') + film.title);
  const [label, tone] = filmStatus(film);
  saved.state.textContent = label;
  saved.state.className = 'state ' + tone;
  const receipt = receiptFor(film);
  const times = [timelineRow(film.archivedOnly ? '归档时间' : '生成完成', film.created)];
  if (receipt?.startedAt) times.push(timelineRow('开始投递', receipt.startedAt));
  if (receipt?.acceptedAt) times.push(timelineRow('上传接收确认', receipt.acceptedAt));
  if (receipt?.deliveredAt || receipt?.state === 'delivered') times.push(timelineRow('收件箱送达确认', receipt.deliveredAt));
  if (receipt?.state === 'published') times.push(timelineRow('TikTok 发布确认', receipt.publishedAt));
  if (['failed', 'uncertain', 'rejected'].includes(receipt?.state)) times.push(timelineRow('投递状态更新', receipt.failedAt || receipt.updatedAt));
  saved.times.replaceChildren(...times);
  return saved.card;
}

function renderFilms() {
  const grid = $('#gallery-grid');
  $('#collection-count').textContent = library === null ? '—' : library.length;
  if (!library?.length) {
    grid.replaceChildren(node('p', 'empty', library === null ? '暂时无法读取成片。' : 'Agent 生成的成片将显示在这里。'));
    return;
  }
  const active = new Set(library.map(film => film.sha));
  for (const [sha, saved] of cardCache) {
    if (!active.has(sha)) { saved.card.remove(); cardCache.delete(sha); }
  }
  // Preserve each mounted player across status polls, including its playback position and focus.
  for (const child of [...grid.children]) if (!child.matches('article')) child.remove();
  for (const [index, film] of library.entries()) {
    const card = cardFor(film);
    if (grid.children[index] !== card) grid.insertBefore(card, grid.children[index] || null);
  }
}

function eventItem(label, title, date, tone = '', detail = '') {
  const item = node('li', tone), top = node('div', 'event-top', label);
  top.append(when(date));
  item.append(top, node('p', 'event-title', title));
  if (detail) item.append(node('p', 'event-detail', detail));
  return item;
}

function renderActivity() {
  const events = [];
  for (const film of library || []) {
    events.push({ label: film.archivedOnly ? '成片已归档' : '成片已生成', title: film.title, date: film.created });
    const receipt = receiptFor(film);
    if (receipt) {
      const [stateLabel, tone] = receiptLabels[receipt.state];
      const label = ['delivered', 'published'].includes(receipt.state) ? 'TikTok 确认 · ' + stateLabel : stateLabel;
      const date = { delivered: receipt.deliveredAt, published: receipt.publishedAt, processing: receipt.acceptedAt,
        failed: receipt.failedAt, rejected: receipt.failedAt, reserved: receipt.startedAt, uncertain: receipt.updatedAt }[receipt.state];
      events.push({ label, title: film.title, date, tone });
    }
  }
  events.sort((a, b) => (dateValue(b.date) ?? -Infinity) - (dateValue(a.date) ?? -Infinity));
  const list = node('ol', 'events');
  for (const event of events.slice(0, 8)) list.append(eventItem(event.label, event.title, event.date, event.tone));
  const container = $('#activity-list');
  const expanded = container.querySelector('details')?.open === true;
  container.replaceChildren(list);
  if (!events.length) container.prepend(node('p', 'empty', '暂无成片活动记录。'));
  const tasks = (queue || []).filter(task => task && Object.hasOwn(queueLabels, task.status) && !task.video_path);
  if (tasks.length) {
    const details = node('details'), tasksList = node('ol', 'events');
    details.open = expanded;
    details.append(node('summary', '', '制作队列 · ' + tasks.length));
    for (const task of tasks) {
      tasksList.append(eventItem(queueLabels[task.status], task.title || '未命名任务', task.created_at, '', '时间为任务加入队列的时间'));
    }
    details.append(tasksList);
    container.append(details);
  }
}

async function refresh() {
  if (loading || changing) return;
  loading = true;
  $('#refresh').disabled = true;
  $('#gallery-grid').setAttribute('aria-busy', 'true');
  warnings.clear();
  try {
    const [films, pipeline] = await Promise.allSettled([readManifest('creations'), readManifest('pipeline')]);
    if (films.status === 'fulfilled') {
      library = validFilms(films.value);
      if (films.value.length !== library.length) warnings.add('部分成片信息不完整。');
    } else warnings.add('成片清单暂时无法更新。');
    if (pipeline.status === 'fulfilled') queue = pipeline.value;
    else warnings.add('制作队列暂时无法更新。');
    if (onWorker) {
      try { feed = await readFeed(library || []); }
      catch { feed = null; warnings.add('未能取得当前沙盒的回执。'); }
    }
    renderFilms();
    renderActivity();
  } finally {
    loading = false;
    $('#gallery-grid').setAttribute('aria-busy', 'false');
    $('#refresh').disabled = false;
    $('#feedback').textContent = [...warnings].join(' ');
    connection();
  }
}

async function toggleAutomation() {
  if (!onWorker || !feed?.connected || !feed.csrf || changing || loading) return;
  const enable = !feed.automation?.enabled;
  changing = true;
  $('#automation-button').disabled = true;
  $('#refresh').disabled = true;
  let message = '';
  try {
    await readJson('/sandbox/tiktok/automation', {
      method: 'POST', referrerPolicy: 'same-origin', body: new URLSearchParams({ enabled: enable ? '1' : '0', csrf: feed.csrf })
    });
  } catch { message = '未能确认自动投递设置，请核对账号和最新状态。'; }
  changing = false;
  await refresh();
  if (message) $('#feedback').textContent = [message, ...warnings].join(' ');
}

$('#refresh').addEventListener('click', refresh);
$('#automation-button').addEventListener('click', toggleAutomation);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
window.addEventListener('pagehide', () => { feed = null; });
window.addEventListener('pageshow', event => { if (event.persisted) refresh(); });
setInterval(() => { if (!document.hidden) refresh(); }, 60000);
refresh();
