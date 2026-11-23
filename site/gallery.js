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
const nyDayFormatter = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit'
});
const receiptLabels = {
  reserved: ['等待发送', 'busy'], processing: ['TikTok 处理中', 'busy'],
  delivered: ['已送达 TikTok 收件箱', 'done'], published: ['已在 TikTok 发布', 'done'],
  failed: ['发送失败', 'issue'], rejected: ['TikTok 拒绝了上传', 'issue'], uncertain: ['投递结果待核实', 'issue']
};
const queueLabels = {
  waiting_classification: '等待素材分类', waiting_material: '等待可用角色图', queued: '已排队',
  preparing_motion: '提取动作中', rendering_clay: '渲染动作中', generating_styles: '准备造型中',
  rendering_sequence: '正在剪辑成片', generating_video: '生成影片中', generating_copy: '生成文案中', needs_attention: '制作需要检查',
  plan_ready: '分镜已完成', waiting_trend: '等待达标趋势', waiting_openai: '等待 OpenAI 策划', agent_planning: 'OpenAI 正在选角和写分镜',
  rejected: '已退回制作'
};
const productionMethods = new Set(['agent_motion', 'photo_sequence', 'original_character_edit', 'named_reference_motion']);
const problemLabels = {
  reconnect_required: '请重新授权 TikTok。', catalog_unavailable: '暂时无法读取成片库。',
  media_unavailable: '暂时无法读取视频文件。', delivery_uncertain: '有一条投递需要核实。',
  quota_limited: '已达到 TikTok 上传限额。', provider_rejected: 'TikTok 拒绝了上传。',
  temporary_failure: '沙盒暂时不可用。'
};

let library = null;
let queue = null;
let feed = null;
let namedSchedule = null;
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
  const response = await fetch(path, {
    cache: 'no-store', credentials: 'same-origin', redirect: 'error',
    signal: AbortSignal.timeout(20000), ...options
  });
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

function trimmed(value, max = 2000) {
  return typeof value === 'string' ? value.trim().slice(0, max) : '';
}

function tiktokSource(raw) {
  const match = /^https:\/\/(?:www\.)?tiktok\.com\/@([A-Za-z0-9._-]{1,100})\/video\/(\d{10,25})\/?$/.exec(trimmed(raw, 400));
  return match ? { url: 'https://www.tiktok.com/@' + match[1] + '/video/' + match[2], creator: match[1].toLowerCase() } : null;
}

function sourceLink(label, url) {
  const anchor = node('a', 'source-link', label);
  anchor.href = url;
  anchor.target = '_blank';
  anchor.rel = 'noopener noreferrer';
  return anchor;
}

