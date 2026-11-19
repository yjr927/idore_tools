const HOME = '/sandbox/tiktok';
const START = '/auth/tiktok/start';
const CALLBACK = '/auth/tiktok/callback';
const PREVIEW_SCRIPT = HOME + '/preview.js';
export const SANDBOX_CLIP_SHA256 = '7e1eb940cf5df0df19febd86ed94d549cbf3ff9263247e85bc9c4618bc615bcb';
const KAI_PATH = '/generated/video-36dc967ec3fc7e4cd1060f94.mp4';
const KAI = {hash:'36dc967ec3fc7e4cd1060f94cf9572ee4a1a7e2def4c2bcdeb6d9648518a3b68', size:6909394};
const API = 'https://open.tiktokapis.com';
const SCOPES = ['user.info.basic', 'video.upload'];
const STATE = '__Host-idore-tt-state';
const SESSION = '__Host-idore-tt-session';
const STATE_SECONDS = 600;
const SESSION_SECONDS = 1800;
const MAX_VIDEO = 8 * 1024 * 1024;
const encoder = new TextEncoder();
const decoder = new TextDecoder();

const headers = {
  'Cache-Control': 'no-store, private',
  'Referrer-Policy': 'no-referrer',
  'X-Content-Type-Options': 'nosniff',
  'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; media-src 'self' blob:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
};

function escape(value) {
  return String(value).replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
}

function page(title, body, status = 200) {
  return new Response(`<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${escape(title)} · idore</title>
<style>body{margin:auto;max-width:40rem;padding:3rem 1.25rem;color:#211d29;background:#fcfaff;font:1rem/1.5 system-ui}h1{font-size:1.9rem;line-height:1.2}video{width:100%;max-height:22rem;background:#17131c;border-radius:.6rem}form{margin:1.25rem 0}label{display:block;margin:.75rem 0}button,.action{display:inline-block;background:#41304e;color:white;padding:.65rem 1rem;border:0;border-radius:.4rem;font:inherit;text-decoration:none;cursor:pointer}a{color:#563975}</style>
<main><small>idore Creative Studio · JJ TEST Sandbox</small><h1>${escape(title)}</h1>${body}</main></html>`, {
    status, headers: {...headers, 'Content-Type': 'text/html; charset=utf-8'},
  });
}

function problem(status, message) {
  return page('TikTok sandbox', `<p>${escape(message)}</p><p><a href="${HOME}">Return to the sandbox</a></p>`, status);
}

function redirect(to) {
  return new Response(null, {status: 303, headers: {...headers, Location: to}});
}

function json(data) {
  return Response.json(data, {headers: {'Cache-Control': 'no-store'}});
}

function base64(bytes) {
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function unbase64(text) {
  return Uint8Array.from(atob(text.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0));
}

function random() {
  return base64(crypto.getRandomValues(new Uint8Array(24)));
}

async function fingerprint(data) {
  const bytes = await crypto.subtle.digest('SHA-256', typeof data === 'string' ? encoder.encode(data) : data);
  return Array.from(new Uint8Array(bytes), b => b.toString(16).padStart(2, '0')).join('');
}

// Separate encryption domains for browser cookies and the server-only grant.
async function key(env, purpose) {
  const material = await crypto.subtle.importKey('raw', encoder.encode(env.TIKTOK_CLIENT_SECRET), 'HKDF', false, ['deriveKey']);
  return crypto.subtle.deriveKey({name:'HKDF', hash:'SHA-256', salt:encoder.encode('idore-tt-sandbox-v1'),
    info:encoder.encode(env.TIKTOK_CLIENT_KEY + ':' + purpose)}, material,
    {name:'AES-GCM', length:256}, false, ['encrypt', 'decrypt']);
}

async function seal(env, purpose, value) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const encrypted = await crypto.subtle.encrypt({name:'AES-GCM', iv}, await key(env, purpose), encoder.encode(JSON.stringify(value)));
  return base64(iv) + '.' + base64(new Uint8Array(encrypted));
}

async function unseal(env, purpose, value) {
  if (typeof value !== 'string' || value.length > 16384 || !/^[\w-]{16}\.[\w-]+$/.test(value)) return null;
  try {
    const [iv, data] = value.split('.');
    const raw = await crypto.subtle.decrypt({name:'AES-GCM', iv:unbase64(iv)}, await key(env, purpose), unbase64(data));
    const result = JSON.parse(decoder.decode(raw));
    return Number.isFinite(result.exp) && result.exp > Date.now() ? result : null;
  } catch { return null; }
}

