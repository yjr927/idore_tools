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
function formatDate(value) {
  if (!value) return '—';
  const time = new Date(value);
  if (!Number.isFinite(time.getTime())) return '—';
  return time.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', timeZoneName: 'short' });
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
function videoCard(video) {
  const canonical = videoUrl(video.url);
  const card = el('article', 'video-card');
  const top = el('div', 'card-top');
  top.append(el('span', 'candidate-pill', '原视频候选 · 待看原片'));
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
  body.append(el('p', 'assessment', video.assessment_note || '已记录公开视频链接。单人、动作重复性与跟拍一致性仍待看原片核验。'));
  const checks = el('div', 'checks');
  checks.append(el('span', '', '单人 · 待核验'), el('span', '', '重复动作 · 待核验'), el('span', '', 'AI 复刻 · 待试做'));
  body.append(checks);
  const metrics = el('div', 'metrics');
  for (const [key, label] of [['views', '播放'], ['likes', '点赞'], ['comments', '评论']]) {
    const cell = el('div'); cell.append(el('strong', '', metric(video.metrics?.[key])), el('span', '', label)); metrics.append(cell);
  }
  body.append(metrics);
  const details = el('div', 'card-details');
  const source = sourceUrl(video.source_url);
  details.append(source ? externalLink(`来源：${video.source || 'TikTok 公开页面'} ↗`, source) : el('span', '', '来源：TikTok 公开页面'));
  details.append(el('span', '', `首次发现 ${formatDate(video.first_seen_at)} · 最近观测 ${formatDate(video.last_seen_at)}`));
  details.append(el('span', '', `原视频发布 ${formatDate(video.published_at)}`));
  body.append(details);
  const actions = el('div', 'card-actions');
  const preview = el('button', 'button primary small', '▶ 查看原片'); preview.type = 'button'; preview.addEventListener('click', () => openPreview(video));
  actions.append(preview, externalLink('TikTok 打开 ↗', canonical.url, 'button small'));
  body.append(actions); card.append(body);
  return card;
}
function renderVideos() {
  const valid = data.videos.filter(video => video && videoUrl(video.url));
  const list = valid.filter(video => (category === 'all' || video.kind === category) && (!onlyFavorites || favorites.has(candidateId(video))) && `${video.title || ''} ${video.author || ''} ${video.kind || ''}`.toLowerCase().includes(search.toLowerCase()));
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
  const runs = data.runs.filter(run => run && typeof run === 'object');
  const latest = runs[0];
  const state = latest?.status || data.status || 'not_run';
  const [title, pill] = statuses[state] || ['采集状态待确认', '状态未知'];
  $('#status-heading').textContent = title;
  $('#status-pill').textContent = pill;
  $('#status-pill').className = `state-pill state-${Object.hasOwn(statuses, state) ? state : 'unknown'}`;
  const defaults = {
    not_run: '尚未取得云端采集记录。需要先运行 GitHub Actions 中的采集任务。',
    blocked: 'TikTok 公开页面没有提供可读取的视频列表。可能需要登录、验证或稍后重试；这不代表 TikTok 没有新趋势。',
    empty: '本次页面读取没有找到候选，不能据此判断没有新趋势。已有数据会保留。'
  };
  $('#status-message').textContent = latest?.message || defaults[state] || '候选来自公开页面，视频播放和动作内容仍待核验。';
  $('#last-attempt').textContent = formatDate(data.last_attempt_at || latest?.started_at);
  const successful = runs.find(run => ['succeeded', 'partial'].includes(run.status) && (run.found > 0 || run.added > 0));
  $('#last-success').textContent = formatDate(data.last_success_at || successful?.finished_at);
  const report = $('#source-report');
  const sources = Array.isArray(latest?.sources) ? latest.sources : [];
  if (!sources.length) report.replaceChildren(el('p', '', '尚无公开页面读取记录。'));
  else report.replaceChildren(...sources.map(source => {
    const row = el('p', 'source-row');
    const link = sourceUrl(source.url);
    row.append(link ? externalLink(`${source.name || 'TikTok 公开页面'} ↗`, link) : el('span', '', source.name || 'TikTok 公开页面'));
    row.append(el('span', '', ` · ${source.status === 'ok' ? `读取到 ${Number.isInteger(source.found) ? source.found : '—'} 条候选` : source.message || '暂不可读取'}`));
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
    data = next; renderStatus(); renderVideos();
  } catch {
    $('#status-heading').textContent = '暂时无法读取已发布结果';
    $('#status-pill').textContent = '读取失败';
    $('#status-pill').className = 'state-pill state-failed';
    $('#status-message').textContent = '请稍后刷新。这是网站数据读取失败，不代表云端没有采集到视频；已有显示内容保留。';
    renderVideos();
  } finally { button.disabled = false; button.replaceChildren(el('span', '', '↻'), document.createTextNode('刷新已发布结果')); }
}

$('#refresh-button').addEventListener('click', loadData);
$('#favorites-filter').addEventListener('change', event => { onlyFavorites = event.target.checked; renderVideos(); });
$('#search-input').addEventListener('input', event => { search = event.target.value.trim(); renderVideos(); });
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