function filmNotes(record, task) {
  const approvedTrend = record.generation_method === 'agent_motion' && record.sequence_type === 'trend_remake' && record.planner === 'openai';
  const selected = record.generation_method === 'named_reference_motion' && record.selection_basis === 'user_selected_reference';
  const source = tiktokSource(record.source_url) || tiktokSource(task?.source_url);
  const caption = trimmed(record.caption || task?.caption, 2200);
  const tags = (Array.isArray(record.hashtags) ? record.hashtags : []).filter(t => typeof t === 'string' && /^#[\p{L}\p{N}_]{1,80}$/u.test(t));
  const publication = trimmed(record.publication_text || task?.publication_text, 3200) || [caption, tags.join(' ')].filter(Boolean).join('\n\n');
  const peers = new Map();
  for (const row of approvedTrend && Array.isArray(record.source_peers) ? record.source_peers : []) {
    const peer = tiktokSource(typeof row === 'string' ? row : row?.url);
    if (peer && peer.creator !== source?.creator && !peers.has(peer.creator)) peers.set(peer.creator, peer);
  }
  return {
    hook: trimmed(record.hook, 400), caption, publication, tags, source,
    sourceTitle: trimmed(record.source_title, 180), group: trimmed(record.material_group || task?.material_group, 12),
    trend: approvedTrend, selected, views: record.source_metrics?.views, likes: record.source_metrics?.likes,
    viewsDisplay: trimmed(record.source_metric_display?.views, 40), likesDisplay: trimmed(record.source_metric_display?.likes, 40),
    peerCount: record.peer_count, peers: [...peers.values()], observed: record.source_observed_at
  };
}

function updateNotes(saved, notes) {
  const signature = JSON.stringify(notes);
  if (saved.notesSignature === signature) return;
  saved.notesSignature = signature;
  const area = saved.notes;
  const wasOpen = area.querySelector('details')?.open === true;
  area.replaceChildren();
  if (notes.hook) {
    const hook = node('p', 'film-hook', notes.hook);
    hook.setAttribute('aria-label', '开场钩子');
    area.append(hook);
  }
  const group = { male: '男生', female: '女生', mixed: '混合' }[notes.group];
  if (!notes.publication && !notes.source && !group) return;
  const details = node('details', 'film-notes');
  details.open = wasOpen;
  details.append(node('summary', '', notes.source ? '发布文案与原片' : '发布信息'));
  if (notes.publication) {
    const header = node('div', 'copy-heading');
    header.append(node('strong', '', 'TikTok 文案'));
    const copy = node('button', 'copy-button', '复制文案');
    copy.type = 'button';
    copy.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(notes.publication);
        copy.textContent = '已复制';
      } catch { copy.textContent = '请选中文案复制'; }
    });
    header.append(copy);
    details.append(header, node('p', 'publication', notes.publication));
  }
  if (group) details.append(node('p', 'film-credit', '所选素材 · ' + group));
  if (notes.source) {
    const line = node('p', 'film-credit');
    line.append(sourceLink((notes.selected ? '指定原片 ↗ ' : '趋势原片 ↗ ') + (notes.sourceTitle || '@' + notes.source.creator), notes.source.url));
    details.append(line);
  }
  if (notes.trend && notes.source) {
    const numbers = [];
    const count = n => Number.isSafeInteger(n) && n >= 0;
    if (count(notes.views)) numbers.push((notes.viewsDisplay || notes.views.toLocaleString('en-US')) + ' 播放');
    if (count(notes.likes)) numbers.push((notes.likesDisplay || notes.likes.toLocaleString('en-US')) + ' 赞');
    if (count(notes.peerCount)) numbers.push(notes.peerCount + ' 位其他创作者跟拍');
    if (numbers.length) details.append(node('p', 'film-credit', numbers.join(' · ')));
    if (dateValue(notes.observed) !== null) {
      const observed = node('p', 'film-credit', '原片核验于 ');
      observed.append(when(notes.observed));
      details.append(observed);
    }
    if (notes.peers.length) {
      const links = node('p', 'peer-links', '同玩法实例 ');
      for (const peer of notes.peers) links.append(sourceLink('@' + peer.creator + ' ↗', peer.url));
      details.append(links);
    }
  }
  area.append(details);
}

const acknowledgementKey = 'idore.work.verification.acknowledged';
let activeChallenge = null;
let offeredChallenge = '';
let acknowledgedChallenge = '';
try { acknowledgedChallenge = localStorage.getItem(acknowledgementKey) || ''; } catch { /* Private browsing can disable storage. */ }

function currentChallenge(discovery) {
  if (!Array.isArray(discovery?.runs) || !discovery.work_imports) return null;
  let latest = null;
  let latestTime = -Infinity;
  for (const run of discovery.runs) {
    if (run?.collector !== 'chatgpt_work' || run.ingestion_status !== 'imported' ||
        typeof run.run_id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$/.test(run.run_id) ||
        !shaPattern.test(run.payload_sha256) || discovery.work_imports[run.run_id] !== run.payload_sha256) continue;
    const time = dateValue(run.observed_at) ?? dateValue(run.finished_at);
    if (time !== null && time > latestTime) { latest = run; latestTime = time; }
  }
  if (!latest || latest.status !== 'blocked') return null;
  const legacySlider = latest.run_id === 'work-tiktok-20261120T140259Z-v2' &&
    latest.payload_sha256 === 'b222d7b6ce8a49aeb3b5f0487f83384a99caa7fe31515ffd73accc269ccad5a8';
  if (latest.action_required !== 'human_verification' && !legacySlider) return null;
  return { id: latest.run_id, observed: latest.observed_at || latest.finished_at };
}