function readCookie(request, name) {
  const entries = (request.headers.get('cookie') || '').split(';').map(s => s.trim()).filter(s => s.startsWith(name + '='));
  return entries.length === 1 ? entries[0].slice(name.length + 1) : '';
}

function cookie(response, name, value, seconds) {
  response.headers.append('Set-Cookie', `${name}=${value}; Path=/; Max-Age=${seconds}; Secure; HttpOnly; SameSite=Lax`);
  return response;
}

async function limitedBytes(body, maximum) {
  if (Number(body.headers.get('content-length') || 0) > maximum || !body.body) throw new Error('Invalid body size');
  const reader = body.body.getReader();
  let length = 0;
  const chunks = [];
  try {
    for (;;) {
      const {done, value} = await reader.read();
      if (done) break;
      length += value.length;
      if (length > maximum) { await reader.cancel(); throw new Error('Body too large'); }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
  return bytes;
}

async function provider(path, options) {
  const response = await fetch(API + path, {...options, redirect:'error', signal:AbortSignal.timeout(15000)});
  return {status:response.status, data:JSON.parse(decoder.decode(await limitedBytes(response, 64 * 1024)))};
}

function apiPost(path, token, body) {
  return provider(path, {method:'POST', headers:{Authorization:'Bearer ' + token, 'Content-Type':'application/json; charset=UTF-8'},
    body:JSON.stringify(body)});
}

function actor(env, name) {
  return env.SANDBOX_ATTEMPTS.get(env.SANDBOX_ATTEMPTS.idFromName(name));
}

async function inside(stub, path, data = {}) {
  const result = await stub.fetch(new Request('https://internal/' + path, {
    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data),
  }));
  if (!result.ok) throw new Error('Private state unavailable');
  return result.json();
}

function configured(env) {
  return Boolean(env.TIKTOK_CLIENT_KEY && env.TIKTOK_CLIENT_SECRET && env.SANDBOX_ATTEMPTS);
}

async function start(url, env) {
  const nonce = random();
  const expires = Date.now() + STATE_SECONDS * 1000;
  const authorization = new URL('https://www.tiktok.com/v2/auth/authorize/');
  authorization.search = new URLSearchParams({client_key:env.TIKTOK_CLIENT_KEY, scope:SCOPES.join(','),
    response_type:'code', redirect_uri:url.origin + CALLBACK, state:nonce}).toString();
  const value = await seal(env, STATE, {nonce, origin:url.origin, exp:expires});
  return cookie(redirect(authorization.href), STATE, value, STATE_SECONDS);
}

function validProviderValue(value, maximum) {
  return typeof value === 'string' && value.length > 0 && value.length <= maximum && /^[!-~]+$/.test(value);
}

async function callback(request, url, env) {
  const finish = response => cookie(response, STATE, '', 0);
  const auth = await unseal(env, STATE, readCookie(request, STATE));
  const params = url.searchParams;
  if (!auth || auth.origin !== url.origin || params.getAll('state').length !== 1 || params.get('state') !== auth.nonce) {
    return finish(problem(400, 'Open the sandbox and connect TikTok in this browser before returning here.'));
  }
  if (params.has('error')) return finish(problem(403, 'TikTok authorization was declined or interrupted.'));
  const code = params.get('code');
  if (params.getAll('code').length !== 1 || !validProviderValue(code, 2048)) return finish(problem(400, 'TikTok did not supply a valid authorization code.'));
  try {
    const token = await provider('/v2/oauth/token/', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'},
      body:new URLSearchParams({client_key:env.TIKTOK_CLIENT_KEY, client_secret:env.TIKTOK_CLIENT_SECRET,
        code, grant_type:'authorization_code', redirect_uri:url.origin + CALLBACK})});
    if (token.status !== 200 || !validProviderValue(token.data.access_token, 4096) || !validProviderValue(token.data.open_id, 256)) {
      return finish(problem(502, 'TikTok did not complete the token exchange. Check the Sandbox credentials and reconnect.'));
    }
    const granted = new Set(String(token.data.scope || '').split(/[\s,]+/));
    if (!SCOPES.every(s => granted.has(s))) return finish(problem(403, 'Connect again and allow basic profile access and video uploads.'));
    const profile = await provider('/v2/user/info/?fields=open_id,display_name', {headers:{Authorization:'Bearer ' + token.data.access_token}});
    const user = profile.data.data?.user;
    if (profile.status !== 200 || profile.data.error?.code !== 'ok' || user?.open_id !== token.data.open_id ||
        typeof user?.display_name !== 'string' || !user.display_name.trim() || user.display_name.length > 160) {
      return finish(problem(502, 'TikTok could not verify the connected profile. Reconnect before uploading.'));
    }
    const ttl = Math.min(SESSION_SECONDS, Math.floor(Number(token.data.expires_in)));
    if (!Number.isSafeInteger(ttl) || ttl < 1) return finish(problem(502, 'TikTok returned an expired authorization.'));
    const exp = Date.now() + ttl * 1000;
    const sid = random();
    const grant = {token:token.data.access_token, display:user.display_name,
      account:await fingerprint('account:' + env.TIKTOK_CLIENT_KEY + ':' + user.open_id), exp};
    await inside(actor(env, 'session:' + sid), 'grant', {box:await seal(env, 'grant:' + sid, grant), exp});
    const session = await seal(env, SESSION, {sid, csrf:random(), origin:url.origin, exp});
    return finish(cookie(redirect(HOME), SESSION, session, ttl));
  } catch { return finish(problem(502, 'Authorization could not be completed. Start a new connection to try again.')); }
}

