(() => {
  'use strict';
  const REPO = 'yjr927/idore_tools', API = 'https://api.github.com', WORKFLOW = `https://github.com/${REPO}/actions/workflows/pipeline.yml`;
  const KAI = 'https://raw.githubusercontent.com/yjr927/idore_tools/bd19680c2f14b89be1a1025b7825d9e49bd4b433/reference-assets/kai-full.png';
  const PAGES = { 'create.html': 'production', 'review.html': 'review', 'settings.html': 'settings', 'materials.html': 'materials' };
  const LABELS = { waiting_classification: '等待分类', waiting_material: '等待角色素材', queued: '已排队', preparing_motion: '提取原片动作', rendering_clay: '渲染动作白模', generating_styles: '准备变装造型', generating_video: '生成角色视频', generating_copy: '准备英文文案', pending_review: '等待审核', approved: '审核已通过', uploading: '正在送至草稿', draft_sent: '已送至 TikTok 草稿', needs_attention: '需要处理', rejected: '已退回制作' };
  const MATERIALS = { male: '男生', female: '女生', mixed: '男女混合', unknown: '男女分组待确认' }, CASTS = { solo: '单人', group: '多人', unknown: '人数待确认' };
  const ASSET_KINDS = { character: '角色图', portrait: '角色肖像', single_character: '角色单图', turnaround: '角色多视图', cast_lineup: '多人组合', scale_sheet: '角色比例图', profile_card: '角色设定卡', group: '组合素材', scene: '场景素材' };
  const DISPLAY_ONLY_KINDS = new Set(['scene', 'turnaround', 'cast_lineup', 'scale_sheet', 'profile_card', 'group']);
  const JOURNAL = 'idore-cloud-dispatch-v1', renderers = new Map(), drafts = new Map();
  const legacyParams = new URLSearchParams(location.search); let removedLegacyGrant = false;
  for (const key of ['code', 'state', 'access_token', 'api_key', 'token', 'error', 'error_description']) if (legacyParams.has(key)) { legacyParams.delete(key); removedLegacyGrant = true; }
  if (removedLegacyGrant) history.replaceState(null, '', location.pathname + (legacyParams.toString() ? '?' + legacyParams.toString() : '') + (location.hash || ''));
  let githubToken = '', credentialGeneration = 0, busy = false, navigating = false, loaded = false, readError = false, pipeline = { items: [], settings: {}, connections: {}, updated_at: '' }, characters = [], charactersLoaded = false, charactersError = false, selectedId = '', lastReview = '', returnTo = './create.html';
  const $ = selector => document.querySelector(selector);
  const el = (tag, className, text) => { const result = document.createElement(tag); if (className) result.className = className; if (text !== undefined) result.textContent = String(text); return result; };
  const text = (value, maximum = 300) => typeof value === 'string' ? value.trim().slice(0, maximum) : '';
  const safeMessage = value => text(value, 500).replace(/(?:github_pat_|ghp_|sk-(?:or-)?)[A-Za-z0-9_-]{8,}/g, '[凭证已省略]');
  const ident = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_-]{0,119}$/.test(value);
  const revision = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
  const date = value => { const time = new Date(value); return value && Number.isFinite(time.getTime()) ? new Intl.DateTimeFormat('zh-CN', { timeZone: 'America/New_York', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(time) : '时间待记录'; };
  function mediaUrl(value, kind = 'video') {
    if (typeof value !== 'string' || value.length > 2400) return '';
    const ext = kind === 'image' ? '(?:png|jpg|jpeg|webp)' : 'mp4';
    if (new RegExp(`^(?:generated|reference-assets)/[A-Za-z0-9_-]{1,160}\\.${ext}$`).test(value)) return new URL('./' + value, location.href).href;
    if (kind === 'image') {
      const nested = value.match(/^https:\/\/raw\.githubusercontent\.com\/yjr927\/idore_tools\/[a-f0-9]{40}\/site\/characters\/([^?#]+)$/);
      if (nested) {
        try { const parts = nested[1].split('/').map(part => decodeURIComponent(part)); if (parts.length < 2 || parts.length > 6 || parts.some(part => !part || part === '.' || part === '..' || part.length > 255 || /[\x00-\x1f\x7f/\\%]/.test(part)) || !/\.png$/i.test(parts[parts.length - 1])) return ''; return new URL(value).href; } catch { return ''; }
      }
    }
    try { const url = new URL(value); return url.protocol === 'https:' && url.host === 'raw.githubusercontent.com' && !url.username && !url.password && !url.search && !url.hash && new RegExp(`^/yjr927/idore_tools/[a-f0-9]{40}/reference-assets/[A-Za-z0-9_-]{1,160}\\.${ext}$`).test(url.pathname) ? url.href : ''; } catch { return ''; }
  }
  function original(value, expected = '') {
    try { const url = new URL(value), match = url.pathname.match(/^\/@[A-Za-z0-9_.-]{1,100}\/video\/(\d{10,25})\/?$/); return url.protocol === 'https:' && ['www.tiktok.com', 'tiktok.com'].includes(url.host) && !url.username && !url.password && match && (!expected || match[1] === expected) ? { url: 'https://www.tiktok.com' + url.pathname.replace(/\/$/, ''), id: match[1] } : null; } catch { return null; }
  }
  function normalizeItem(item) {
    if (!item || !ident(item.id) || !Object.hasOwn(LABELS, item.status)) return null;
    const sourceId = typeof item.source_id === 'string' ? item.source_id.replace(/^tiktok-/, '') : '';
    const quotaMessage = item.copy_provider === 'openrouter' ? 'OpenRouter 余额或授权额度不足。补充额度后，可重试英文文案与标签。' : 'OpenAI 余额或项目额度不足。补充额度后，可重试英文文案与标签。';
    return { ...item, title: text(item.title, 180) || '未命名的制作任务', character: text(item.character, 80), source_id: /^\d{10,25}$/.test(sourceId) ? sourceId : '', source_url: original(item.source_url, sourceId)?.url || '', material_group: Object.hasOwn(MATERIALS, item.material_group) ? item.material_group : 'unknown', cast_type: Object.hasOwn(CASTS, item.cast_type) ? item.cast_type : 'unknown', revision: revision(item.revision) ? item.revision : '', message: ['insufficient_quota', 'insufficient_credits'].includes(item.error_code) ? quotaMessage : safeMessage(item.message), video: mediaUrl(item.video_path), poster: mediaUrl(item.poster_path, 'image'), clay: mediaUrl(item.clay_path), image: mediaUrl(item.character_image, 'image') || (item.character === 'KAI' ? KAI : ''), caption: text(item.caption, 6000), captions: Array.isArray(item.captions) ? item.captions.filter(value => typeof value === 'string').slice(0, 5).map(value => text(value, 6000)) : [], hashtags: Array.isArray(item.hashtags) ? item.hashtags.slice(0, 50) : [] };
  }
  function copyConfig() {
    const source = pipeline.connections.copy || {}, provider = pipeline.settings.copy_provider || source.provider;
    return { provider: provider === 'openrouter' ? 'openrouter' : 'openai', model: text(pipeline.settings.copy_model || source.model, 140), label: provider === 'openrouter' ? 'OpenRouter' : 'OpenAI' };
  }
  function link(label, href, className = '') { const result = el('a', className, label); result.href = href; if (href.startsWith('https:')) { result.target = '_blank'; result.rel = 'noopener noreferrer'; } else if (/\b(?:create|review|settings|materials)\.html/.test(href)) result.dataset.workspace = ''; return result; }
  function button(label, handler, className = 'button') { const result = el('button', className, label); result.type = 'button'; result.addEventListener('click', handler); return result; }
  function notify(message = '', kind = '', settings = false) { const area = $('#workspace-message'); if (!area) return; area.className = 'workspace-message' + (kind ? ' ' + kind : ''); area.replaceChildren(document.createTextNode(message)); if (settings) area.append(document.createTextNode(' '), link('前往连接页 →', './settings.html')); }
  function badge(item) { return el('span', 'status-badge ' + (['needs_attention', 'rejected'].includes(item.status) ? 'attention' : ['pending_review', 'approved', 'draft_sent'].includes(item.status) ? 'ready' : ''), LABELS[item.status]); }
  function meta(item) { return [item.character || '角色待匹配', MATERIALS[item.material_group], CASTS[item.cast_type]].join(' · '); }
  function empty(title, subtitle, href = '', label = '') { const panel = el('div', 'empty-panel'); panel.append(el('span', 'empty-symbol', '✳'), el('h2', '', title), el('p', '', subtitle)); if (href) panel.append(link(label, href, 'button')); return panel; }
  function consent(label = '允许本次操作使用云端生成额度。') { const wrap = el('label', 'consent'), input = el('input'); input.type = 'checkbox'; wrap.append(input, el('span', '', label)); return { wrap, input }; }
  function authHint() { const note = el('p', 'subtle'); note.append(document.createTextNode('操作前请先 '), link('连接工作台 →', './settings.html')); return note; }
  function itemLink(item, page = 'review.html') { return `./${page}?item=${encodeURIComponent(item.id)}`; }
  function selectItem(item) { selectedId = item.id; const url = new URL(location.href); url.searchParams.set('item', item.id); history.replaceState(null, '', url.href); renderPage(); }
  function currentItem(items = pipeline.items) { return items.find(item => item.id === selectedId) || items[0] || null; }
  async function publicDocument(path, maximum = 3 * 1024 * 1024) {
    const url = new URL(path, location.href), response = await fetch(url.href, { credentials: 'omit', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer' });
    if (response.status === 404) return null;
    if (!response.ok || response.url && response.url !== url.href || Number(response.headers.get('Content-Length')) > maximum) throw new Error('unavailable');
    const raw = await response.text(); if (raw.length > maximum) throw new Error('too large'); return JSON.parse(raw);
  }
  async function loadPipeline({ quiet = false, render = true } = {}) {
    try {
      const data = await publicDocument('./data/pipeline.json');
      if (!data) { if (!loaded) pipeline = { items: [], settings: {}, connections: {}, updated_at: '' }; loaded = true; if (!quiet) notify('云端制作记录还未发布。你可以先在连接页设置服务。', '', true); }
      else { if (data.schema_version !== 1 || !Array.isArray(data.items) || data.items.length > 2000) throw new Error(); pipeline = { ...data, items: data.items.map(normalizeItem).filter(Boolean).sort((a, b) => (new Date(b.created_at).getTime() || 0) - (new Date(a.created_at).getTime() || 0)), settings: data.settings || {}, connections: data.connections || {} }; loaded = true; }
      readError = false; if (render) renderPage(); return pipeline;
    } catch { readError = true; if (!quiet) notify('暂时无法读取云端进度，已保留当前页面。请稍后刷新。', 'error'); if (render) renderPage(); throw new Error('暂时无法核对云端最新版本，请刷新后再操作。'); }
  }
  async function github(path, { method = 'GET', body, token = githubToken } = {}) {
    const prefix = `/repos/${REPO}`, workflow = `${prefix}/actions/workflows/pipeline.yml`;
    if (!(method === 'GET' && [prefix, workflow].includes(path) || method === 'POST' && path === workflow + '/dispatches') || !token) throw new Error('请先连接当前工作台会话。');
    let response;
    try { response = await fetch(API + path, { method, credentials: 'omit', redirect: 'error', cache: 'no-store', referrerPolicy: 'no-referrer', headers: { Accept: 'application/vnd.github+json', Authorization: 'Bearer ' + token, ...(body ? { 'Content-Type': 'application/json' } : {}) }, ...(body ? { body: JSON.stringify(body) } : {}) }); }
    catch { throw Object.assign(new Error(method === 'POST' ? '请求结果不明确，请先查看云端任务；本站不会自动重发。' : '暂时无法连接 GitHub，请稍后再试。'), { uncertain: method === 'POST' }); }
    if (!response.ok) { const messages = { 401: '工作台凭证无效或已过期，请重新连接。', 403: '当前凭证权限不足。请确认本仓库的 Actions 读写和 Contents 读取权限。', 404: '未找到本仓库工作流，或当前凭证没有访问权限。', 422: '云端尚不接受这项操作，请刷新记录后再试。', 429: '请求较多，请稍后再试。' }; throw Object.assign(new Error(messages[response.status] || '云端暂时未接受这项请求，请查看任务记录。'), { uncertain: method === 'POST' && response.status >= 500 }); }
    if (method === 'POST') { if (response.status !== 204) throw Object.assign(new Error('尚未取得明确的接收结果，请先查看云端任务。'), { uncertain: true }); return null; }
    try { const value = await response.json(); if (!value || typeof value !== 'object') throw new Error(); return value; } catch { throw new Error('没有取得可核对的 GitHub 信息，请稍后再试。'); }
  }
  async function fingerprint(value) { const raw = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify(value))); return Array.from(new Uint8Array(raw), byte => byte.toString(16).padStart(2, '0')).join(''); }
  function readRequests() { const raw = JSON.parse(localStorage.getItem(JOURNAL) || '{}'); if (!raw || Array.isArray(raw) || typeof raw !== 'object') throw new Error(); return raw; }
  async function dispatch(action, item = null, extra = {}) {
    if (busy) return;
    if (!githubToken) { notify('请先连接当前工作台会话，再发送操作请求。', 'error', true); return; }
    if (!['approve', 'reject', 'copy', 'retry', 'sync', 'check'].includes(action)) return;
    const paid = ['copy', 'retry', 'sync'].includes(action);
    if (paid && extra.consent !== true) { notify('请先确认本次操作可使用云端生成额度。', 'error'); return; }
    if (item && !revision(item.revision)) { notify('这条记录还没有可核对的版本，请等待云端更新。', 'error'); return; }
    const copyOperation = action === 'copy' || action === 'retry' && Boolean(item?.video), expectedCopy = extra.copyConfig || copyConfig();
    busy = true; paintSession();
    try {
      await loadPipeline({ quiet: true, render: false });
      const current = item ? pipeline.items.find(row => row.id === item.id) : null;
      if (item && (!current || current.revision !== item.revision)) throw new Error('云端内容已更新。请刷新并重新确认，尚未发送请求。');
      if (copyOperation) { const currentCopy = copyConfig(); if (currentCopy.provider !== expectedCopy.provider || currentCopy.model !== expectedCopy.model) throw new Error('文案服务或模型设置已更新，请刷新并重新确认额度使用。'); }
      const caption = action === 'approve' ? text(extra.caption, 2200) : '', hashtags = action === 'approve' ? text(extra.hashtags, 2000) : '';
      if ([caption, hashtags].some(value => value.includes(githubToken) || /(?:github_pat_|ghp_|sk-(?:or-)?)[A-Za-z0-9_-]{12,}/.test(value))) throw new Error('文案或标签中包含凭证格式的内容，已停止提交。');
      if (action === 'approve' && (!caption || !hashtags || !hashtags.split(/\s+/).every(tag => /^#[\p{L}\p{N}_]{1,100}$/u.test(tag)))) throw new Error('请填写文案；标签以 # 开头，用空格分隔。');
      const targetAccount = action === 'approve' && revision(extra.targetAccount) ? extra.targetAccount : '';
      if (action === 'approve') { const liveAccount = pipeline.connections.tiktok || {}; if (liveAccount.configured && (!targetAccount || targetAccount !== liveAccount.account_fingerprint) || !liveAccount.configured && targetAccount) throw new Error('TikTok 发布账号已变化或尚未核对。请刷新并重新审核，尚未发送请求。'); }
      const inputs = { action, item_id: item?.id || '', revision: item?.revision || '', caption, hashtags, request_id: '', target_account: targetAccount };
      const key = await fingerprint({ ...inputs, revision: inputs.revision || pipeline.updated_at });
      const submit = async () => {
        let requests; try { requests = readRequests(); } catch { throw new Error('无法保存操作记录。为避免重复请求，请允许本站使用浏览器存储。'); }
        const prior = requests[key];
        const acknowledgedFailure = prior?.outcome === 'sent' && item && current.status === 'needs_attention' && current.updated_at !== prior.observed_version && Array.isArray(pipeline.requests) && pipeline.requests.includes(prior.request_id);
        if (prior && !acknowledgedFailure) throw new Error(prior.outcome === 'uncertain' ? '上次请求结果还不明确，已阻止重复发送。请先查看云端任务。' : '这项请求已送达。请等待云端更新后刷新，未重复发送。');
        inputs.request_id = crypto.randomUUID(); requests[key] = { request_id: inputs.request_id, outcome: 'uncertain', at: Date.now(), observed_version: current?.updated_at || '' }; localStorage.setItem(JOURNAL, JSON.stringify(requests));
        try { await github(`/repos/${REPO}/actions/workflows/pipeline.yml/dispatches`, { method: 'POST', body: { ref: 'main', inputs } }); }
        catch (failure) { if (!failure.uncertain) { delete requests[key]; localStorage.setItem(JOURNAL, JSON.stringify(requests)); } throw failure; }
        requests[key].outcome = 'sent';
        try { localStorage.setItem(JOURNAL, JSON.stringify(requests)); } catch { throw new Error('请求已送达，但操作记录未能更新。请先查看云端任务，勿重复提交。'); }
      };
      if (navigator.locks?.request) await navigator.locks.request('idore-cloud-dispatch', { mode: 'exclusive', ifAvailable: true }, async lock => { if (!lock) throw new Error('另一个标签页正在发送请求，请稍后查看进度。'); await submit(); });
      else if (paid) throw new Error('浏览器暂不支持安全提交，请使用最新版 Chrome，或从连接页打开手动云端入口。');
      else await submit();
      notify('请求已送到云端。完成并同步后，页面才会显示新结果。', 'success');
    } catch (failure) { notify(failure.message || '请求未完成，请先查看云端任务。', 'error'); }
    finally { busy = false; paintSession(); }
  }
  function mediaPanel(item, defaultMode = '') {
    const panel = el('div', 'media-panel'), tabs = el('div', 'media-tabs'), frame = el('div', 'media-frame'), modes = [['original', '原片', Boolean(item.source_url)], ['clay', '动作白模', Boolean(item.clay)], ['final', '成片', Boolean(item.video)]];
    const selected = modes.find(([key, , available]) => key === defaultMode && available)?.[0] || (item.video ? 'final' : item.clay ? 'clay' : 'original');
    function show(mode) {
      for (const video of frame.querySelectorAll('video')) video.pause(); frame.replaceChildren();
      for (const child of tabs.children) { const active = child.dataset.mode === mode; child.classList.toggle('active', active); child.setAttribute('aria-pressed', String(active)); }
      if (mode === 'original') { const source = original(item.source_url, item.source_id); if (source) { const iframe = el('iframe'); iframe.src = `https://www.tiktok.com/player/v1/${source.id}?autoplay=0`; iframe.title = 'TikTok 原片预览'; iframe.loading = 'lazy'; iframe.referrerPolicy = 'no-referrer'; iframe.allowFullscreen = true; frame.append(iframe); } else frame.append(el('p', 'media-empty', '原片链接尚未准备好。')); }
      else { const url = mode === 'clay' ? item.clay : item.video; if (url) { const video = el('video'); video.src = url; video.controls = true; video.playsInline = true; video.preload = 'metadata'; if (mode === 'final' && item.poster) video.poster = item.poster; video.setAttribute('aria-label', mode === 'clay' ? '已提取的动作白模' : item.title + '成片'); frame.append(video); } else frame.append(el('p', 'media-empty', mode === 'clay' ? '动作白模尚未生成。' : '成片尚未生成。')); }
    }
    for (const [mode, label, available] of modes) { const tab = button(label, () => show(mode), 'media-tab'); tab.dataset.mode = mode; tab.disabled = !available; tabs.append(tab); }
    panel.append(tabs, frame); show(selected); if (item.source_url) panel.append(link('打开原片 ↗', item.source_url, 'original-link')); return panel;
  }
  function paintSession() {
    for (const item of document.querySelectorAll('[data-session-label]')) item.textContent = githubToken ? '工作台已连接' : '连接工作台 ↗';
    for (const item of document.querySelectorAll('[data-review-count]')) { const count = pipeline.items.filter(row => row.status === 'pending_review').length; item.hidden = !count; item.textContent = String(count); }
    for (const item of document.querySelectorAll('[data-cloud-action]')) item.disabled = busy || item.dataset.actionBlocked === 'true';
    const disconnect = $('#disconnect-workspace'); if (disconnect) disconnect.disabled = busy;
  }
  function tagText(item) { return item.hashtags.map(value => typeof value === 'string' ? value : value?.tag).filter(value => typeof value === 'string').map(value => value.startsWith('#') ? value : '#' + value).join(' '); }
  function citationUrl(value) { if (typeof value !== 'string' || value.length > 2000) return ''; try { const url = new URL(value); return url.protocol === 'https:' && !url.username && !url.password && url.hostname.includes('.') && !/^[0-9.]+$/.test(url.hostname) && !/\.(?:localhost|local|internal)$/.test(url.hostname) ? url.href : ''; } catch { return ''; } }
  function getDraft(item) { let draft = drafts.get(item.id); if (!draft || !draft.dirty && draft.revision !== item.revision) { draft = { revision: item.revision, caption: item.caption || item.captions[0] || '', hashtags: tagText(item), dirty: false }; drafts.set(item.id, draft); } return draft; }
  function renderReview() {
    const root = $('#review-root'); if (!root) return;
    const items = pipeline.items.filter(item => item.video || ['pending_review', 'approved', 'uploading', 'draft_sent'].includes(item.status)), item = currentItem(items);
    root.setAttribute('aria-busy', 'false');
    if (!item) { root.replaceChildren(empty('暂时没有待审核的成片。', '制作完成后，视频和英文文案会来到这里。', './create.html', '查看制作进度 →')); return; }
    const draft = getDraft(item), account = pipeline.connections.tiktok || {}, targetAccount = account.configured && revision(account.account_fingerprint) ? account.account_fingerprint : '', signature = JSON.stringify([pipeline.updated_at, item.id, item.revision, Boolean(githubToken), item.status, targetAccount, account.account_label]); if (lastReview === signature && root.children.length) return; lastReview = signature;
    const layout = el('div', 'review-layout'), queue = el('aside', 'review-queue'); queue.append(el('h2', '', '作品列表'));
    for (const row of items) { const entry = button('', () => { selectedId = row.id; lastReview = ''; selectItem(row); }, 'review-item' + (row.id === item.id ? ' active' : '')); entry.append(el('strong', '', row.title), badge(row), el('small', '', meta(row))); queue.append(entry); }
    const detail = el('section', 'review-detail'), header = el('div', 'review-title'); header.append(el('h2', '', item.title), badge(item)); detail.append(header, el('p', 'meta', meta(item) + ' · ' + date(item.created_at)), mediaPanel(item));
    const edit = el('div', 'review-editor'), caption = el('textarea'), tags = el('textarea'); caption.id = 'review-caption'; caption.rows = 5; caption.maxLength = 2200; caption.value = draft.caption; tags.id = 'review-tags'; tags.rows = 3; tags.maxLength = 2000; tags.value = draft.hashtags;
    const captionLabel = el('label', 'field', '英文文案'); captionLabel.htmlFor = caption.id; const tagsLabel = el('label', 'field', '发布标签'); tagsLabel.htmlFor = tags.id; edit.append(captionLabel, caption, tagsLabel, tags, el('p', 'subtle', '标签用空格分隔。编辑保留在当前工作台会话，通过审核后保存。'));
    for (const input of [caption, tags]) input.addEventListener('input', () => { draft.caption = caption.value; draft.hashtags = tags.value; draft.dirty = true; copy.disabled = !caption.value.trim() && !tags.value.trim(); });
    if (item.captions.length > 1) { const variants = el('details', 'small-details'); variants.append(el('summary', '', '其他文案备选')); for (const value of item.captions) variants.append(button(value, () => { caption.value = value; draft.caption = value; draft.dirty = true; }, 'caption-option')); edit.append(variants); }
    const explained = item.hashtags.filter(value => value && typeof value === 'object' && typeof value.reason === 'string');
    if (explained.length) { const reasons = el('details', 'small-details tag-reasons'); reasons.append(el('summary', '', '查看标签依据')); for (const value of explained.slice(0, 12)) { const row = el('p'); row.append(el('strong', '', text(value.tag, 100)), document.createTextNode(' · ' + text(value.reason, 300))); const url = citationUrl(value.source_url); if (url) row.append(document.createTextNode(' '), link('查看来源 ↗', url, 'text-link')); reasons.append(row); } edit.append(reasons); }
    const copy = button('复制文案与标签', async () => { try { await navigator.clipboard.writeText(caption.value.trim() + '\n\n' + tags.value.trim()); notify('文案与标签已复制。', 'success'); } catch { notify('暂时无法复制，请选中文字手动复制。', 'error'); } }); copy.disabled = !caption.value.trim() && !tags.value.trim(); edit.append(copy);
    const stale = draft.revision !== item.revision;
    if (stale) { const warn = el('div', 'inline-note warning', '云端版本已更新，请重新载入后再审核。'); warn.append(button('重新载入审核版本', () => { drafts.delete(item.id); lastReview = ''; renderReview(); }, 'text-button')); edit.append(warn); }
    if (!item.revision) edit.append(el('p', 'inline-note warning', '这条记录还没有可核对的审核版本，等待云端同步。'));
    const copyRoute = copyConfig(), consentBox = consent(`允许这次生成文案与标签使用 ${copyRoute.label} 额度。`);
    if (item.copy_provider || item.copy_model) edit.append(el('p', 'subtle', '本次文案服务：' + (item.copy_provider === 'openrouter' ? 'OpenRouter' : 'OpenAI') + (item.copy_model ? ' · ' + text(item.copy_model, 140) : '')));
      if (item.video && ['generating_copy', 'needs_attention'].includes(item.status)) { edit.append(consentBox.wrap); const copyAction = button('生成文案与标签', () => dispatch('copy', item, { consent: consentBox.input.checked, copyConfig: copyRoute })); copyAction.dataset.cloudAction = ''; copyAction.dataset.actionBlocked = stale || !item.revision ? 'true' : ''; copyAction.disabled = stale || !item.revision; edit.append(copyAction); }
    if (account.configured) edit.append(el('p', 'approval-target', targetAccount ? '发布账号：' + (text(account.account_label, 80) || '已连接的 TikTok 账号') : 'TikTok 账号信息待核对，请先到连接页检查。'));
    const actions = el('div', 'review-actions'), canReview = item.status === 'pending_review' && item.revision && !stale && (!account.configured || targetAccount);
    const approve = button(account.configured ? '通过并送至 TikTok 草稿' : '通过审核', () => dispatch('approve', item, { caption: caption.value, hashtags: tags.value, targetAccount }), 'button primary'); approve.disabled = !canReview; approve.dataset.revisionBlocked = canReview ? '' : 'true';
    const reject = button('退回制作', () => dispatch('reject', item), 'button quiet'); reject.disabled = !canReview; actions.append(reject, approve); edit.append(actions);
    if (!githubToken) edit.append(authHint());
    edit.append(el('p', 'subtle', pipeline.connections.tiktok?.configured ? '送到草稿后，仍需在 TikTok 中粘贴文案、完成编辑并确认发布。' : 'TikTok 尚未接入，通过后先保存审核结果。接入后仍需在 TikTok 完成编辑与发布。'));
    if (item.message) edit.append(el('p', 'inline-note', item.message)); detail.append(edit); layout.append(queue, detail); root.replaceChildren(layout);
  }
  function connectionCard(name, description, key) {
    const value = pipeline.connections[key] || {}, panel = el('section', 'service-card'), top = el('div', 'service-top');
    const copyRoute = copyConfig(), staleCopy = key === 'copy' && (value.provider && value.provider !== copyRoute.provider || value.model && copyRoute.model && value.model !== copyRoute.model);
    const available = value.configured === true && ['connected', 'ok', 'ready', 'succeeded', 'healthy'].includes(value.status), quota = ['insufficient_quota', 'quota_exceeded', 'quota', 'billing_hard_limit_reached', 'insufficient_credits'].includes(value.status), bad = quota || ['failed', 'error', 'needs_attention', 'auth', 'missing_key', 'missing_model', 'rate_limited', 'invalid_output', 'request_failed'].includes(value.status);
    const unchecked = !value.status || value.status === 'unchecked';
    const routerCopy = key === 'copy' && (value.provider || pipeline.settings.copy_provider) === 'openrouter';
    top.append(el('h2', '', name), el('span', 'status-badge' + (available && !staleCopy ? ' ready' : bad && !staleCopy ? ' attention' : ''), staleCopy ? '待重新检查' : quota ? routerCopy || key === 'openrouter' ? 'OpenRouter 余额 / 授权额度不足' : '余额 / 项目额度不足' : key === 'copy' && value.status === 'generating' ? '正在生成文案' : unchecked ? '待检查' : !value.configured ? '未设置' : available ? '凭证已验证' : bad ? '需要处理' : '已保存 · 待检查')); panel.append(top, el('p', 'service-description', description));
    if (staleCopy) panel.append(el('p', 'subtle', '下次文案使用 ' + copyRoute.label + '；以下为上次服务的记录。'));
    if (key === 'copy' && value.model) panel.append(el('p', 'meta', text(value.model, 140)));
    if (quota) panel.append(el('p', 'inline-note warning', staleCopy ? '这是上次服务的额度记录；新文案服务仍待实际请求验证。' : key === 'openai' && copyConfig().provider === 'openrouter' ? '这是直接 OpenAI 调用的额度问题，不会阻止经 OpenRouter 生成文案。' : routerCopy ? '请补充 OpenRouter 余额或调整授权额度，修复后重试文案。' : key === 'openrouter' ? '请补充 OpenRouter 余额或调整授权额度，修复后重试视频。' : '请在 OpenAI 补充余额或调整项目额度，修复后再重试。'));
    if (value.message && /[\u3400-\u9fff]/.test(value.message)) panel.append(el('p', 'subtle', safeMessage(value.message)));
    if (key === 'tiktok' && value.configured && value.account_label) panel.append(el('p', 'subtle', '发布账号：' + text(value.account_label, 80)));
    if (key !== 'tiktok') panel.append(link(value.configured ? '管理云端凭证 ↗' : '设置云端凭证 ↗', `https://github.com/${REPO}/settings/secrets/actions`, 'text-link'));
    return panel;
  }
  function renderSettings() {
    const root = $('#settings-root'); if (!root) return; root.setAttribute('aria-busy', 'false');
    const session = el('section', 'settings-panel'), top = el('div', 'section-heading'); top.append(el('h2', '', '当前工作台会话'), el('span', 'status-badge' + (githubToken ? ' ready' : ''), githubToken ? '已连接' : '未连接')); session.append(top, el('p', 'subtle', '连接后可从网页审核、重试和检查云端服务。刷新、关闭或离开工作台后需重新连接。'));
    if (!githubToken) {
      const form = el('form', 'session-form'), label = el('label', 'field', 'GitHub 工作台凭证'), input = el('input'); input.id = 'github-workspace-token'; input.type = 'password'; input.autocomplete = 'off'; input.spellcheck = false; input.maxLength = 512; label.htmlFor = input.id; const submit = el('button', 'button primary', '连接工作台'); submit.type = 'submit'; form.append(label, input, submit);
      form.addEventListener('submit', async event => { event.preventDefault(); if (busy) return; const token = input.value.trim(), generation = credentialGeneration; input.value = ''; if (!/^github_pat_[A-Za-z0-9_]{20,490}$/.test(token)) { notify('请使用本仓库的细粒度 GitHub 凭证。', 'error'); return; } busy = true; submit.disabled = true;
        try { const repo = await github(`/repos/${REPO}`, { token }); if (repo.full_name !== REPO || repo.permissions?.pull !== true) throw new Error('这份凭证没有本仓库的读取权限。'); const workflow = await github(`/repos/${REPO}/actions/workflows/pipeline.yml`, { token }); if (workflow.path !== '.github/workflows/pipeline.yml' || workflow.state !== 'active') throw new Error('云端制作工作流尚未启用，请查看手动云端入口。'); if (generation !== credentialGeneration) throw new Error('之前的工作台会话已结束，请重新连接。'); githubToken = token; notify('工作台已连接，尚未发送任何生成请求。', 'success'); renderSettings(); paintSession(); } catch (failure) { githubToken = ''; notify(failure.message || '未完成连接，请检查权限。', 'error'); } finally { busy = false; submit.disabled = false; paintSession(); }
      }); session.append(form);
      const setup = el('details', 'small-details'); setup.append(el('summary', '', '第一次连接怎么设置？'), el('p', '', '创建仅限 yjr927/idore_tools 的 fine-grained token，开放 Actions 读写、Contents 读取权限。它只保留在当前标签页内，不写入浏览器存储。'), link('创建 GitHub 凭证 ↗', `https://github.com/settings/personal-access-tokens/new?target_name=yjr927&name=idore-workspace&description=idore%20cloud%20workspace`, 'text-link')); session.append(setup);
    } else { const row = el('div', 'settings-actions'); row.append(link('返回工作台 →', returnTo, 'button primary')); const disconnect = button('断开当前会话', () => { if (busy) return; githubToken = ''; renderSettings(); paintSession(); notify('当前工作台已断开。云端服务仍按原设置运行。'); }); disconnect.id = 'disconnect-workspace'; row.append(disconnect); session.append(row); }
    const serviceHeading = el('div', 'section-heading settings-section-title'); serviceHeading.append(el('h2', '', '云端服务')); const check = button('检查连接（不生成）', () => dispatch('check')); check.dataset.cloudAction = ''; serviceHeading.append(check);
    const services = el('div', 'service-grid'), copyRoute = copyConfig(); services.append(connectionCard('英文文案与标签', copyRoute.provider === 'openrouter' ? 'OpenAI 模型，经 OpenRouter 分析成片、搜索资料并准备英文文案与标签。' : '通过直接 OpenAI 调用分析成片并准备文案与标签。', 'copy'), connectionCard('OpenRouter · 视频', '使用角色图和动作白模生成视频。', 'openrouter'));
    const tiktok = connectionCard('TikTok', '审核后送至草稿，在 TikTok 中完成最终编辑。', 'tiktok');
    tiktok.append(link('在 JJ TEST 测试 TikTok 草稿上传 ↗', 'https://idore-tools.yjr927.workers.dev/sandbox/tiktok', 'text-link'));
    if (!pipeline.connections.tiktok?.configured) { const disabled = button('连接 TikTok', () => {}); disabled.disabled = true; tiktok.append(disabled); const steps = el('details', 'small-details'); steps.append(el('summary', '', '接入前需要准备什么？'), el('p', '', '先创建 TikTok 开发者应用，申请视频上传权限并完成平台审核，再连接发布账号。当前尚未接入，网页不会假装上传成功。'), link('查看 TikTok 官方接入步骤 ↗', 'https://developers.tiktok.com/doc/content-posting-api-get-started/', 'text-link')); tiktok.append(steps); }
    services.append(tiktok);
    const optionalStyles = el('details', 'small-details material-section'); optionalStyles.append(el('summary', '', '可选 · OpenAI 变装造型图'), connectionCard('OpenAI · 可选造型图', '仅用于可选的变装造型图，不影响 OpenRouter 文案服务。', 'openai'));
    const keys = el('p', 'subtle'); keys.append(document.createTextNode('视频与当前文案共用云端 OpenRouter 密钥；直接 OpenAI 密钥仅供可选造型图。密钥保存在 GitHub Secrets，网页不会读取或展示。 '), link('管理云端凭证 ↗', `https://github.com/${REPO}/settings/secrets/actions`, 'text-link'));
    const materials = el('section', 'settings-panel material-section'); materials.id = 'role-materials'; const materialHeading = el('div', 'section-heading'); materialHeading.append(el('h2', '', '角色素材'), link('查看角色素材 →', './materials.html', 'button')); materials.append(materialHeading);
    const enabled = characters.filter(item => item.enabled === true && !DISPLAY_ONLY_KINDS.has(item.asset_kind)), ips = new Set(characters.map(item => materialName(item.ip, '未分组')));
    materials.append(el('p', 'subtle', charactersLoaded ? `${ips.size} 个 IP · ${characters.length} 份素材 · ${enabled.length} 份已启用。先选 IP，再选日常图或万圣节图。` : charactersError ? '角色素材清单暂时无法读取。' : '正在读取角色素材清单…'));
    if (pipeline.items.some(item => item.status === 'waiting_material' && item.material_group === 'female') && !enabled.some(item => item.material_group === 'female')) materials.append(el('p', 'inline-note warning', characters.some(item => item.material_group === 'female' && item.asset_kind !== 'scene') ? '女生角色素材已收录，尚未启用制作匹配；相关任务会继续等待。' : '女生角色的匹配素材仍待补充或确认，相关任务会继续等待。'));
    const fallback = el('p', 'subtle settings-fallback'); fallback.append(document.createTextNode('网页暂时无法连接时，也可使用 '), link('手动云端入口 ↗', WORKFLOW, 'text-link'));
    root.replaceChildren(session, serviceHeading, services, keys, optionalStyles, materials, fallback);
  }
  function context() { return { pipeline, characters, el, text, date, link, button, badge, meta, empty, consent, authHint, itemLink, selectItem, currentItem, mediaPanel, dispatch, copyConfig, connected: Boolean(githubToken), busy, notify }; }
  function materialName(value, fallback = '') { return typeof value === 'string' && value.length ? value.slice(0, 255) : fallback; }
  function materialHref(ip = '', collection = '') { const url = new URL('./materials.html', location.href); if (ip) url.searchParams.set('ip', ip); if (ip && collection) url.searchParams.set('collection', collection); return url.pathname + url.search; }
  function renderMaterials() {
    const root = $('#materials-root'); if (!root) return; root.setAttribute('aria-busy', String(!charactersLoaded && !charactersError));
    if (!charactersLoaded) { root.replaceChildren(empty(charactersError ? '素材清单暂时无法读取。' : '正在读取角色素材…', charactersError ? '请稍后刷新，已保存的素材不会因此改变。' : '从已发布的角色清单读取。')); return; }
    if (!characters.length) { root.replaceChildren(empty('还没有已发布的角色素材。', '角色图片加入后，会先按 IP 名称显示在这里。')); return; }
    const groups = new Map();
    for (const item of characters) { const ip = materialName(item.ip, '未分组'), collection = materialName(item.collection, '日常图'); if (!groups.has(ip)) groups.set(ip, new Map()); const variants = groups.get(ip); if (!variants.has(collection)) variants.set(collection, []); variants.get(collection).push(item); }
    const params = new URLSearchParams(location.search), ip = params.get('ip') || '', collection = ip ? params.get('collection') || '' : '';
    const breadcrumb = el('nav', 'material-breadcrumb'); breadcrumb.setAttribute('aria-label', '素材位置'); breadcrumb.append(link('角色素材', materialHref())); if (ip) { breadcrumb.append(el('span', '', ' / ')); breadcrumb.append(collection ? link(ip, materialHref(ip)) : el('span', '', ip)); } if (collection) breadcrumb.append(el('span', '', ' / ' + collection));
    const summary = el('div', 'material-section-heading');
    if (!ip) {
      summary.append(el('h2', '', '选择一个 IP'), el('p', 'meta', `${groups.size} 个 IP · 先选 IP，再看素材分类`));
      const grid = el('div', 'ip-grid');
      for (const [name, variants] of groups) { const count = [...variants.values()].reduce((sum, rows) => sum + rows.length, 0), card = link('', materialHref(name), 'ip-card'); card.append(el('span', 'ip-symbol', '✳'), el('h3', '', name), el('p', '', `${variants.size} 个分类 · ${count} 份素材`), el('span', 'ip-open', '查看分类 →')); grid.append(card); }
      root.replaceChildren(breadcrumb, summary, grid); return;
    }
    const variants = groups.get(ip); if (!variants) { root.replaceChildren(breadcrumb, empty('没有找到这个 IP。', '返回 IP 列表，选择已保存的素材。', materialHref(), '返回 IP 列表 →')); return; }
    if (!collection) {
      const preferred = ['日常图', '万圣节图', '场景素材'], rank = name => preferred.includes(name) ? preferred.indexOf(name) : preferred.length, names = [...variants.keys()].sort((a, b) => rank(a) - rank(b));
      summary.append(el('h2', '', ip), el('p', 'meta', `${variants.size} 个素材分类 · 点击分类查看图片`));
      const grid = el('div', 'ip-grid variant-grid');
      for (const name of names) { const card = link('', materialHref(ip, name), 'variant-card'); card.append(el('span', 'ip-symbol', '▱'), el('h3', '', name), el('p', '', `${variants.get(name).length} 份素材`), el('span', 'ip-open', '查看图片 →')); grid.append(card); }
      root.replaceChildren(breadcrumb, summary, grid, link('← 返回 IP 列表', materialHref(), 'button material-back')); return;
    }
    const items = variants.get(collection); if (!items) { root.replaceChildren(breadcrumb, empty('没有找到这个素材分类。', '返回当前 IP，选择已保存的素材分类。', materialHref(ip), '返回素材分类 →')); return; }
    summary.append(el('h2', '', collection), el('p', 'meta', `${items.length} 份素材 · 保留原始名称与完整画面`));
    const grid = el('div', 'role-assets-grid');
    for (const item of items) {
      const card = el('article', 'role-asset-card'), name = materialName(item.name, '未命名素材'), filename = materialName(item.filename), image = mediaUrl(item.image_url, 'image'), scene = item.asset_kind === 'scene', displayOnly = DISPLAY_ONLY_KINDS.has(item.asset_kind), frame = el('div', 'role-asset-frame');
      if (image) { const preview = link('', image, 'role-image-link'), img = el('img'); img.src = image; img.alt = ip + ' · ' + name; img.loading = 'lazy'; img.decoding = 'async'; preview.setAttribute('aria-label', '查看 ' + ip + ' / ' + name + ' 原图'); preview.append(img); frame.append(preview); } else frame.append(el('p', 'meta', '图片地址待确认'));
      const body = el('div', 'role-asset-body'); body.append(el('h3', '', name)); if (filename) body.append(el('p', 'role-filename', filename)); const characterGroup = materialName(item.character_group); if (characterGroup) body.append(el('p', 'meta role-character-group', characterGroup));
      const labels = el('div', 'role-asset-labels');
      if (scene) labels.append(el('span', '', '场景素材'), el('span', '', '仅展示'));
      else { if (ASSET_KINDS[item.asset_kind]) labels.append(el('span', '', ASSET_KINDS[item.asset_kind])); labels.append(el('span', '', MATERIALS[item.material_group] || MATERIALS.unknown), el('span', '', CASTS[item.cast_type] || CASTS.unknown), el('span', item.enabled === true && !displayOnly ? 'enabled' : '', displayOnly ? '仅展示' : item.enabled === true ? '已启用匹配' : '尚未启用匹配')); } body.append(labels);
      if (image) body.append(link('查看原图 ↗', image, 'text-link')); card.append(frame, body); grid.append(card);
    }
    root.replaceChildren(breadcrumb, summary, grid, link('← 返回素材分类', materialHref(ip), 'button material-back'));
  }
  function renderPage() { const page = document.body.dataset.page; if (!loaded && readError && !['settings', 'materials'].includes(page)) { const root = $(page === 'review' ? '#review-root' : '#production-root'); root?.replaceChildren(empty('云端记录暂时无法读取。', '请稍后刷新，现有任务不会因此重新提交。')); root?.setAttribute('aria-busy', 'false'); return; } if (page === 'review') renderReview(); else if (page === 'settings') renderSettings(); else if (page === 'materials') renderMaterials(); else renderers.get(page)?.(context()); paintSession(); }
  async function navigate(href, push = true) {
    const url = new URL(href, location.href), name = url.pathname.split('/').pop(); if (url.origin !== location.origin || !PAGES[name] || navigating) return;
    navigating = true;
    try { const response = await fetch(url.href.split('#')[0], { credentials: 'omit', redirect: 'error' }); if (!response.ok) throw new Error(); const raw = await response.text(); if (raw.length > 200000) throw new Error(); const doc = new DOMParser().parseFromString(raw, 'text/html'); if (doc.body.dataset.page !== PAGES[name]) throw new Error(); for (const script of doc.body.querySelectorAll('script')) script.remove(); for (const video of document.querySelectorAll('video')) video.pause(); if (document.body.dataset.page !== 'settings') returnTo = location.pathname + location.search; document.body.replaceWith(document.importNode(doc.body, true)); document.title = doc.title; if (push) history.pushState(null, '', url.href); selectedId = new URLSearchParams(url.search).get('item') || ''; lastReview = ''; renderPage(); window.scrollTo(0, 0); const heading = document.querySelector('h1'); heading?.setAttribute('tabindex', '-1'); heading?.focus({ preventScroll: true }); } catch { notify('页面暂时无法打开，请稍后再试。', 'error'); } finally { navigating = false; }
  }
  document.addEventListener('click', event => {
    const anchor = event.target.closest('a[data-workspace]'); if (anchor && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey && event.button === 0) { event.preventDefault(); navigate(anchor.href); return; }
    if (event.target.closest('[data-refresh]')) { if (document.body.dataset.page === 'materials') loadCharacters(); else loadPipeline().catch(() => {}); }
  });
  window.addEventListener('popstate', () => navigate(location.href, false));
  window.addEventListener('pagehide', () => { githubToken = ''; credentialGeneration++; });
  window.addEventListener('pageshow', event => { if (event.persisted) { lastReview = ''; renderPage(); } });
  window.IdoreWorkspace = Object.freeze({ register: (name, renderer) => { renderers.set(name, renderer); if (loaded && document.body.dataset.page === name) renderPage(); }, render: renderPage });
  async function loadCharacters() {
    try { const data = await publicDocument('./data/characters.json', 4 * 1024 * 1024); if (data !== null && (data.schema_version !== 1 || !Array.isArray(data.items) || data.items.length > 5000 || !data.items.every(item => item && typeof item === 'object' && !Array.isArray(item)))) throw new Error(); if (data) characters = data.items; else if (!charactersLoaded) characters = []; charactersLoaded = true; charactersError = false; }
    catch { charactersError = true; if (document.body.dataset.page === 'materials') notify('暂时无法读取最新素材，已保留之前的清单。', 'error'); }
    if (['settings', 'materials'].includes(document.body.dataset.page)) renderPage();
  }
  async function boot() {
    selectedId = new URLSearchParams(location.search).get('item') || ''; const source = original(new URLSearchParams(location.search).get('reference'));
    try { await loadPipeline(); if (!selectedId && source) { selectedId = pipeline.items.find(item => item.source_id === source.id)?.id || ''; if (!selectedId && document.body.dataset.page === 'production') notify('这条原片还未进入制作队列。同步最新趋势后再查看。'); renderPage(); } } catch { /* An honest unavailable state stays visible. */ }
    await loadCharacters();
    setInterval(() => { if (!busy && document.visibilityState !== 'hidden') { loadPipeline({ quiet: true }).catch(() => {}); if (document.body.dataset.page === 'materials') loadCharacters(); } }, 30000);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, { once: true }); else queueMicrotask(boot);
})();