function openChallenge() {
  const modal = $('#verification-dialog');
  if (!activeChallenge || modal.open || typeof modal.showModal !== 'function') return;
  offeredChallenge = activeChallenge.id;
  modal.showModal();
  $('#verification-confirm').focus();
}

function renderChallenge(challenge) {
  const banner = $('#verification-status'), modal = $('#verification-dialog');
  const oldId = activeChallenge?.id;
  activeChallenge = challenge;
  banner.hidden = !challenge;
  if (!challenge) {
    if (modal.open) modal.close();
    return;
  }
  const time = $('#verification-observed');
  time.replaceChildren(when(challenge.observed));
  if (oldId !== challenge.id && modal.open) modal.close();
  if (challenge.id !== acknowledgedChallenge && offeredChallenge !== challenge.id) openChallenge();
}

function acknowledgeChallenge() {
  if (!activeChallenge) return;
  acknowledgedChallenge = activeChallenge.id;
  try { localStorage.setItem(acknowledgementKey, activeChallenge.id); } catch { /* Keep in memory for this visit. */ }
  $('#verification-dialog').close();
}

function normalizeFilm(raw) {
  if (!raw || !shaPattern.test(raw.sha256) || !['character', 'api_test'].includes(raw.kind)) return null;
  const id = 'video-' + raw.sha256.slice(0, 24);
  if (raw.id !== id || raw.video_path !== 'generated/' + id + '.mp4') return null;
  return {
    record: raw, id, sha: raw.sha256, kind: raw.kind, video: new URL(raw.video_path, assetBase).href,
    poster: raw.poster_path === 'generated/' + id + '.jpg' ? new URL(raw.poster_path, assetBase).href : '',
    title: typeof raw.title === 'string' && raw.title.trim() ? raw.title.trim().slice(0, 180) : '未命名成片',
    created: dateValue(raw.generated_at) !== null ? raw.generated_at : raw.created_at,
    archivedOnly: raw.timestamp_basis === 'workflow_artifact', sequence: raw.generation_method === 'photo_sequence',
    originalEdit: raw.generation_method === 'original_character_edit',
    namedRemake: raw.generation_method === 'named_reference_motion' && raw.selection_basis === 'user_selected_reference',
    ensemble: raw.sequence_type === 'ip_cast',
    trendRemake: raw.generation_method === 'agent_motion' && raw.planner === 'openai' && raw.sequence_type === 'trend_remake',
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
    link.textContent = '进入沙盒 ↗';
    msg.textContent = '进入 JJ TEST，查看账号回执与自动投递。';
    return;
  }
  link.href = '/auth/tiktok/start?return=gallery';
  if (!feed) {
    link.hidden = true;
    msg.textContent = '暂时无法读取账号状态。';
    return;
  }
  if (!connected) {
    link.textContent = '连接 TikTok 沙盒';
    msg.textContent = '连接测试账号，查看回执或开启自动投递。';
    return;
  }
  const enabled = feed.automation?.enabled === true;
  const previousGrantFailed = feed.automation?.problem === 'reconnect_required' && feed.canEnable === true;
  const problem = previousGrantFailed ? '' : problemLabels[feed.automation?.problem] || '';
  msg.textContent = (enabled
    ? '自动投递已开启 · 符合条件的新成片将送至此账号收件箱。'
    : '开启后，符合条件的新成片将送至此账号的 TikTok 收件箱。') + (problem ? ' ' + problem : '');
  const reauthorize = !enabled && feed.canEnable === false;
  link.textContent = '重新授权 TikTok';
  link.hidden = !reauthorize;
  button.hidden = (!enabled && reauthorize) || !feed.csrf;
  button.disabled = changing || loading;
  button.textContent = enabled ? '暂停自动投递' : '开启自动投递';
}

function receiptFor(film) { return feed?.connected ? feed.receipts[film.sha] : null; }