async function sessionFor(request, url, env) {
  const session = await unseal(env, SESSION, readCookie(request, SESSION));
  if (!session || session.origin !== url.origin) return null;
  const stored = await inside(actor(env, 'session:' + session.sid), 'grant/read');
  const grant = await unseal(env, 'grant:' + session.sid, stored.box);
  return grant ? {session, grant} : null;
}

function receiptActor(env, grant, clip) {
  return actor(env, 'attempt:' + grant.account + ':' + clip);
}

const states = {
  reserved:'An upload was started. Check your TikTok inbox before taking further action.',
  processing:'TikTok is processing the upload. Refresh the status to see when the inbox notification is sent.',
  delivered:'Delivered to TikTok inbox. Open TikTok on your phone, tap the notification and continue editing.',
  published:'TikTok reports the creator has completed the post.',
  failed:'TikTok reports the transfer failed. No inbox delivery was confirmed.',
  uncertain:'TikTok has not confirmed delivery. Check your inbox before repeating any upload.',
  rejected:'TikTok declined the upload request. Address the issue and reconnect before trying again.',
};
const rejectionReason = {
  access_token_invalid:'The TikTok authorization expired.', scope_not_authorized:'Video upload permission was not granted.',
  spam_risk_too_many_pending_share:'TikTok reports too many pending uploads.', spam_risk_user_banned_from_posting:'TikTok restricted uploads for this account.',
  rate_limit_exceeded:'TikTok requested a slower request rate.', invalid_param:'TikTok rejected the upload parameters.',
};

function hidden(name, value) { return `<input type="hidden" name="${name}" value="${escape(value)}">`; }

// This script reads only the file the user selected. The first transfer happens
// when the user confirms their account and submits the multipart form.
const previewScript = `
const form = document.querySelector('#sandbox-upload');
if (form) {
  const picker = form.elements.video;
  const consent = form.elements.confirm;
  const button = form.querySelector('button');
  const video = form.querySelector('video');
  const hint = form.querySelector('[role="status"]');
  let objectUrl;
  let generation = 0;
  function clearPreview() {
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    objectUrl = undefined;
    video.removeAttribute('src');
    video.load();
    video.hidden = true;
    consent.checked = false;
    consent.disabled = true;
    button.disabled = true;
  }
  picker.addEventListener('change', async () => {
    const current = ++generation;
    clearPreview();
    const file = picker.files[0];
    if (!file) { hint.textContent = 'Choose the idore sandbox test clip.'; return; }
    if (file.size > ${MAX_VIDEO} || file.size < 12) { hint.textContent = 'Choose the original sandbox test clip.'; return; }
    hint.textContent = 'Checking the clip on this device…';
    try {
      const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', await file.arrayBuffer()));
      if (current !== generation) return;
      const hash = Array.from(digest, b => b.toString(16).padStart(2, '0')).join('');
      if (hash !== form.elements.clip.value) { hint.textContent = 'This file does not match the idore sandbox clip.'; return; }
      objectUrl = URL.createObjectURL(file);
      video.src = objectUrl;
      video.hidden = false;
      consent.disabled = false;
      hint.textContent = 'Preview the clip and confirm the destination account.';
    } catch {
      if (current === generation) hint.textContent = 'This browser could not read the file. Select it again.';
    }
  });
  consent.addEventListener('change', () => { button.disabled = !objectUrl || !consent.checked; });
  form.addEventListener('submit', event => {
    if (!objectUrl || !consent.checked) event.preventDefault();
    else button.disabled = true;
  });
  window.addEventListener('pagehide', () => { generation++; clearPreview(); });
}`;

