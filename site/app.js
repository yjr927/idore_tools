'use strict';

const $ = selector => document.querySelector(selector);
const statuses = {
  not_run: ['等待首次采集', '尚未运行'],
  running: ['正在采集', '进行中'],
  succeeded: ['采集已完成', '取得候选'],
  partial: ['部分来源取得结果', '部分可用'],
  empty: ['本次没有返回候选', '暂无结果'],
  blocked: ['TikTok 页面暂不可读取', '访问受阻'],
  failed: ['本次采集未完成', '采集失败'],
  interrupted: ['本次采集中断', '已中断']
};
const kindLabels = { hands: '手势 / 舞蹈', transition: '变装 / 妆容', pov: '表情 / POV' };
const favoriteKey = 'idore-tiktok-discovery-favorites-v1';
let data = { videos: [], runs: [] };
let category = 'all';
let search = '';
let onlyFavorites = false;
let historyDate = '';
let toastTimer;
let favorites = new Set();
try {
  const stored = JSON.parse(localStorage.getItem(favoriteKey) || '[]');
  if (Array.isArray(stored)) favorites = new Set(stored.filter(value => typeof value === 'string'));
} catch { /* Favorites remain available for this visit if storage is unavailable. */ }

function el(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined) node.textContent = String(content);
  return node;
}
const collectionTimeZone = 'America/New_York';
const collectionDateFormat = new Intl.DateTimeFormat('en-CA', {
  timeZone: collectionTimeZone, year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
});
function dateParts(value) {
  if (!value) return null;
  const time = new Date(value);
  if (!Number.isFinite(time.getTime())) return null;
  return Object.fromEntries(collectionDateFormat.formatToParts(time).map(part => [part.type, part.value]));
}
function collectionDay(value) {
  const parts = dateParts(value);
  return parts ? `${parts.year}-${parts.month}-${parts.day}` : '';
}
function formatDate(value) {
  const parts = dateParts(value);
  return parts ? `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}` : '—';
}
function runMoment(run) { return run.observed_at || run.started_at || run.finished_at; }
function orderedRuns(runs) {
  return (Array.isArray(runs) ? runs : []).filter(run => run && typeof run === 'object').sort((a, b) => {
    const time = run => { const value = new Date(runMoment(run)).getTime(); return Number.isFinite(value) ? value : -Infinity; };
    return time(b) - time(a);
  }).slice(0, 90);
}
function metric(value) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return '—';
  return new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 1 }).format(value);
}
function videoUrl(value) {
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:' || !['www.tiktok.com', 'tiktok.com'].includes(url.hostname) || url.username || url.password || (url.port && url.port !== '443')) return null;
    const match = url.pathname.match(/^\/@([a-zA-Z0-9_.-]{1,100})\/video\/(\d{10,25})\/?$/);
    if (!match) return null;
    return { url: `https://www.tiktok.com/@${match[1]}/video/${match[2]}`, author: match[1], videoId: match[2] };
  } catch { return null; }
}
function sourceUrl(value) {
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:' || !['www.tiktok.com', 'tiktok.com'].includes(url.hostname) || url.username || url.password || (url.port && url.port !== '443')) return null;
    return url.href;
  } catch { return null; }
}
function externalLink(label, url, className) {
  const link = el('a', className, label);
  link.href = url;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  return link;
}
function toast(message) {
  clearTimeout(toastTimer);
  $('#toast').textContent = message;
  $('#toast').hidden = false;
  toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 4000);
}
function candidateId(video) { return videoUrl(video.url)?.videoId || video.id; }
function toggleFavorite(video) {
  const id = candidateId(video);
  if (favorites.has(id)) favorites.delete(id); else favorites.add(id);
  try { localStorage.setItem(favoriteKey, JSON.stringify([...favorites])); } catch { toast('收藏保留在本次访问中；当前浏览器不允许写入本地存储。'); }
  renderVideos();
}
function openPreview(video) {
  const canonical = videoUrl(video.url);
  if (!canonical) return;
  $('#preview-title').textContent = `查看原片 · @${canonical.author}`;
  $('#preview-original').href = canonical.url;
  const frame = el('iframe');
  frame.src = `https://www.tiktok.com/player/v1/${canonical.videoId}`;
  frame.title = `TikTok 原视频：${video.title || canonical.author}`;
  frame.allow = 'fullscreen';
  frame.referrerPolicy = 'no-referrer';
  $('#preview-container').replaceChildren(frame);
  $('#preview-dialog').showModal();
}
function workObservation(video) {
  return video.source_kind === 'chatgpt_work' && video.original_watched === true;
}
function observationCheck(label, value, watched) {
  const state = watched && typeof value === 'boolean' ? value : null;
  return el('span', state === true ? 'check-confirmed' : state === false ? 'check-unmatched' : '',
    `${label} · ${state === true ? '已确认' : state === false ? '不符合' : '待核验'}`);
}
function relatedExamples(video) {
  const examples = Array.isArray(video.related_videos) ? video.related_videos : [];
  const valid = examples.filter(item => item && videoUrl(item.url));
  if (!valid.length) return null;
  const details = el('details', 'related-examples');
  details.append(el('summary', '', `跟拍线索 · ${valid.length} 条`));
  for (const item of valid.slice(0, 8)) {
    const canonical = videoUrl(item.url);
    const row = el('div', 'related-example');
    row.append(externalLink(`@${canonical.author} · 打开原片 ↗`, canonical.url));
    const watched = video.source_kind === 'chatgpt_work' && item.original_watched === true;
    const comparable = workObservation(video) && watched;
    const consistency = comparable && item.same_pattern === true ? '已观察到同一玩法' : comparable && item.same_pattern === false ? '已观察到不同玩法' : '玩法一致性待核验';
    row.append(el('p', '', `${watched ? '原片已观看' : '原片待观看'} · ${consistency}`));
    if (typeof item.note === 'string' && item.note.trim()) row.append(el('p', '', item.note));
    details.append(row);
  }
  return details;
}
function historySnapshot(video, run) {
  const canonical = videoUrl(video.url);
  const snapshot = el('article', 'history-video');
  const heading = el('div', 'history-video-heading');
  heading.append(el('h4', '', video.title || `@${canonical.author} 的原视频候选`));
  heading.append(externalLink('打开当次原视频 ↗', canonical.url));
  snapshot.append(heading, el('p', 'history-video-meta', `@${canonical.author} · ${kindLabels[video.kind] || '玩法待分类'} · ${workObservation(video) ? 'Work 已观看原片' : '原片待核验'}`));
  snapshot.append(el('p', 'history-motion', video.motion_summary || '该次未保存动作描述；不能据此判断单人、动作重复性或复刻难度。'));
  const checks = el('div', 'checks');
  checks.append(observationCheck('单人', video.checks?.solo, workObservation(video)), observationCheck('重复动作', video.checks?.repetitive, workObservation(video)), observationCheck('固定机位', video.checks?.fixed_camera, workObservation(video)), el('span', '', 'AI 复刻 · 未试做'));
  snapshot.append(checks);
  const observed = video.observed_at || runMoment(run);
  const values = [['views', '播放'], ['likes', '点赞'], ['comments', '评论'], ['shares', '分享']].map(([key, label]) => `${label} ${metric(video.metrics?.[key])}`).join(' · ');
  snapshot.append(el('p', 'history-video-meta', `当次页面指标：${values}。观察时间 ${formatDate(observed)}（纽约）；未获取项为“—”。`));
  const published = video.published_at ? formatDate(video.published_at) : video.published_text ? `${video.published_text}（当时页面显示）` : '未核验';
  snapshot.append(el('p', 'history-video-meta', `原视频发布：${published}`));
  if (typeof video.recreation_notes === 'string' && video.recreation_notes.trim()) snapshot.append(el('p', 'history-motion', `复刻建议（未试做）：${video.recreation_notes}`));
  const related = relatedExamples(video);
  if (related) snapshot.append(related);
  const source = sourceUrl(video.source_url);
  const label = `来源：${video.source || (video.source_kind === 'chatgpt_work' ? 'ChatGPT Work 原片观察' : 'TikTok 公开页面')}`;
  snapshot.append(source ? externalLink(`${label} ↗`, source, 'history-video-source') : el('p', 'history-video-meta', label));
  return snapshot;
}
function historyRun(run) {
  const row = el('details', 'history-run');
  const summary = el('summary');
  const date = el('time', 'history-run-time', formatDate(runMoment(run)));
  if (typeof runMoment(run) === 'string') date.dateTime = runMoment(run);
  const status = Object.hasOwn(statuses, run.status) ? run.status : 'unknown';
  const counts = value => Number.isInteger(value) && value >= 0 ? value : '—';
  summary.append(date,
    el('span', `state-pill state-${status}`, statuses[status]?.[1] || '状态未记录'),
    el('span', 'history-run-counts', `观察 ${counts(run.found)} 条 · 新增 ${counts(run.added)} 条`),
    el('span', 'history-run-source', run.collector === 'chatgpt_work' || run.source_kind === 'chatgpt_work' ? 'ChatGPT Work' : '公开页面采集'));
  row.append(summary);
  const body = el('div', 'history-run-body');
  if (typeof run.message === 'string' && run.message.trim()) body.append(el('p', 'history-run-message', run.message));
  const snapshots = Array.isArray(run.video_snapshots) ? run.video_snapshots.filter(video => video && videoUrl(video.url)).slice(0, 8) : [];
  if (snapshots.length) {
    body.append(el('p', 'history-evidence-note', `本批保存 ${snapshots.length} 条视频的当时记录；指标按页面显示，AI 复刻建议尚未试做。`));
    body.append(...snapshots.map(video => historySnapshot(video, run)));
  } else {
    const note = Array.isArray(run.video_snapshots) ? '该次没有保存可展示的视频快照。失败或空结果不会补造视频信息。' : '这条较早的记录未保存当批视频快照，历史视频详情不可用；只能查看当时保存的来源。';
    body.append(el('p', 'history-evidence-note', note));
    const sources = Array.isArray(run.sources) ? run.sources : [];
    for (const source of sources) {
      if (!source || typeof source !== 'object') continue;
      const sourceRow = el('p', 'history-source-row');
      const link = sourceUrl(source.url);
      sourceRow.append(link ? externalLink(`${source.name || 'TikTok 来源'} ↗`, link) : el('span', '', source.name || '来源名称未记录'));
      if (link) sourceRow.append(el('span', '', ` · ${link}`));
      else if (typeof source.url === 'string' && source.url.trim()) sourceRow.append(el('span', '', ` · 来源链接（未校验）：${source.url.slice(0, 500)}`));
      if (typeof source.message === 'string' && source.message.trim()) sourceRow.append(el('span', '', ` · ${source.message}`));
      body.append(sourceRow);
    }
    if (!sources.length) body.append(el('p', 'history-video-meta', '此记录未附来源链接。'));
  }
  row.append(body);
  return row;
}
function renderHistory() {
  const saved = orderedRuns(data.runs);
  const filtered = saved.filter(run => !historyDate || collectionDay(runMoment(run)) === historyDate);
  $('#history-count').textContent = `已保存 ${saved.length} 次${historyDate ? ` · 当天 ${filtered.length} 次` : ''}`;
  const list = $('#history-list');
  if (!filtered.length) {
    const empty = el('div', 'history-empty');
    empty.append(el('p', '', historyDate ? '所选纽约日期没有已收录的采集记录。' : '尚未收到采集记录。完成采集并更新数据后，这里才会显示真实历史。'));
    list.replaceChildren(empty);
    return;
  }
  const groups = [];
  let day = null;
  let section;
  for (const run of filtered) {
    const nextDay = collectionDay(runMoment(run)) || '日期未记录';
    if (nextDay !== day) {
      day = nextDay;
      section = el('section', 'history-day');
      section.append(el('h3', '', day));
      groups.push(section);
    }
    section.append(historyRun(run));
  }
  list.replaceChildren(...groups);
}
function videoCard(video) {
  const canonical = videoUrl(video.url);
  const watched = workObservation(video);
  const card = el('article', 'video-card');
  const top = el('div', 'card-top');
  top.append(el('span', `candidate-pill${watched ? ' watched-pill' : ''}`, watched ? 'Work 已观看原片' : '原视频候选 · 待看原片'));
  const saved = favorites.has(candidateId(video));
  const favorite = el('button', `favorite-button${saved ? ' selected' : ''}`, saved ? '★ 已收藏' : '☆ 收藏');
  favorite.type = 'button';
  favorite.setAttribute('aria-pressed', String(saved));
  favorite.addEventListener('click', () => toggleFavorite(video));
  top.append(favorite);
  card.append(top);
  const visual = el('div', 'card-visual');
  const play = el('button', 'play-button', '▶');
  play.type = 'button';
  play.setAttribute('aria-label', `尝试播放 ${video.title || canonical.author}`);
  play.addEventListener('click', () => openPreview(video));
  visual.append(el('span', 'video-platform', 'TikTok'), play, el('span', 'video-preview-note', '原片播放需 TikTok 页面可用'));
  card.append(visual);
  const body = el('div', 'card-body');
  body.append(el('p', 'card-author', `@${canonical.author} · ${kindLabels[video.kind] || '玩法待分类'}`));
  body.append(el('h3', '', video.title || 'TikTok 公开视频候选'));
  if (!watched || video.assessment_note !== video.motion_summary) body.append(el('p', 'assessment', video.assessment_note || (watched ? 'Work 已记录原片观察。观看记录不代表当前热门，也不代表 AI 复刻效果已经试做。' : '已记录公开视频链接。单人、动作重复性与跟拍一致性仍待看原片核验。')));
  if (typeof video.motion_summary === 'string' && video.motion_summary.trim()) {
    const summary = el('div', 'observation-notes');
    summary.append(el('strong', '', watched ? '原片动作观察' : '动作记录 · 待原片核验'), el('p', '', video.motion_summary));
    body.append(summary);
  }
  const checks = el('div', 'checks');
  checks.append(
    observationCheck('单人', video.checks?.solo, watched),
    observationCheck('重复动作', video.checks?.repetitive, watched),
    observationCheck('固定机位', video.checks?.fixed_camera, watched),
    el('span', watched && video.trend_verified === true ? 'check-confirmed' : '', watched && video.trend_verified === true ? '跟拍玩法 · 有原片依据' : '跟拍玩法 · 待核验'),
    el('span', '', 'AI 复刻 · 未试做')
  );
  body.append(checks);
  if (watched && video.trend_verified === true) body.append(el('p', 'assessment', '已有同玩法跟拍的原片观察依据；这不代表已确认当前热度或增长。'));
  if (typeof video.recreation_notes === 'string' && video.recreation_notes.trim()) {
    const notes = el('div', 'observation-notes');
    notes.append(el('strong', '', '复刻建议 · 尚未试做'), el('p', '', video.recreation_notes));
    body.append(notes);
  }
  const related = relatedExamples(video);
  if (related) body.append(related);
  const metrics = el('div', 'metrics');
  for (const [key, label] of [['views', '播放'], ['likes', '点赞'], ['comments', '评论']]) {
    const cell = el('div'); cell.append(el('strong', '', metric(video.metrics?.[key])), el('span', '', label)); metrics.append(cell);
  }
  body.append(metrics);
  const details = el('div', 'card-details');
  const source = sourceUrl(video.source_url);
  const sourceLabel = video.source || (video.source_kind === 'chatgpt_work' ? 'ChatGPT Work · TikTok 原片记录' : 'TikTok 公开页面');
  details.append(source ? externalLink(`来源：${sourceLabel} ↗`, source) : el('span', '', `来源：${sourceLabel}`));
  details.append(el('span', '', `首次发现 ${formatDate(video.first_seen_at)} · 最近观测 ${formatDate(video.last_seen_at)}`));
  details.append(el('span', '', `原视频发布 ${video.published_at ? formatDate(video.published_at) : video.published_text || '未核验'}${!video.published_at && video.published_text ? '（页面当时显示）' : ''}`));
  body.append(details);
  const actions = el('div', 'card-actions');
  const preview = el('button', 'button primary small', '▶ 查看原片'); preview.type = 'button'; preview.addEventListener('click', () => openPreview(video));
  actions.append(preview, externalLink('TikTok 打开 ↗', canonical.url, 'button small'));
  body.append(actions); card.append(body);
  return card;
}
function renderVideos() {
  const valid = data.videos.filter(video => video && videoUrl(video.url));
  const list = valid.filter(video => (category === 'all' || video.kind === category || (Array.isArray(video.kinds) && video.kinds.includes(category))) && (!onlyFavorites || favorites.has(candidateId(video))) && `${video.title || ''} ${video.author || ''} ${video.kind || ''} ${video.motion_summary || ''} ${video.recreation_notes || ''}`.toLowerCase().includes(search.toLowerCase()));
  $('#nav-count').textContent = String(valid.length);
  $('#result-count').textContent = `${list.length} 条`;
  const grid = $('#video-grid');
  if (list.length) { grid.replaceChildren(...list.map(videoCard)); return; }
  const empty = el('div', 'empty-state');
  empty.append(el('span', 'empty-icon', onlyFavorites ? '☆' : '⌕'));
  empty.append(el('h3', '', onlyFavorites ? '还没有收藏的候选' : valid.length ? '没有符合当前筛选的候选' : '等待第一批原视频候选'));
  empty.append(el('p', '', onlyFavorites ? '收藏喜欢的视频，方便回来继续比较。收藏只保存在当前浏览器。' : valid.length ? '试试其他玩法分类或搜索词。' : '公开页面没有返回结果时，我们会保留真实状态，不会使用示例视频填充。可在上方查看采集记录。'));
  grid.replaceChildren(empty);
}
function renderStatus() {
  const runs = orderedRuns(data.runs);
  const latest = runs[0];
  const fromWork = data.collector === 'chatgpt_work' || latest?.source_kind === 'chatgpt_work' || latest?.collector === 'chatgpt_work';
  const state = latest?.status || data.status || 'not_run';
  const [title, pill] = statuses[state] || ['采集状态待确认', '状态未知'];
  $('#status-heading').textContent = fromWork ? ({ succeeded: 'Work 最近采集取得候选', partial: 'Work 最近采集取得部分候选', empty: 'Work 本次未提交候选', failed: 'Work 本次未完成', blocked: 'Work 访问遇到限制', not_run: '等待 Work 结果', running: 'Work 结果记录中', interrupted: 'Work 本次中断' }[state] || 'Work 结果状态待确认') : title;
  $('#status-pill').textContent = fromWork && ['succeeded', 'partial'].includes(state) ? 'Work 记录' : pill;
  $('#status-pill').className = `state-pill state-${Object.hasOwn(statuses, state) ? state : 'unknown'}`;
  const defaults = {
    not_run: '尚未取得云端采集记录。需要先运行 GitHub Actions 中的采集任务。',
    blocked: 'TikTok 公开页面没有提供可读取的视频列表。可能需要登录、验证或稍后重试；这不代表 TikTok 没有新趋势。',
    empty: '本次页面读取没有找到候选，不能据此判断没有新趋势。已有数据会保留。'
  };
  $('#status-message').textContent = latest?.message || (fromWork ? '这里展示 Work 提交到 GitHub 后发布的结果。网站不会自行启动 Work，也不表示已启用每日任务。' : defaults[state] || '候选来自公开页面，视频播放和动作内容仍待核验。');
  $('#last-attempt-label').textContent = fromWork ? 'Work 最近尝试' : '最近尝试';
  $('#last-success-label').textContent = fromWork ? 'Work 最近取得结果' : '最近取得结果';
  $('#last-attempt').textContent = formatDate(data.last_attempt_at || latest?.started_at);
  const successful = runs.find(run => ['succeeded', 'partial'].includes(run.status) && (run.found > 0 || run.added > 0));
  $('#last-success').textContent = formatDate(data.last_success_at || successful?.finished_at);
  const report = $('#source-report');
  const sources = Array.isArray(latest?.sources) ? latest.sources : [];
  if (!sources.length) report.replaceChildren(el('p', '', fromWork ? '暂无独立来源报告，请查看每条候选的原片来源与观察记录。' : '尚无公开页面读取记录。'));
  else report.replaceChildren(...sources.map(source => {
    const row = el('p', 'source-row');
    const link = sourceUrl(source.url);
    row.append(link ? externalLink(`${source.name || 'TikTok 公开页面'} ↗`, link) : el('span', '', source.name || 'TikTok 公开页面'));
    row.append(el('span', '', ` · ${source.status === 'ok' ? `记录 ${Number.isInteger(source.found) ? source.found : '—'} 条候选` : source.message || '暂不可读取'}`));
    return row;
  }));
}
async function loadData() {
  const button = $('#refresh-button'); button.disabled = true; button.textContent = '正在读取…';
  try {
    const response = await fetch(`./data/discovery.json?t=${Date.now()}`, { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const next = await response.json();
    if (!next || !Array.isArray(next.videos) || !Array.isArray(next.runs)) throw new Error('Invalid data schema');
    data = next; renderStatus(); renderVideos(); renderHistory();
  } catch {
    $('#status-heading').textContent = '暂时无法读取已发布结果';
    $('#status-pill').textContent = '读取失败';
    $('#status-pill').className = 'state-pill state-failed';
    $('#status-message').textContent = '请稍后刷新。这是网站数据读取失败，不代表云端没有采集到视频；已有显示内容保留。';
    renderVideos(); renderHistory();
  } finally { button.disabled = false; button.replaceChildren(el('span', '', '↻'), document.createTextNode('刷新已发布结果')); }
}

$('#refresh-button').addEventListener('click', loadData);
$('#favorites-filter').addEventListener('change', event => { onlyFavorites = event.target.checked; renderVideos(); });
$('#search-input').addEventListener('input', event => { search = event.target.value.trim(); renderVideos(); });
$('#history-date-filter').addEventListener('change', event => { historyDate = event.target.value; renderHistory(); });
$('#history-clear-filter').addEventListener('click', () => { historyDate = ''; $('#history-date-filter').value = ''; renderHistory(); });
document.querySelectorAll('[data-category]').forEach(button => button.addEventListener('click', () => {
  category = button.dataset.category;
  document.querySelectorAll('[data-category]').forEach(tab => { const active = tab.dataset.category === category; tab.classList.toggle('active', active); tab.setAttribute('aria-pressed', String(active)); });
  renderVideos();
}));
$('#close-preview').addEventListener('click', () => $('#preview-dialog').close());
$('#preview-dialog').addEventListener('close', () => $('#preview-container').replaceChildren());
$('#preview-dialog').addEventListener('click', event => {
  if (event.target !== $('#preview-dialog')) return;
  const bounds = event.target.getBoundingClientRect();
  if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) event.target.close();
});
loadData();