function filmStatus(film) {
  const receipt = receiptFor(film);
  if (receipt) return receiptLabels[receipt.state];
  if (film.kind === 'api_test') return ['测试片 · 仅预览', ''];
  if (!feed?.connected) return ['已生成 · 回执需连接沙盒', ''];
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
    if (film.sequence || film.trendRemake || film.originalEdit || film.namedRemake) {
      kindLabel = film.namedRemake ? '指定原片复刻' : film.originalEdit ? '原创角色短片' : film.trendRemake ? '趋势改编' : film.ensemble ? '系列角色混剪' : '单角色串剪';
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
    const notes = node('div', 'notes');
    body.append(heading, state, times, notes, error);
    card.append(player, body);
    saved = { card, video, title, download, state, times, notes };
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
  const task = (queue || []).find(row => row?.sha256 === film.sha);
  updateNotes(saved, filmNotes(film.record, task));
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

function validNamedSchedule(raw) {
  const day = raw?.date;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day) || !Number.isInteger(raw.start_hour) || raw.start_hour < 0 ||
      raw.start_hour > 23 || !Array.isArray(raw.references) || !raw.references.length) throw Error('invalid named schedule');
  const slots = new Set(), sources = new Set();
  for (const ref of raw.references) {
    const source = tiktokSource(ref?.url);
    if (!source || ref.source_id !== 'tiktok-' + source.url.split('/').pop() || sources.has(ref.source_id) ||
        typeof ref.slot !== 'string' || !ref.slot.startsWith(day + '-') || slots.has(ref.slot) || !trimmed(ref.title, 160)) {
      throw Error('invalid scheduled reference');
    }
    slots.add(ref.slot);
    sources.add(ref.source_id);
  }
  return raw;
}

function matchesNamed(record, ref) {
  return record?.generation_method === 'named_reference_motion' && record.selection_basis === 'user_selected_reference' &&
    record.sequence_slot === ref.slot && record.trend_id === ref.source_id;
}

function dailyOutput(films, accountFeed, now = Date.now(), schedule = namedSchedule) {
  if (!films) return { generated: null, delivered: null };
  const today = nyDayFormatter.format(now);
  const completed = films.filter(film => {
    const time = dateValue(film.created);
    return film.kind === 'character' && !film.archivedOnly && time !== null && nyDayFormatter.format(time) === today;
  });
  const named = schedule?.references || [];
  const groups = schedule?.date === today
    ? named.map(ref => completed.filter(film => matchesNamed(film.record, ref))).filter(group => group.length)
    : completed.filter(film => ['agent_motion', 'original_character_edit'].includes(film.record.generation_method) ||
        named.some(ref => matchesNamed(film.record, ref))).map(film => [film]);
  const delivered = accountFeed?.connected
    ? groups.filter(group => group.some(film => ['delivered', 'published'].includes(accountFeed.receipts?.[film.sha]?.state))).length
    : null;
  return { generated: groups.length, delivered };
}

function namedStatus(ref, films, tasks, accountFeed) {
  const finished = (films || []).filter(film => matchesNamed(film.record, ref));
  if (finished.length) {
    const states = finished.map(film => accountFeed?.receipts?.[film.sha]?.state);
    const state = ['published', 'delivered', 'processing', 'reserved', 'uncertain', 'failed', 'rejected'].find(key => states.includes(key));
    return state ? receiptLabels[state][0] : '已生成 · 暂无投递回执';
  }
  const task = tasks?.find(row => matchesNamed(row, ref));
  if (!task) return tasks && films ? '已排期' : '已排期 · 状态待更新';
  if (task.status === 'generated') return '成片清单待同步';
  return trimmed(task.message, 120) || queueLabels[task.status] || '已排期';
}

function renderNamedSchedule(today) {
  const schedule = namedSchedule;
  $('#daily-target').textContent = schedule ? '目标 ' + (schedule.date === today ? schedule.references.length : 3) + ' 条' : '目标待同步';
  const section = $('#named-plan');
  section.hidden = !schedule || schedule.date < today;
  if (section.hidden) return;
  $('#named-heading').textContent = schedule.date.slice(5).replace('-', '/') + ' 指定原片复刻';
  $('#named-start').textContent = String(schedule.start_hour).padStart(2, '0') + ':00 起 · 纽约';
  const items = schedule.references.map(ref => {
    const item = node('li');
    item.append(sourceLink(ref.title + ' ↗', ref.url), node('p', 'event-detail', namedStatus(ref, library, queue, feed)));
    return item;
  });
  $('#named-references').replaceChildren(...items);
}