async function landing(env, auth, clipHash, isKai) {
  if (!auth) return page('Test a TikTok draft', `<p>Connect your JJ TEST account to preview KAI · 手势舞 and send it to your TikTok inbox.</p><p><a class="action" href="${START}">Connect TikTok</a></p>`);
  const {session, grant} = auth;
  const row = await inside(receiptActor(env, grant, clipHash), 'read');
  const fields = hidden('csrf', session.csrf) + hidden('clip', clipHash);
  let actions = '';
  if (row.state) actions += `<p role="status">${escape(states[row.state] || states.uncertain)} ${escape(rejectionReason[row.reason] || '')}</p>`;
  if (!row.state || row.state === 'rejected' && row.sid !== session.sid) {
    if (isKai) actions += `<form method="post" action="${HOME}/upload">${fields}
      <label><input type="checkbox" name="confirm" value="inbox" required> I reviewed KAI · 手势舞 and want to send it to the TikTok inbox for ${escape(grant.display)}.</label>
      <button>Send KAI video to TikTok inbox</button></form>`;
    else actions += `<form id="sandbox-upload" method="post" enctype="multipart/form-data" action="${HOME}/upload">${fields}
      <label>Choose the local idore sandbox test clip <input type="file" name="video" accept="video/mp4,.mp4" required></label>
      <p role="status">The preview stays on this device until you choose Send.</p><video controls playsinline preload="metadata" hidden></video>
      <label><input type="checkbox" name="confirm" value="inbox" required disabled> I reviewed this clip and want to send it to the TikTok inbox for ${escape(grant.display)}.</label>
      <button disabled>Send test video to TikTok inbox</button><noscript>Enable JavaScript to preview your local clip.</noscript></form><script src="${PREVIEW_SCRIPT}" defer></script>`;
  }
  if (row.publishId && !['delivered','published','failed'].includes(row.state)) actions += `<form method="post" action="${HOME}/status">${fields}<button>Refresh TikTok status</button></form>`;
  const media = isKai ? `<h2>KAI · 手势舞</h2><video controls playsinline preload="metadata" src="${KAI_PATH}"></video>` : '';
  const alternative = isKai ? `<a href="${HOME}?clip=sample">Use the local test clip</a>` : `<a href="${HOME}">Preview KAI · 手势舞</a>`;
  const preview = page('Preview your sandbox draft', `<p>Connected TikTok account: <strong>${escape(grant.display)}</strong></p>${media}${actions}<p>${alternative} · <a href="${START}">Reconnect TikTok</a></p>`);
  // Form navigations need an Origin header for the same-origin check on POST.
  preview.headers.set('Referrer-Policy', 'same-origin');
  return preview;
}

function trustedUpload(value) {
  if (typeof value !== 'string' || value.length > 4096) return null;
  try {
    const u = new URL(value);
    if (u.protocol !== 'https:' || u.username || u.password || u.port || u.hash || u.pathname !== '/video/' ||
        !u.search || !/^(open-upload|upload\.(us|eu|sg))\.tiktokapis\.com$/.test(u.hostname)) return null;
    return u.href;
  } catch { return null; }
}

