(() => {
  'use strict';
  let filter = 'all';
  const WORKING = new Set(['queued', 'preparing_motion', 'rendering_clay', 'generating_styles', 'generating_video', 'generating_copy']);
  const WAITING = new Set(['waiting_classification', 'waiting_material', 'needs_attention', 'rejected']);
  const STAGES = ['原片', '动作提取', '白模渲染', '角色成片', '英文文案'];
  function render(ctx) {
    const root = document.querySelector('#production-root'); if (!root) return;
    root.setAttribute('aria-busy', 'false');
    const { pipeline, el, button, link, badge, meta, empty } = ctx;
    const limit = pipeline.settings.daily_limit;
    document.querySelector('#production-rule').textContent = Number.isInteger(limit) && limit > 0 ? `每天最多 ${limit} 条 · K-pop 2 / 虚拟偶像 2 · Work 云端采集` : '美国 / 英文 · 制作规则待同步';
    const groups = { all: pipeline.items, working: pipeline.items.filter(item => WORKING.has(item.status)), review: pipeline.items.filter(item => item.status === 'pending_review'), attention: pipeline.items.filter(item => WAITING.has(item.status)) };
    const summary = el('div', 'production-summary');
    for (const [key, label] of [['all', '全部任务'], ['working', '制作中'], ['review', '待审核'], ['attention', '等待处理']]) {
      const control = button('', () => { filter = key; window.IdoreWorkspace.render(); }, 'summary-card' + (filter === key ? ' active' : '')); control.setAttribute('aria-pressed', String(filter === key)); control.append(el('strong', '', groups[key].length), el('span', '', label)); summary.append(control);
    }
    const operations = el('div', 'production-actions'), syncConsent = ctx.consent('允许同步后在每日限额内继续制作，使用云端服务额度。'), sync = button('同步并继续制作', () => ctx.dispatch('sync', null, { consent: syncConsent.input.checked })); sync.dataset.cloudAction = ''; operations.append(syncConsent.wrap, sync);
    if (!pipeline.items.length) { root.replaceChildren(summary, operations, empty('制作队列还空着。', '同步趋势后，符合条件的原片会进入云端制作流程。', './', '去看趋势素材 →')); return; }
    const items = groups[filter], selected = ctx.currentItem(items);
    if (!selected) { root.replaceChildren(summary, operations, empty('这个分类暂时没有任务。', '切换到全部任务，查看已经保存的制作记录。')); return; }
    const layout = el('div', 'production-layout'), list = el('div', 'production-list');
    for (const item of items) {
      const row = button('', () => ctx.selectItem(item), 'production-item' + (item.id === selected.id ? ' active' : '')); row.setAttribute('aria-pressed', String(item.id === selected.id));
      if (item.image) { const image = el('img', 'production-thumb'); image.src = item.image; image.alt = item.character || '角色形象'; image.loading = 'lazy'; row.append(image); } else row.append(el('span', 'production-thumb placeholder', '✳'));
      const body = el('div', 'production-item-body'); body.append(el('h3', '', item.title), el('p', 'meta', meta(item)), badge(item)); row.append(body, el('span', 'production-chevron', '›')); list.append(row);
    }
    const detail = el('section', 'production-detail'), heading = el('div', 'detail-heading'), title = el('div'); title.append(el('h2', '', selected.title), el('p', 'meta', meta(selected))); heading.append(title, badge(selected)); detail.append(heading, ctx.mediaPanel(selected));
    const statusStep = { queued: 0, preparing_motion: 1, rendering_clay: 2, generating_video: 3, generating_copy: 4, pending_review: 5, approved: 5, uploading: 5, draft_sent: 5 };
    let index = statusStep[selected.status];
    if (index === undefined && selected.status === 'needs_attention') index = statusStep[selected.stage] ?? (selected.video ? 4 : selected.clay ? 3 : -1);
    if (index === undefined) index = -1;
    const steps = el('ol', 'production-steps'); steps.setAttribute('aria-label', '原片到角色成片的制作阶段');
    STAGES.forEach((stage, step) => { const row = el('li', 'production-step' + (step < index ? ' done' : step === index ? ' active' : '')); row.append(el('span', '', step < index ? '✓' : step + 1), document.createTextNode(stage)); if (step === index) row.setAttribute('aria-current', 'step'); steps.append(row); }); detail.append(steps);
    if (selected.status === 'waiting_classification') detail.append(el('p', 'material-hold', '等待确认原片的分组、人数与是否变妆变装。确认前不会开始角色生成。'));
    if (selected.status === 'waiting_material') detail.append(el('p', 'material-hold', selected.material_group === 'female' ? '等待女生角色素材。不会用男生角色代替。' : '等待匹配的角色素材，准备好后再继续。'));
    if (selected.message) detail.append(el('p', 'inline-note' + (selected.status === 'needs_attention' ? ' warning' : ''), selected.message));
    const actions = el('div', 'detail-actions');
    if (selected.video) actions.append(link('去审核成片 →', ctx.itemLink(selected), 'button primary'));
    if (['waiting_material', 'waiting_classification'].includes(selected.status)) actions.append(link('查看连接与素材 →', './settings.html', 'button'));
    if (['needs_attention', 'rejected'].includes(selected.status) || selected.video && selected.status === 'generating_copy') {
      const paid = ctx.consent('允许本次重试或文案生成使用云端服务额度。'); detail.append(paid.wrap);
      if (selected.video) { const copy = button('生成文案与标签', () => ctx.dispatch('copy', selected, { consent: paid.input.checked })); copy.dataset.cloudAction = ''; copy.dataset.actionBlocked = selected.revision ? '' : 'true'; copy.disabled = !selected.revision; actions.append(copy); }
      if (['needs_attention', 'rejected'].includes(selected.status)) { const retry = button('重试当前步骤', () => ctx.dispatch('retry', selected, { consent: paid.input.checked })); retry.dataset.cloudAction = ''; retry.dataset.actionBlocked = selected.revision ? '' : 'true'; retry.disabled = !selected.revision; actions.append(retry); }
    }
    if (actions.children.length) detail.append(actions);
    if (!ctx.connected) detail.append(ctx.authHint());
    layout.append(list, detail); root.replaceChildren(summary, operations, layout);
  }
  window.IdoreWorkspace.register('production', render);
})();