function referenceBlocker(task) {
  if (task.status === 'waiting_classification') {
    const missing = [];
    if (!['male', 'female', 'mixed'].includes(task.material_group)) missing.push('素材分组');
    if (!['solo', 'group'].includes(task.cast_type)) missing.push('人数');
    if (missing.length) return '待核实' + missing.join('和');
    if (task.transformation === 'unknown') return '待核实换装或妆容变化';
    if (task.original_watched === false) return '待播放核实原片';
  }
  return trimmed(task.message, 180) || queueLabels[task.status];
}

function renderActivity() {
  const now = Date.now();
  renderNamedSchedule(nyDayFormatter.format(now));
  const progress = dailyOutput(library, feed, now);
  $('#daily-generated').textContent = progress.generated ?? '—';
  $('#daily-delivered').textContent = progress.delivered ?? '—';
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
  for (const task of queue || []) {
    if (!productionMethods.has(task?.generation_method) || task.video_path || !Object.hasOwn(queueLabels, task.status)) continue;
    const quota = task.error_code === 'insufficient_quota' || task.error_code === 'insufficient_credits';
    const label = quota ? 'OpenAI 额度不足' : queueLabels[task.status];
    events.push({ label, title: task.title || '趋势改编', detail: trimmed(task.message, 180),
      date: task.updated_at || task.created_at, tone: task.status === 'needs_attention' || quota ? 'issue' : 'busy' });
  }
  events.sort((a, b) => (dateValue(b.date) ?? -Infinity) - (dateValue(a.date) ?? -Infinity));
  const list = node('ol', 'events');
  for (const event of events.slice(0, 8)) list.append(eventItem(event.label, event.title, event.date, event.tone, event.detail));
  const container = $('#activity-list');
  const expanded = container.querySelector('details')?.open === true;
  container.replaceChildren(list);
  if (!events.length) container.prepend(node('p', 'empty', '暂无成片活动记录。'));
  const tasks = (queue || []).filter(task => task && !productionMethods.has(task.generation_method) && Object.hasOwn(queueLabels, task.status) && !task.video_path);
  if (tasks.length) {
    const details = node('details'), tasksList = node('ol', 'events');
    details.open = expanded;
    details.append(node('summary', '', '参考素材 · ' + tasks.length), node('p', 'event-detail', '按素材入队时间排序'));
    tasks.sort((a, b) => (dateValue(b.created_at) ?? -Infinity) - (dateValue(a.created_at) ?? -Infinity));
    for (const task of tasks) {
      const item = eventItem(referenceBlocker(task), task.title || '未命名素材', task.created_at);
      const source = tiktokSource(task.source_url);
      if (source) {
        const detail = node('p', 'event-detail');
        detail.append(sourceLink('查看原片 ↗', source.url));
        item.append(detail);
      }
      tasksList.append(item);
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
    const [films, pipeline, discovery, schedule] = await Promise.allSettled([
      readManifest('creations'), readManifest('pipeline'),
      readJson(new URL('data/discovery.json', assetBase), { credentials: 'omit' }),
      readJson(new URL('data/named-motion.json', assetBase), { credentials: 'omit' }).then(validNamedSchedule)
    ]);
    if (schedule.status === 'fulfilled') namedSchedule = schedule.value;
    else warnings.add('指定复刻日程暂时无法更新。');
    if (discovery.status === 'fulfilled' && Array.isArray(discovery.value?.runs)) {
      renderChallenge(currentChallenge(discovery.value));
    }
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

$('#verification-open').addEventListener('click', openChallenge);
$('#verification-confirm').addEventListener('click', acknowledgeChallenge);
$('#verification-dialog').addEventListener('close', () => {
  if (activeChallenge) $('#verification-open').focus();
});
$('#refresh').addEventListener('click', refresh);
$('#automation-button').addEventListener('click', toggleAutomation);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
window.addEventListener('pagehide', () => { feed = null; });
window.addEventListener('pageshow', event => { if (event.persisted) refresh(); });
setInterval(() => { if (!document.hidden) refresh(); }, 60000);
refresh();