async function upload(stub, session, grant, clip) {
  const attempt = random();
  const reservation = await inside(stub, 'reserve', {sid:session.sid, attempt});
  if (!reservation.created) return;
  const update = changes => inside(stub, 'update', {attempt, ...changes});
  try {
    const size = clip.bytes.length;
    const init = await apiPost('/v2/post/publish/inbox/video/init/', grant.token, {
      source_info:{source:'FILE_UPLOAD', video_size:size, chunk_size:size, total_chunk_count:1},
    });
    const reason = init.data.error?.code;
    if ([400, 401, 403, 429].includes(init.status) && Object.hasOwn(rejectionReason, reason)) {
      await update({state:'rejected', reason}); return;
    }
    const id = init.data.data?.publish_id;
    const address = trustedUpload(init.data.data?.upload_url);
    if (init.status !== 200 || reason !== 'ok' || !validProviderValue(id, 64) || !address) {
      await update({state:'uncertain'}); return;
    }
    await update({state:'processing', publishId:id});
    const transfer = await fetch(address, {method:'PUT', body:clip.bytes, redirect:'error', signal:AbortSignal.timeout(30000),
      headers:{'Content-Type':'video/mp4','Content-Length':String(size),'Content-Range':`bytes 0-${size - 1}/${size}`}});
    if (transfer.body) await transfer.body.cancel();
    if (transfer.status !== 201) await update({state:'uncertain'});
  } catch { await update({state:'uncertain'}); }
}

async function status(stub, grant) {
  const row = await inside(stub, 'read');
  if (!row.publishId || ['delivered','published','failed'].includes(row.state)) return;
  const fetched = await apiPost('/v2/post/publish/status/fetch/', grant.token, {publish_id:row.publishId});
  if (fetched.status !== 200 || fetched.data.error?.code !== 'ok') return;
  const phase = fetched.data.data?.status;
  const next = {SEND_TO_USER_INBOX:'delivered', PUBLISH_COMPLETE:'published', FAILED:'failed'}[phase];
  if (next) await inside(stub, 'update', {attempt:row.attempt, state:next});
}

async function action(request, url, env, auth, sampleHash, kai) {
  if (request.headers.get('Origin') !== url.origin || !auth) return problem(403, 'Reconnect TikTok and use the form in this browser.');
  const isUpload = url.pathname === HOME + '/upload';
  const contentType = request.headers.get('content-type') || '';
  const kind = contentType.split(';')[0].trim().toLowerCase();
  const multipart = kind === 'multipart/form-data';
  if (kind !== 'application/x-www-form-urlencoded' && !(isUpload && multipart)) return problem(415, 'Use the sandbox review form.');
  let form;
  let body;
  try { body = await limitedBytes(request, multipart ? MAX_VIDEO + 16384 : 2048); }
  catch { return problem(413, 'The submitted form is too large or empty.'); }
  try {
    form = multipart ? await new Response(body, {headers:{'Content-Type':contentType}}).formData() : new URLSearchParams(decoder.decode(body));
  } catch { return problem(400, 'Invalid review form.'); }
  if (form.getAll('csrf').length !== 1 || form.get('csrf') !== auth.session.csrf || form.getAll('clip').length !== 1) return problem(403, 'The review session changed. Open the preview again.');
  const clipHash = form.get('clip');
  const isKai = clipHash === kai.hash;
  if (!isKai && clipHash !== sampleHash) return problem(409, 'The clip changed. Review the new preview before continuing.');
  if (isUpload && multipart === isKai) return problem(415, 'Submit the review form for the selected clip.');
  const stub = receiptActor(env, auth.grant, clipHash);
  if (isUpload) {
    if (form.getAll('confirm').length !== 1 || form.get('confirm') !== 'inbox') return problem(400, 'Review the clip and confirm the destination before sending.');
    let bytes;
    if (isKai) {
      if ([...form.keys()].some(k => !['csrf','clip','confirm'].includes(k))) return problem(400, 'Use the displayed KAI review form.');
      if (!env.ASSETS) return problem(503, 'The KAI video is unavailable.');
      const asset = await env.ASSETS.fetch(new Request(new URL(KAI_PATH, url.origin), {signal:AbortSignal.timeout(15000)}));
      if (asset.status !== 200) return problem(503, 'The KAI video is unavailable.');
      try { bytes = await limitedBytes(asset, kai.size); } catch { return problem(409, 'The stored KAI video differs from the reviewed version.'); }
      if (bytes.length !== kai.size) return problem(409, 'The stored KAI video differs from the reviewed version.');
    } else {
      const file = form.get('video');
      if (form.getAll('video').length !== 1 || !file || typeof file.arrayBuffer !== 'function' || file.size < 12) return problem(400, 'Select the original idore sandbox MP4.');
      if (file.size > MAX_VIDEO) return problem(413, 'The selected test clip is too large.');
      bytes = new Uint8Array(await file.arrayBuffer());
    }
    if (decoder.decode(bytes.subarray(4, 8)) !== 'ftyp' || await fingerprint(bytes) !== clipHash) return problem(409, 'The video does not match the reviewed version.');
    await upload(stub, auth.session, auth.grant, {bytes});
  } else await status(stub, auth.grant);
  return redirect(isKai ? HOME : HOME + '?clip=sample');
}

