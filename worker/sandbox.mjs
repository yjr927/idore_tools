import { listFilms, readFilm } from './catalog.mjs';

const HOME = '/sandbox/tiktok';
const FEED = HOME + '/feed';
const AUTOMATION = HOME + '/automation';
const GALLERY = '/gallery.html';
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
const VIEWER = '__Host-idore-tt-viewer';
const VIEWER_SECONDS = 30 * 86400;
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

function json(data, status = 200) {
  return Response.json(data, {status, headers: {...headers, 'Vary':'Cookie'}});
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

// Only literal reasons, bounded HTTP statuses and recognized TikTok codes may
// cross into a diagnostic. Raw errors can include the provider URL or tokens.
class SafeDiagnostic extends Error {}

async function noRedirectFetch(url, options, timeoutMs) {
  let response;
  try {
    response = await fetch(url, {...options, redirect:'manual', signal:AbortSignal.timeout(timeoutMs)});
  } catch { throw new SafeDiagnostic('connection failed or timed out'); }
  if (response.status >= 300 && response.status < 400) {
    if (response.body) await response.body.cancel();
    throw new SafeDiagnostic('redirect refused (HTTP ' + response.status + ')');
  }
  return response;
}

async function provider(path, options) {
  const response = await noRedirectFetch(API + path, options, 15000);
  let bytes;
  try { bytes = await limitedBytes(response, 64 * 1024); }
  catch { throw new SafeDiagnostic('unreadable or oversized response (HTTP ' + response.status + ')'); }
  let data;
  try { data = JSON.parse(decoder.decode(bytes)); }
  catch { throw new SafeDiagnostic('non-JSON response (HTTP ' + response.status + ')'); }
  if (!data || Array.isArray(data) || typeof data !== 'object') throw new SafeDiagnostic('invalid response (HTTP ' + response.status + ')');
  return {status:response.status, data};
}

const safeAuthCodes = new Set(['invalid_request','invalid_client','invalid_grant','invalid_scope','invalid_token',
  'unauthorized_client','unsupported_grant_type','access_denied','access_token_invalid','scope_not_authorized',
  'invalid_params','rate_limit_exceeded','server_error','internal_error','temporarily_unavailable']);

function refused(result) {
  const message = result.data.error;
  const code = typeof message === 'string' ? message : message?.code;
  return new SafeDiagnostic('provider refused or returned incomplete data (HTTP ' + result.status +
    (safeAuthCodes.has(code) ? ', ' + code : '') + ')');
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
  if (!result.ok) throw new SafeDiagnostic('private storage unavailable (HTTP ' + result.status + ')');
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
  const gallery = url.searchParams.get('return') === 'gallery';
  const value = await seal(env, STATE, {nonce, origin:url.origin, gallery, exp:expires});
  return cookie(redirect(authorization.href), STATE, value, STATE_SECONDS);
}

function validProviderValue(value, maximum) {
  return typeof value === 'string' && value.length > 0 && value.length <= maximum && /^[!-~]+$/.test(value);
}

function renewableGrant(token, account) {
  const lifetime = field => Number.isSafeInteger(token[field]) && token[field] > 0 ? Date.now() + token[field] * 1000 : 0;
  const accessExp = lifetime('expires_in');
  const exp = lifetime('refresh_expires_in');
  if (!validProviderValue(token.access_token, 4096) || !validProviderValue(token.refresh_token, 4096) || !accessExp || !exp) return null;
  return {account, token:token.access_token, refresh:token.refresh_token, accessExp, exp};
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
  let stage = 'token exchange';
  try {
    const token = await provider('/v2/oauth/token/', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'},
      body:new URLSearchParams({client_key:env.TIKTOK_CLIENT_KEY, client_secret:env.TIKTOK_CLIENT_SECRET,
        code, grant_type:'authorization_code', redirect_uri:url.origin + CALLBACK}).toString()});
    if (token.status !== 200 || !validProviderValue(token.data.access_token, 4096) || !validProviderValue(token.data.open_id, 256)) {
      throw refused(token);
    }
    const granted = new Set(String(token.data.scope || '').split(/[\s,]+/));
    if (!SCOPES.every(s => granted.has(s))) return finish(problem(403, 'Connect again and allow basic profile access and video uploads.'));
    stage = 'TikTok profile verification';
    const profile = await provider('/v2/user/info/?fields=open_id,display_name', {headers:{Authorization:'Bearer ' + token.data.access_token}});
    const user = profile.data.data?.user;
    if (profile.status !== 200 || profile.data.error?.code !== 'ok' || user?.open_id !== token.data.open_id ||
        typeof user?.display_name !== 'string' || !user.display_name.trim() || user.display_name.length > 160) {
      throw refused(profile);
    }
    const ttl = Math.min(SESSION_SECONDS, Math.floor(Number(token.data.expires_in)));
    if (!Number.isSafeInteger(ttl) || ttl < 1) return finish(problem(502, 'TikTok returned an expired authorization.'));
    const exp = Date.now() + ttl * 1000;
    stage = 'private authorization sealing';
    const sid = random();
    const grant = {token:token.data.access_token, display:user.display_name,
      account:await fingerprint('account:' + env.TIKTOK_CLIENT_KEY + ':' + user.open_id), exp};
    if (auth.gallery) grant.renewal = renewableGrant(token.data, grant.account);
    const box = await seal(env, 'grant:' + sid, grant);
    stage = 'Durable Object session persistence';
    await inside(actor(env, 'session:' + sid), 'grant', {box, exp});
    stage = 'browser session cookie';
    const session = await seal(env, SESSION, {sid, csrf:random(), origin:url.origin, exp});
    const response = finish(cookie(redirect(auth.gallery ? GALLERY : HOME), SESSION, session, ttl));
    if (auth.gallery) {
      const viewer = await seal(env, VIEWER, {account:grant.account, display:grant.display, csrf:random(), origin:url.origin,
        exp:Date.now() + VIEWER_SECONDS * 1000});
      cookie(response, VIEWER, viewer, VIEWER_SECONDS);
    } else cookie(response, VIEWER, '', 0);
    return response;
  } catch (error) {
    const detail = error instanceof SafeDiagnostic ? ' — ' + error.message : '';
    return finish(problem(502, 'Sandbox authorization failed at ' + stage + detail + '. Start a new connection to retry.'));
  }
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

function publicReceipt(row) {
  if (!Object.hasOwn(states, row.state)) return null;
  const result = {state:row.state};
  for (const field of ['startedAt','acceptedAt','deliveredAt','publishedAt','failedAt','updatedAt']) {
    if (typeof row[field] === 'string') result[field] = row[field];
  }
  return result;
}

function visibleSettings(config) {
  if (!config) return {enabled:false};
  return {enabled:config.enabled, startedAt:config.startedAt, problem:config.problem || null};
}

async function galleryRequest(request, url, env, auth) {
  const known = await unseal(env, VIEWER, readCookie(request, VIEWER));
  const viewer = known?.origin === url.origin && (!auth || auth.grant.account === known.account) ? known :
    auth ? {account:auth.grant.account, display:auth.grant.display, csrf:auth.session.csrf} : null;
  if (!viewer) return url.pathname === FEED ? json({connected:false, receipts:{}}) : json({error:'reconnect_required'}, 401);
  const coordinator = actor(env, 'automation:' + viewer.account);
  if (url.pathname === FEED) {
    const value = url.searchParams.get('clips') || '';
    const hashes = [...new Set(value ? value.split(',') : [])];
    if (hashes.length > 20 || hashes.some(h => !/^[a-f0-9]{64}$/.test(h))) return json({error:'invalid_clips'}, 400);
    const automation = await inside(coordinator, 'automation/read');
    const rows = await Promise.all(hashes.map(async h => [h, publicReceipt(await inside(receiptActor(env, viewer, h), 'read'))]));
    return json({connected:true, display:viewer.display, csrf:viewer.csrf, canEnable:Boolean(auth?.grant.renewal),
      automation, receipts:Object.fromEntries(rows.filter(([,row]) => row))});
  }
  if (request.headers.get('Origin') !== url.origin) return json({error:'invalid_origin'}, 403);
  if (request.headers.get('content-type')?.split(';')[0] !== 'application/x-www-form-urlencoded') return json({error:'invalid_form'}, 415);
  let fields;
  try { fields = new URLSearchParams(decoder.decode(await limitedBytes(request, 1024))); }
  catch { return json({error:'invalid_form'}, 400); }
  if (fields.getAll('csrf').length !== 1 || fields.get('csrf') !== viewer.csrf) return json({error:'invalid_csrf'}, 403);
  const toggle = fields.get('enabled');
  if (fields.getAll('enabled').length !== 1 || !['0','1'].includes(toggle)) return json({error:'invalid_form'}, 400);
  if (toggle === '0') return json(await inside(coordinator, 'automation/disable'));
  if (!auth?.grant.renewal) return json({error:'reconnect_required'}, 401);
  const box = await seal(env, 'automation:' + viewer.account, auth.grant.renewal);
  const result = await inside(coordinator, 'automation/enable', {account:viewer.account, origin:url.origin, box});
  return json(result, result.error ? 503 : 200);
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
  sandbox_user_not_authorized:'TikTok did not recognize this account as a Sandbox target.',
  sandbox_restriction:'TikTok restricted the operation in this Sandbox.',
  unaudited_client_can_only_post_to_private_accounts:'TikTok restricted this unaudited client to private accounts.',
  reached_active_user_cap:'TikTok reports that the app reached its active user limit.',
  client_not_authorized:'TikTok did not authorize this client for the upload.',
  invalid_scope:'TikTok rejected the scope.', permission_denied:'TikTok denied the requested permission.',
  url_ownership_unverified:'TikTok could not verify URL ownership.',
};

function canReserve(row, sid, kai, automatic = false) {
  if (!row || Object.keys(row).length === 0) return true;
  if (automatic) return row.state === 'rejected' && row.stage === 'init_only' && !row.publishId &&
    ['rate_limit_exceeded','spam_risk_too_many_pending_share'].includes(row.reason) && Date.parse(row.retryAt) <= Date.now();
  if (row.retryCount || row.publishId) return false;
  // Only the previous implementation could persist an unstaged uncertain
  // receipt. It committed publishId before the first PUT and removed that ID
  // only on a terminal state, so this specific receipt proves no bytes left.
  if (kai && row.state === 'uncertain' && !Object.hasOwn(row, 'stage')) return true;
  return row.state === 'rejected' && row.sid !== sid;
}

function errorCode(data, token) {
  const code = typeof data.error === 'string' ? data.error : data.error?.code;
  return typeof code === 'string' && code !== token && code !== 'ok' && /^[a-z_]{1,70}$/.test(code) ? code : '';
}

function errorDetail(result, code) {
  return 'TikTok HTTP ' + result.status + (code ? ', code: ' + code : '') + '.';
}

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
  if (!auth) return page('Test a TikTok draft', `<p>Connect your JJ TEST account to preview KAI · 手势舞 and send it to your TikTok inbox.</p><p><a class="action" href="${START}">Connect TikTok</a></p><p><a href="${GALLERY}">Agent 成片库与自动投递设置</a></p>`);
  const {session, grant} = auth;
  const row = await inside(receiptActor(env, grant, clipHash), 'read');
  const fields = hidden('csrf', session.csrf) + hidden('clip', clipHash);
  let actions = '';
  if (row.state) {
    const beforeTransfer = !['delivered','failed','published'].includes(row.state) &&
      (row.stage === 'init_only' || row.state === 'uncertain' && !row.publishId && !row.stage);
    const message = beforeTransfer ? 'Upload initialization. No video bytes were sent.' : states[row.state] || states.uncertain;
    const reason = Object.hasOwn(rejectionReason, row.reason) ? rejectionReason[row.reason] : '';
    actions += `<p role="status">${escape(message)} ${escape(row.detail || '')} ${escape(reason)} ${escape(row.checkDetail || '')}</p>`;
  }
  if (canReserve(row, session.sid, isKai)) {
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
  const preview = page('Preview your sandbox draft', `<p>Connected TikTok account: <strong>${escape(grant.display)}</strong></p>${media}${actions}<p><a href="${GALLERY}">Agent 成片库与自动投递设置</a></p><p>${alternative} · <a href="${START}">Reconnect TikTok</a></p>`);
  // Form navigations need an Origin header for the same-origin check on POST.
  preview.headers.set('Referrer-Policy', 'same-origin');
  return preview;
}

function uploadAddress(value) {
  if (typeof value !== 'string' || value.length > 8192) return {issue:'TikTok did not return a usable transfer address.'};
  try {
    const u = new URL(value);
    // Only a dedicated TikTok upload endpoint supplied directly by the official
    // initialization response. Both /video/ and /upload/ appear in TikTok's
    // transfer documentation. Never follow redirects or forward bearer tokens.
    const owned = /^(?:open-upload|upload)(?:-[a-z0-9]{1,15})?(?:\.[a-z0-9-]{1,20})?\.tiktokapis\.com$/.test(u.hostname);
    if (!owned) return {issue:'TikTok returned an unexpected transfer host.'};
    if (u.protocol !== 'https:' || u.username || u.password || u.port || u.hash || !/^\/(video|upload)\/?$/.test(u.pathname) || !u.search) {
      return {issue:'Unsupported signed transfer address on ' + u.hostname + '.'};
    }
    return {href:u.href};
  } catch { return {issue:'TikTok returned a malformed transfer address.'}; }
}

async function upload(stub, session, grant, clip, isKai, automatic = false) {
  const attempt = random();
  const reservation = await inside(stub, 'reserve', {sid:session.sid, attempt, kai:isKai, automatic});
  if (!reservation.created) return;
  const update = changes => inside(stub, 'update', {attempt, ...changes});
  let stage = 'init_only';
  try {
    const size = clip.bytes.length;
    const init = await apiPost('/v2/post/publish/inbox/video/init/', grant.token, {
      source_info:{source:'FILE_UPLOAD', video_size:size, chunk_size:size, total_chunk_count:1},
    });
    const id = init.data.data?.publish_id;
    const publishId = validProviderValue(id, 256) ? id : undefined;
    const reason = errorCode(init.data, grant.token);
    if (init.status !== 200 || init.data.error?.code !== 'ok') {
      const rejected = !publishId && reason && [200,400,401,403,429].includes(init.status);
      await update({state:rejected ? 'rejected' : 'uncertain', stage, reason, publishId,
        detail:'TikTok declined or could not confirm initialization. ' + errorDetail(init, reason)}); return;
    }
    if (!publishId) {
      await update({state:'uncertain', stage, detail:'TikTok returned no usable status identifier. ' + errorDetail(init, reason)}); return;
    }
    const address = uploadAddress(init.data.data?.upload_url);
    if (!address.href) {
      await update({state:'uncertain', stage, publishId, detail:address.issue + ' ' + errorDetail(init, reason)}); return;
    }
    // This durable write must finish before any bytes can leave the Worker.
    const permission = await update({state:'processing', stage:'transfer_started', publishId});
    if (!permission.applied) return;
    stage = 'transfer_started';
    const transfer = await noRedirectFetch(address.href, {method:'PUT', body:clip.bytes,
      headers:{'Content-Type':'video/mp4','Content-Length':String(size),'Content-Range':`bytes 0-${size - 1}/${size}`}}, 30000);
    if (transfer.body) await transfer.body.cancel();
    if (transfer.status !== 201) await update({state:'uncertain', stage, detail:'TikTok transfer returned HTTP ' + transfer.status + '.'});
    else await update({state:'processing', stage:'transfer_accepted', detail:'TikTok accepted the video bytes (HTTP 201).'});
  } catch (error) {
    const detail = error instanceof SafeDiagnostic ? error.message : 'request interrupted';
    await update({state:'uncertain', stage, detail:(stage === 'init_only' ? 'TikTok initialization: ' : 'TikTok transfer: ') + detail + '.'});
  }
}

async function status(stub, grant, followDelivery = false) {
  const row = await inside(stub, 'read');
  if (!row.publishId || ['published','failed'].includes(row.state) || row.state === 'delivered' && !followDelivery) return;
  const update = changes => inside(stub, 'update', {attempt:row.attempt, ...changes});
  let fetched;
  try { fetched = await apiPost('/v2/post/publish/status/fetch/', grant.token, {publish_id:row.publishId}); }
  catch (error) { await update({checkDetail:'Status check: ' + (error instanceof SafeDiagnostic ? error.message : 'request interrupted') + '.'}); return; }
  if (fetched.status !== 200 || fetched.data.error?.code !== 'ok') {
    await update({checkDetail:'Status check: ' + errorDetail(fetched, errorCode(fetched.data, grant.token))}); return;
  }
  const phase = fetched.data.data?.status;
  const next = {SEND_TO_USER_INBOX:'delivered', PUBLISH_COMPLETE:'published', FAILED:'failed'}[phase];
  const fail = next === 'failed' ? errorCode({error:fetched.data.data?.fail_reason}, grant.token) : '';
  const inProgress = ['PROCESSING_UPLOAD','PROCESSING_DOWNLOAD'].includes(phase) ? phase : '';
  await update(next ? {state:next, checkDetail:fail ? 'TikTok failure: ' + fail + '.' : ''} :
    {checkDetail:inProgress ? 'TikTok status: ' + inProgress + '.' : 'TikTok has not provided a final status.'});
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
    await upload(stub, auth.session, auth.grant, {bytes}, isKai);
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
      if (![HOME, HOME + '/', START, CALLBACK, FEED, AUTOMATION, HOME + '/upload', HOME + '/status'].includes(pathname)) {
        return env.ASSETS ? env.ASSETS.fetch(request) : new Response('Not found', {status:404});
      }
      if (url.protocol !== 'https:') return problem(400, 'Open the secure HTTPS sandbox URL.');
      const write = pathname === HOME + '/upload' || pathname === HOME + '/status';
      const method = write || pathname === AUTOMATION ? 'POST' : 'GET';
      if (request.method !== method) { const res = problem(405, 'Unsupported request method.'); res.headers.set('Allow', method); return res; }
      if (!configured(env)) return problem(503, 'The sandbox is waiting for its TikTok developer credentials and private session storage.');
      try {
        if (pathname === START) return await start(url, env);
        if (pathname === CALLBACK) return await callback(request, url, env);
        const auth = await sessionFor(request, url, env);
        if (pathname === FEED || pathname === AUTOMATION) return await galleryRequest(request, url, env, auth);
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
  constructor(ctx, env) {
    this.storage = ctx.storage;
    this.env = env;
    this.queue = Promise.resolve();
  }

  // A pause or reconnection cannot interleave with a credential rotation or a
  // new send. Each media receipt separately reserves its upload transactionally.
  serialized(work) {
    const result = this.queue.then(work);
    this.queue = result.catch(() => {});
    return result;
  }

  async disable(config, problem = null) {
    if (config) {
      config.enabled = false;
      config.problem = problem;
      delete config.box;
      await this.storage.put('automation', config);
    }
    await this.storage.deleteAlarm();
    return visibleSettings(config);
  }

  async enable(data) {
    const old = await this.storage.get('automation');
    if (old?.enabled) {
      await this.storage.setAlarm(Date.now() + 1000);
      return visibleSettings(old);
    }
    const startedAt = new Date().toISOString();
    let films;
    try { films = await listFilms(this.env, data.origin); }
    catch { return {error:'catalog_unavailable'}; }
    // Usually empty. Snapshot any pre-existing oddly future-dated entries as
    // well as setting the normal time cutoff so enabling never sends history.
    const baseline = films.filter(f => f.autoEligible && Date.parse(f.generatedAt) > Date.parse(startedAt)).map(f => f.hash);
    const config = {enabled:true, account:data.account, origin:data.origin, box:data.box, startedAt, baseline};
    await this.storage.setAlarm(Date.now() + 1000);
    await this.storage.put('automation', config);
    return visibleSettings(config);
  }

  async credentials(config) {
    const purpose = 'automation:' + config.account;
    const saved = await unseal(this.env, purpose, config.box);
    if (!saved || saved.account !== config.account) {
      await this.disable(config, 'reconnect_required'); return null;
    }
    if (saved.accessExp > Date.now() + 2 * 60000) return saved;
    const result = await provider('/v2/oauth/token/', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'},
      body:new URLSearchParams({client_key:this.env.TIKTOK_CLIENT_KEY, client_secret:this.env.TIKTOK_CLIENT_SECRET,
        grant_type:'refresh_token', refresh_token:saved.refresh}).toString()});
    if (result.status !== 200 || result.data.error) {
      if ([400,401,403].includes(result.status) && ['invalid_grant','access_denied','invalid_token'].includes(result.data.error)) {
        await this.disable(config, 'reconnect_required'); return null;
      }
      throw new SafeDiagnostic('temporary_failure');
    }
    const returnedAccount = await fingerprint('account:' + this.env.TIKTOK_CLIENT_KEY + ':' + result.data.open_id);
    const permissions = new Set(String(result.data.scope || '').split(/[\s,]+/));
    const next = renewableGrant(result.data, returnedAccount);
    if (returnedAccount !== config.account || !permissions.has('video.upload') || !next) {
      await this.disable(config, 'reconnect_required'); return null;
    }
    config.box = await seal(this.env, purpose, next);
    await this.storage.put('automation', config);
    return next;
  }

  async pollDeliveries(config, grant) {
    const watches = await this.storage.get('watches') || {};
    const due = Object.entries(watches).filter(([,at]) => at <= Date.now()).sort((a,b) => a[1] - b[1]).slice(0, 2);
    for (const [hash] of due) {
      const stub = receiptActor(this.env, grant, hash);
      await status(stub, grant, true);
      const row = await inside(stub, 'read');
      if (!row.state) {
        // A crash before a reservation leaves no external side effect.
        await this.storage.delete('handled:' + hash);
        delete watches[hash];
      } else if (row.publishId && !['failed','published'].includes(row.state) &&
          !(row.state === 'delivered' && Date.parse(row.deliveredAt) + 30 * 86400000 < Date.now())) {
        watches[hash] = Date.now() + (row.state === 'delivered' ? 6 * 3600000 : 2 * 60000);
      } else {
        delete watches[hash];
      }
    }
    await this.storage.put('watches', watches);
  }

  async nextFilm(config, films) {
    const cutoff = Date.parse(config.startedAt);
    const skipped = new Set(config.baseline);
    const candidates = films.filter(f => f.autoEligible && Date.parse(f.generatedAt) > cutoff &&
      Date.parse(f.generatedAt) <= Date.now() && !skipped.has(f.hash));
    candidates.sort((a,b) => Date.parse(a.generatedAt) - Date.parse(b.generatedAt));
    for (const film of candidates) {
      const handled = await this.storage.get('handled:' + film.hash);
      if (!handled || typeof handled === 'number' && handled <= Date.now()) return film;
    }
    return null;
  }

  async deliver(config, grant, film) {
    const stub = receiptActor(this.env, grant, film.hash);
    const existing = await inside(stub, 'read');
    if (canReserve(existing, 'agent', false, true)) {
      let bytes;
      try { bytes = await readFilm(this.env, config.origin, film); }
      catch {
        await this.storage.put('handled:' + film.hash, Date.now() + 60 * 60000);
        return 'media_unavailable';
      }
      // Mark for polling before init so a process restart cannot lose the
      // provider receipt. The account/digest reservation prevents repeat PUTs.
      const watches = await this.storage.get('watches') || {};
      watches[film.hash] = Date.now();
      await this.storage.put('watches', watches);
      await upload(stub, {sid:'agent'}, grant, {bytes}, false, true);
    }
    const row = await inside(stub, 'read');
    if (row.state === 'rejected' && ['access_token_invalid','scope_not_authorized','invalid_scope'].includes(row.reason)) {
      await this.disable(config, 'reconnect_required');
      return 'reconnect_required';
    }
    if (row.publishId) {
      const watches = await this.storage.get('watches') || {};
      watches[film.hash] = Date.now();
      await this.storage.put('watches', watches);
    }
    const retry = row.state === 'rejected' && Date.parse(row.retryAt);
    await this.storage.put('handled:' + film.hash, retry || true);
    if (retry) { config.cooldownUntil = retry; return 'quota_limited'; }
    if (row.state === 'uncertain' || row.state === 'reserved') return 'delivery_uncertain';
    if (row.state === 'rejected' || row.state === 'failed') return 'provider_rejected';
    return null;
  }

  async run(config) {
    // Arm the next scan first so termination, checkout failures, or provider
    // timeouts do not strand an enabled account. Requires no open web page.
    await this.storage.setAlarm(Date.now() + 2 * 60000);
    try {
      const grant = await this.credentials(config);
      if (!grant) return;
      await this.pollDeliveries(config, grant);
      if (config.cooldownUntil > Date.now()) return;
      let films;
      try { films = await listFilms(this.env, config.origin); }
      catch { config.problem = 'catalog_unavailable'; await this.storage.put('automation', config); return; }
      const film = await this.nextFilm(config, films);
      if (film) config.problem = await this.deliver(config, grant, film);
      else if (['catalog_unavailable','quota_limited','temporary_failure'].includes(config.problem)) config.problem = null;
    } catch { config.problem = 'temporary_failure'; }
    await this.storage.put('automation', config);
  }

  async fetch(request) {
    const path = new URL(request.url).pathname;
    if (request.method !== 'POST') return new Response(null, {status:405});
    const data = await request.json();
    if (path === '/automation/read') return json(visibleSettings(await this.storage.get('automation')));
    if (path === '/automation/enable') return json(await this.serialized(() => this.enable(data)));
    if (path === '/automation/disable') return json(await this.serialized(async () => this.disable(await this.storage.get('automation'))));
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
        if (!canReserve(old, data.sid, data.kai === true, data.automatic === true)) return false;
        const now = new Date().toISOString();
        await tx.put('attempt', {sid:data.sid, attempt:data.attempt, state:'reserved', stage:'init_only', retryCount:old ? 1 : 0,
          startedAt:now, updatedAt:now});
        return true;
      });
      return json({created});
    }
    if (path === '/update') {
      const applied = await this.storage.transaction(async tx => {
        const row = await tx.get('attempt');
        if (!row || row.attempt !== data.attempt || ['failed','published','rejected'].includes(row.state) ||
            row.state === 'delivered' && data.state !== 'published') return false;
        const now = new Date().toISOString();
        if (data.stage === 'transfer_accepted' && !row.acceptedAt) row.acceptedAt = now;
        if (data.state === 'delivered' && !row.deliveredAt) row.deliveredAt = now;
        if (data.state === 'published' && !row.publishedAt) row.publishedAt = now;
        if (['rejected','failed'].includes(data.state) && !row.failedAt) row.failedAt = now;
        row.updatedAt = now;
        if (data.state) row.state = data.state;
        if (data.stage && !(data.stage === 'init_only' && row.stage === 'transfer_started')) row.stage = data.stage;
        if (data.publishId) row.publishId = data.publishId;
        if (data.reason) row.reason = data.reason;
        if (data.detail) row.detail = data.detail;
        if (data.checkDetail !== undefined) row.checkDetail = data.checkDetail;
        if (data.state === 'rejected' && ['rate_limit_exceeded','spam_risk_too_many_pending_share'].includes(data.reason)) {
          row.retryAt = new Date(Date.now() + (data.reason === 'rate_limit_exceeded' ? 1 : 24) * 3600000).toISOString();
        }
        if (['failed','published'].includes(row.state)) delete row.publishId;
        await tx.put('attempt', row);
        return true;
      });
      return json({applied});
    }
    return new Response(null, {status:404});
  }

  async alarm() {
    await this.serialized(async () => {
      const config = await this.storage.get('automation');
      if (config?.enabled) await this.run(config);
      else if (!config) await this.storage.deleteAll(); // Expired OAuth session.
    });
  }
}
