'use strict';

(() => {
  const workerOrigin = 'https://idore-tools.yjr927.workers.dev';
  const githubDispatch = 'https://api.github.com/repos/yjr927/idore_tools/actions/workflows/photo-generation.yml/dispatches';
  const sessionKey = 'idore-photo-current-v1';
  const maxBytes = 6 * 1024 * 1024;
  const types = new Map([['image/jpeg', 'jpg'], ['image/png', 'png'], ['image/webp', 'webp']]);
  const $ = id => document.getElementById(id);
  let files = [];
  let job = null;
  let timer = 0;
  let busy = false;
  let started = false;
  let pendingUpload = false;
  let resultUrl = '';
  let waitingForFiles = false;
  let terminal = false;

  function notice(message) {
    $('status').textContent = message;
  }

  function error(message = '') {
    $('form-error').textContent = message;
    $('form-error').hidden = !message;
  }

  function saveJob() {
    try {
      if (job) sessionStorage.setItem(sessionKey, JSON.stringify(job));
      else sessionStorage.removeItem(sessionKey);
    } catch { /* In private browsing the current tab can still finish the request. */ }
  }

  function refreshControls() {
    $('editor').disabled = busy || Boolean(job && !waitingForFiles);
    $('generate').disabled = busy || Boolean(job && !waitingForFiles);
    $('generate').textContent = waitingForFiles ? '提交至当前任务' : job ? '已创建任务' : '生成照片 ↗';
    $('token').disabled = Boolean(job) || busy;
    $('connection-mode').textContent = $('token').value.trim() ? '使用本次会话凭证' : 'GitHub 网页授权';
  }

  function clearFiles() {
    for (const item of files) URL.revokeObjectURL(item.url);
    files = [];
    renderFiles();
  }

  function renderFiles() {
    $('file-count').textContent = files.length + ' / 4';
    $('previews').replaceChildren();
    files.forEach((item, index) => {
      const li = document.createElement('li');
      const img = document.createElement('img');
      img.src = item.url;
      img.alt = '参考图 ' + (index + 1) + '，' + item.file.name;
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.textContent = '×';
      remove.setAttribute('aria-label', '移除参考图 ' + (index + 1));
      remove.addEventListener('click', () => {
        if ($('editor').disabled) return;
        URL.revokeObjectURL(item.url);
        files.splice(index, 1);
        renderFiles();
        $('files').focus();
      });
      li.append(img, remove);
      $('previews').append(li);
    });
  }

  function addFiles(selected) {
    if ($('editor').disabled) return;
    const next = [...selected];
    const total = files.reduce((sum, item) => sum + item.file.size, 0) + next.reduce((sum, file) => sum + file.size, 0);
    if (files.length + next.length > 4) return error('每张成图最多可使用 4 张参考图。');
    if (total > maxBytes) return error('参考图总大小超过 6 MiB，请缩小图片后再添加。');
    if (next.some(file => !types.has(file.type) || !file.size)) return error('请选择有效的 JPG、PNG 或 WebP 图片。');
    error();
    files.push(...next.map(file => ({ file, url: URL.createObjectURL(file) })));
    renderFiles();
  }

  function readImage(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.addEventListener('load', () => resolve({ mime: file.type, data: String(reader.result).split(',')[1] }));
      reader.addEventListener('error', () => reject(new Error('image_read')));
      reader.readAsDataURL(file);
    });
  }

  function hex(bytes) {
    return Array.from(bytes, n => n.toString(16).padStart(2, '0')).join('');
  }

  async function jobId(secret) {
    return hex(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(secret))));
  }

  function api(op, options = {}) {
    return fetch('/api/photo/' + job.id + '/' + op, {
      cache: 'no-store', credentials: 'omit', redirect: 'error', referrerPolicy: 'no-referrer',
      ...options,
      headers: { ...options.headers, Authorization: 'Bearer ' + job.secret },
      signal: AbortSignal.timeout(op === 'input' || op === 'result' ? 60000 : 20000)
    });
  }

  function schedule(delay = 2500) {
    clearTimeout(timer);
    if (!terminal) timer = window.setTimeout(check, delay);
  }

  function finish(message) {
    terminal = true;
    waitingForFiles = false;
    pendingUpload = false;
    clearTimeout(timer);
    $('manual').hidden = true;
    $('check').hidden = true;
    $('new').hidden = false;
    $('empty-text').textContent = resultUrl ? '' : '本次没有可下载的照片';
    refreshControls();
    notice(message);
  }

  const failures = {
    missing_key: '云端未配置 OpenRouter 项目密钥。',
    input_timeout: '云端等待上传超时，请新建照片任务。',
    invalid_input: '参考图或提示词未通过检查，请重新选择有效图片。',
    provider_auth: 'OpenRouter 未接受项目凭证。',
    quota_exceeded: '项目额度不足，请联系工作台管理员。',
    rate_limited: 'OpenRouter 暂时限流。',
    provider_rejected: '模型未接受本次输入，请检查参考图和提示词。',
    submission_uncertain: '模型请求的结果未能确认。请先核对项目的 OpenRouter 调用记录，以免重复计费。',
    invalid_result: '服务商回复中没有可用的照片，请先核对 OpenRouter 调用记录。',
    delivery_failed: '生成结果未能保存到工作台，请先核对 OpenRouter 调用记录。'
  };

  async function upload() {
    pendingUpload = false;
    waitingForFiles = false;
    refreshControls();
    notice('正在上传参考图和提示词…');
    const prompt = $('prompt').value.trim();
    const images = await Promise.all(files.map(item => readImage(item.file)));
    const response = await api('input', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prompt, images })
    });
    if ([400, 413, 415, 422].includes(response.status)) {
      waitingForFiles = true;
      refreshControls();
      error('上传未通过。请检查图片格式、大小和提示词，并提交至当前任务。');
      return;
    }
    if (!response.ok && response.status !== 409) throw new Error('upload_status');
    notice('素材已交给云端，正在等待模型生成。');
  }

  async function check() {
    if (!job || busy || terminal) return;
    busy = true;
    clearTimeout(timer);
    $('check').disabled = true;
    try {
      const response = await api('status');
      if (response.status === 404 && Date.now() - job.time < 30 * 60000) {
        notice(job.manual ? '等待你在 GitHub 启动工作流，启动后本页会自动上传素材。' : '正在等待 GitHub 分配云端任务…');
        schedule(3500);
        return;
      }
      if (response.status === 410 || response.status === 404) {
        finish('任务已过期或未在有效时间内启动。');
        return;
      }
      if (response.status === 401 || response.status === 403) {
        finish('当前会话无法读取这个任务。');
        return;
      }
      if (!response.ok) throw new Error('status');
      const state = await response.json();
      $('manual').hidden = true;
      if (state.status === 'waiting') {
        if (pendingUpload) await upload();
        else {
          waitingForFiles = true;
          notice('云端已就绪。请确认参考图和提示词，再点「提交至当前任务」。');
        }
      } else if (state.status === 'uploaded' || state.status === 'generating') {
        waitingForFiles = false;
        $('empty-text').textContent = state.status === 'generating' ? 'Seedream 正在生成照片…' : '参考图已上传';
        notice(state.status === 'generating' ? '模型已开始生成，可以在此查看进度。' : '素材已上传，正在准备生成。');
      } else if (state.status === 'ready') {
        notice('照片已生成，正在读取…');
        const binary = await api('result');
        if (!binary.ok) throw new Error('read_result');
        const image = await binary.blob();
        if (!types.has(image.type) || !image.size) throw new Error('read_result');
        resultUrl = URL.createObjectURL(image);
        $('photo').src = resultUrl;
        $('photo').hidden = false;
        $('empty').hidden = true;
        $('download').href = resultUrl;
        $('download').download = 'idore-photo-' + job.id.slice(0, 8) + '.' + types.get(image.type);
        $('download').hidden = false;
        finish('已生成 1 张照片。请在任务有效期内下载保存。');
        return;
      } else if (state.status === 'failed') {
        finish(failures[state.code] || '本次云端任务未完成，请查看 GitHub Actions 中对应的运行记录。');
        return;
      } else throw new Error('status');
      schedule(waitingForFiles ? 6000 : 2500);
    } catch {
      $('check').hidden = false;
      notice('暂时无法读取云端进度，将继续查询同一任务。');
      schedule(7000);
    } finally {
      busy = false;
      $('check').disabled = false;
      refreshControls();
    }
  }

  function showManual() {
    $('job-id').value = job.id;
    $('manual').hidden = false;
  }

  async function submit(event) {
    event.preventDefault();
    if (busy || started || terminal) return;
    error();
    if (!files.length) { error('请先添加至少一张参考图。'); $('files').focus(); return; }
    const length = Array.from($('prompt').value.trim()).length;
    if (!length || length > 4000) { error('请输入 1 至 4000 字的提示词。'); $('prompt').focus(); return; }
    const token = $('token').value.trim();
    if (!job && token && !/^github_pat_[a-zA-Z0-9_]{20,490}$/.test(token)) {
      $('connection').open = true;
      error('请使用此仓库的细粒度 GitHub 凭证，或清空后通过 GitHub 网页启动。');
      $('token').focus(); return;
    }
    if (job && !waitingForFiles) return;
    pendingUpload = true;
    waitingForFiles = false;
    if (job) { refreshControls(); await check(); return; }
    started = true;
    busy = true;
    refreshControls();
    try {
      const secret = hex(crypto.getRandomValues(new Uint8Array(32)));
      job = { secret, id: await jobId(secret), time: Date.now(), manual: !token };
      saveJob();
      if (token) {
        notice('正在启动云端工作流…');
        const response = await fetch(githubDispatch, {
          method: 'POST', headers: { Authorization: 'Bearer ' + token, Accept: 'application/vnd.github+json', 'Content-Type': 'application/json' },
          body: JSON.stringify({ ref: 'main', inputs: { job_id: job.id } }),
          credentials: 'omit', redirect: 'error', referrerPolicy: 'no-referrer', signal: AbortSignal.timeout(30000)
        });
        if ([400,401,403,404,422,429].includes(response.status)) {
          const status = response.status;
          job = null;
          saveJob();
          pendingUpload = false;
          $('connection').open = true;
          error(status === 429 ? 'GitHub 暂时限流，请稍后再试。' : 'GitHub 未接受启动请求，请检查仓库访问和 Actions 写入权限，或清空凭证使用网页授权。');
          notice('云端任务未启动。');
          return;
        }
        if (!response.ok) throw new Error('dispatch');
        notice('已提交启动请求，等待云端就绪。');
      } else {
        showManual();
        notice('请在 GitHub 网页启动本次任务。');
        $('manual').scrollIntoView({ block: 'nearest' });
      }
    } catch {
      if (!job) { error('当前浏览器无法建立私有任务，请使用支持 HTTPS 的现代浏览器。'); return; }
      notice('启动请求结果待确认，将查询当前任务。');
    } finally {
      busy = false;
      started = false;
      refreshControls();
      if (job) schedule(1000);
    }
  }

  function reset() {
    if (busy || !terminal) return;
    if (resultUrl) URL.revokeObjectURL(resultUrl);
    resultUrl = '';
    job = null;
    saveJob();
    pendingUpload = false;
    terminal = false;
    waitingForFiles = false;
    $('photo').removeAttribute('src');
    $('photo').hidden = true;
    $('download').removeAttribute('href');
    $('download').hidden = true;
    $('empty').hidden = false;
    $('empty-text').textContent = '生成的照片会显示在这里';
    $('new').hidden = true;
    $('check').hidden = true;
    $('prompt').value = '';
    $('prompt-count').textContent = '0 / 4000';
    clearFiles();
    error();
    notice('');
    refreshControls();
    $('files').focus();
  }

  async function start() {
    if (location.origin !== workerOrigin) {
      $('mirror').hidden = false;
      $('editor').disabled = true;
      $('token').disabled = true;
      $('generate').disabled = true;
      $('compose').addEventListener('submit', event => event.preventDefault());
      return;
    }
    $('compose').addEventListener('submit', submit);
    $('new').addEventListener('click', reset);
    $('check').addEventListener('click', check);
    $('prompt').addEventListener('input', () => {
      $('prompt-count').textContent = Array.from($('prompt').value).length + ' / 4000';
    });
    $('token').addEventListener('input', refreshControls);
    $('files').addEventListener('change', event => {
      addFiles(event.target.files);
      event.target.value = '';
    });
    $('drop').addEventListener('dragover', event => {
      event.preventDefault();
      if (!$('editor').disabled) $('drop').classList.add('drag');
    });
    $('drop').addEventListener('dragleave', () => $('drop').classList.remove('drag'));
    $('drop').addEventListener('drop', event => {
      event.preventDefault();
      $('drop').classList.remove('drag');
      addFiles(event.dataTransfer.files);
    });
    $('copy').addEventListener('click', async () => {
      if (!job) return;
      try {
        await navigator.clipboard.writeText(job.id);
        notice('已复制任务编号。');
      } catch { $('job-id').focus(); $('job-id').select(); notice('请复制选中的任务编号。'); }
    });
    try {
      const saved = JSON.parse(sessionStorage.getItem(sessionKey));
      if (saved && /^[a-f0-9]{64}$/.test(saved.secret) && saved.id === await jobId(saved.secret) &&
          Number.isFinite(saved.time) && typeof saved.manual === 'boolean') job = saved;
    } catch { /* Nothing to resume. */ }
    if (job) {
      if (job.manual) showManual();
      notice('正在恢复这个标签页的任务…');
      schedule(0);
    }
    refreshControls();
  }

  start();
})();