export function createSandboxWorker(clipHash = SANDBOX_CLIP_SHA256, kai = KAI) {
  if (!/^[a-f0-9]{64}$/.test(clipHash) || !/^[a-f0-9]{64}$/.test(kai.hash) || kai.hash === clipHash ||
      !Number.isSafeInteger(kai.size) || kai.size < 12 || kai.size > MAX_VIDEO) throw new Error('Invalid sandbox clip definition');
  return {
    async fetch(request, env) {
      const url = new URL(request.url);
      const {pathname} = url;
      if (pathname === PREVIEW_SCRIPT) {
        if (request.method !== 'GET') return new Response(null, {status:405, headers:{...headers,Allow:'GET'}});
        return new Response(previewScript, {headers:{...headers, 'Content-Type':'text/javascript; charset=utf-8'}});
      }
      if (pathname === '/sandbox-test/idore-sandbox.mp4') return new Response(null, {status:404, headers});
      if (![HOME, HOME + '/', START, CALLBACK, HOME + '/upload', HOME + '/status'].includes(pathname)) {
        return env.ASSETS ? env.ASSETS.fetch(request) : new Response('Not found', {status:404});
      }
      if (url.protocol !== 'https:') return problem(400, 'Open the secure HTTPS sandbox URL.');
      const write = pathname === HOME + '/upload' || pathname === HOME + '/status';
      const method = write ? 'POST' : 'GET';
      if (request.method !== method) { const res = problem(405, 'Unsupported request method.'); res.headers.set('Allow', method); return res; }
      if (!configured(env)) return problem(503, 'The sandbox is waiting for its TikTok developer credentials and private session storage.');
      try {
        if (pathname === START) return await start(url, env);
        if (pathname === CALLBACK) return await callback(request, url, env);
        const auth = await sessionFor(request, url, env);
        if (write) return await action(request, url, env, auth, clipHash, kai);
        const isKai = url.searchParams.get('clip') !== 'sample';
        return await landing(env, auth, isKai ? kai.hash : clipHash, isKai);
      } catch { return problem(503, 'The sandbox could not complete this request. Return to the preview to check your status.'); }
    },
  };
}

export default createSandboxWorker();

// Private service binding. Session objects expire; per-account, per-clip receipts
// retain a small deduplication record even after the browser session expires.
export class SandboxAttempt {
  constructor(ctx) { this.storage = ctx.storage; }

  async fetch(request) {
    const path = new URL(request.url).pathname;
    if (request.method !== 'POST') return new Response(null, {status:405});
    const data = await request.json();
    if (path === '/grant') {
      await this.storage.put('grant', data.box);
      await this.storage.setAlarm(data.exp);
      return json({ok:true});
    }
    if (path === '/grant/read') return json({box:await this.storage.get('grant') || null});
    if (path === '/read') return json(await this.storage.get('attempt') || {});
    if (path === '/reserve') {
      const created = await this.storage.transaction(async tx => {
        const old = await tx.get('attempt');
        if (old && !(old.state === 'rejected' && old.sid !== data.sid)) return false;
        await tx.put('attempt', {sid:data.sid, attempt:data.attempt, state:'reserved'});
        return true;
      });
      return json({created});
    }
    if (path === '/update') {
      await this.storage.transaction(async tx => {
        const row = await tx.get('attempt');
        if (!row || row.attempt !== data.attempt || ['delivered','failed','published','rejected'].includes(row.state)) return;
        row.state = data.state;
        if (data.publishId) row.publishId = data.publishId;
        if (data.reason) row.reason = data.reason;
        if (['delivered','failed','published'].includes(row.state)) delete row.publishId;
        await tx.put('attempt', row);
      });
      return json({ok:true});
    }
    return new Response(null, {status:404});
  }

  async alarm() { await this.storage.deleteAll(); }
}
