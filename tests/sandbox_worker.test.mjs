import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import deployedWorker, { createSandboxWorker, SandboxAttempt } from '../worker/sandbox.mjs';

const origin = 'https://idore-tools.yjr927.workers.dev';
const landing = '/sandbox/tiktok';
const samplePage = landing + '?clip=sample';
const kaiPath = '/generated/video-36dc967ec3fc7e4cd1060f94.mp4';
const kaiDigest = '36dc967ec3fc7e4cd1060f94cf9572ee4a1a7e2def4c2bcdeb6d9648518a3b68';
const kaiSize = 6909394;
const callback = '/auth/tiktok/callback';
const initPath = '/v2/post/publish/inbox/video/init/';
const statusPath = '/v2/post/publish/status/fetch/';
const clip = Buffer.from('000000186674797069736f6d0000020069736f6d69736f32', 'hex');
const clipHash = createHash('sha256').update(clip).digest('hex');
const accessToken = 'fake-server-only-token-1259';
const clientSecret = 'fake-sandbox-client-secret-used-in-tests';
const uploadUrl = 'https://open-upload.tiktokapis.com/video/?upload_id=demo&upload_token=test-private-capability';
const openId = 'private-sandbox-open-id';
const mockRedirect = 'https://redirect.example/private-target';

// The transaction queue also models concurrently arriving requests to one object.
class MemoryStorage {
  entries = new Map();
  queue = Promise.resolve();
  async get(key) { return structuredClone(this.entries.get(key)); }
  async put(key, value) { this.entries.set(key, structuredClone(value)); }
  async delete(key) { return this.entries.delete(key); }
  async deleteAll() { this.entries.clear(); }
  async setAlarm(at) { this.alarmAt = at; }
  async deleteAlarm() { this.alarmAt = null; }
  transaction(fn) {
    const result = this.queue.then(() => fn(this));
    this.queue = result.catch(() => {});
    return result;
  }
}

function fixture(t, { scope = 'video.upload,user.info.basic', dropInitResponse = false, useDeployment = false,
  kaiAsset, redirectOn, initReply, signedUploadUrl = uploadUrl, onPut, archive, refresh, tokenTTL = 86400,
  statusReply } = {}) {
  const worker = useDeployment ? deployedWorker : createSandboxWorker(clipHash);
  const calls = [];
  const objects = new Map();
  const env = {
    TIKTOK_CLIENT_KEY: 'sandbox-client-key',
    TIKTOK_CLIENT_SECRET: clientSecret,
    ASSETS: { async fetch(request) {
      if (new URL(request.url).pathname !== kaiPath) return new Response('unrelated asset');
      const bytes = kaiAsset ?? await readFile(new URL('../site' + kaiPath, import.meta.url));
      return new Response(bytes, { headers: { 'Content-Type': 'video/mp4', 'Content-Length': String(bytes.length) } });
    } },
    SANDBOX_ATTEMPTS: {
      idFromName: name => name,
      get(id) {
        if (!objects.has(id)) {
          const storage = new MemoryStorage();
          const instance = new SandboxAttempt({ storage }, env);
          objects.set(id, { storage, alarm: () => instance.alarm(), fetch: (input, options) => instance.fetch(new Request(input, options)) });
        }
        return objects.get(id);
      },
    },
  };
  const ok = data => Response.json({ data, error: { code: 'ok' } });
  t.mock.method(globalThis, 'fetch', async (input, options) => {
    const req = new Request(input, options);
    const url = new URL(req.url);
    const bytes = new Uint8Array(await req.arrayBuffer());
    const text = req.method === 'PUT' ? '' : new TextDecoder().decode(bytes);
    calls.push({ url, method: req.method, headers: req.headers, bytes, text, redirect: req.redirect });
    // workerd accepts manual/follow. These credentialed requests must use manual.
    assert.equal(req.redirect, 'manual');
    if (url.origin === 'https://yjr927.github.io' && archive) {
      assert.equal(req.headers.get('Authorization'), null);
      if (url.pathname === '/idore_tools/data/creations.json') return Response.json({schema_version:1, items:archive.items});
      if (url.pathname === '/idore_tools/generated/video-' + clipHash.slice(0,24) + '.mp4') return new Response(archive.bytes || clip);
      throw new Error('unexpected archive URL');
    }
    if (redirectOn === url.pathname) {
      return new Response('private provider diagnostic', { status: 302, headers: { Location: mockRedirect } });
    }
    if (req.url === signedUploadUrl && req.method === 'PUT') return onPut ? onPut(objects) : new Response(null, { status: 201 });
    assert.equal(url.origin, 'https://open.tiktokapis.com');
    switch (url.pathname) {
      case '/v2/oauth/token/':
        if (new URLSearchParams(text).get('grant_type') === 'refresh_token' && refresh) return refresh();
        return Response.json({ access_token: accessToken, token_type: 'Bearer', open_id: openId,
          refresh_token: 'fake-ignored-refresh-token', expires_in:tokenTTL, refresh_expires_in:86400 * 100, scope });
      case '/v2/user/info/':
        return ok({ user: { open_id: openId, display_name: 'Sandbox Tester' } });
      case initPath:
        if (dropInitResponse) throw new Error('Mocked lost initialization response');
        return initReply ? initReply() : ok({ publish_id: 'v_inbox_file~v2.12345', upload_url: signedUploadUrl });
      case statusPath:
        return ok({ status: statusReply ? statusReply() : 'SEND_TO_USER_INBOX' });
      default:
        throw new Error('Unexpected mock endpoint ' + url.pathname);
    }
  });
  const cookies = new Map();
  async function visit(path, { method = 'GET', form, requestOrigin = origin } = {}) {
    const headers = new Headers();
    if (cookies.size) headers.set('Cookie', [...cookies].map(([k, v]) => k + '=' + v).join('; '));
    if (method === 'POST') headers.set('Origin', requestOrigin);
    const req = new Request(new URL(path, origin), {
      method, headers, body: form,
    });
    const res = await worker.fetch(req, env);
    for (const raw of res.headers.getSetCookie()) {
      const [pair] = raw.split(';');
      const i = pair.indexOf('=');
      const name = pair.slice(0, i);
      const value = pair.slice(i + 1);
      if (!value || /Max-Age=0\b/i.test(raw)) cookies.delete(name);
      else cookies.set(name, value);
    }
    return res;
  }
  async function start(gallery = false) {
    const res = await visit('/auth/tiktok/start' + (gallery ? '?return=gallery' : ''));
    assert.ok([302, 303, 307].includes(res.status));
    const authorization = new URL(res.headers.get('Location'));
    assert.equal(authorization.origin, 'https://www.tiktok.com');
    assert.equal(authorization.searchParams.get('redirect_uri'), origin + callback);
    assert.deepEqual(new Set(authorization.searchParams.get('scope').split(',')), new Set(['user.info.basic', 'video.upload']));
    const stateCookie = res.headers.getSetCookie().find(c => c.startsWith('__Host-idore-tt-state='));
    assert.match(stateCookie, /HttpOnly/i);
    assert.match(stateCookie, /Secure/i);
    assert.match(stateCookie, /SameSite=Lax/i);
    const state = authorization.searchParams.get('state');
    assert.ok(state);
    return state;
  }
  async function complete(state) {
    return visit(callback + '?' + new URLSearchParams({ code: 'fake-test-auth-code', state }));
  }
  async function session(source = 'sample') {
    const auth = await complete(await start());
    assert.equal(auth.status, 303);
    assert.equal(new URL(auth.headers.get('Location'), origin).pathname, landing);
    const preview = await visit(source === 'sample' ? samplePage : landing);
    assert.equal(preview.headers.get('Referrer-Policy'), 'same-origin');
    const html = await preview.text();
    assert.match(html, /Sandbox Tester/);
    const inputs = [...html.matchAll(/<input\b[^>]*>/gi)].map(([tag]) =>
      Object.fromEntries([...tag.matchAll(/([\w-]+)\s*=\s*["']([^"']*)["']/g)].map(([, k, v]) => [k, v])));
    const field = name => inputs.find(input => input.name === name)?.value;
    assert.ok(field('csrf'));
    assert.match(field('clip'), /^[0-9a-f]{64}$/);
    if (source === 'sample') {
      assert.match(html, /enctype="multipart\/form-data"/i);
      assert.equal(inputs.find(input => input.name === 'video')?.type, 'file');
      assert.doesNotMatch(html, /src=["'][^"']+\.mp4/i);
      if (!useDeployment) assert.equal(field('clip'), clipHash);
    } else {
      assert.match(html, /KAI/);
      assert.ok(html.includes(kaiPath));
      assert.equal(field('clip'), kaiDigest);
      assert.equal(inputs.some(input => input.type === 'file'), false);
    }
    return { csrf: field('csrf'), clip: field('clip'), confirm: 'inbox' };
  }
  function post(route, fields, requestOrigin, bytes = clip) {
    const form = route === 'upload' ? new FormData() : new URLSearchParams();
    for (const [key, value] of Object.entries(fields)) form.set(key, value);
    if (route === 'upload' && bytes !== null) form.append('video', new Blob([bytes], { type: 'video/mp4' }), 'test.mp4');
    return visit(landing + '/' + route, { method: 'POST', form, requestOrigin });
  }
  const callsTo = path => calls.filter(c => c.url.pathname === path);
  return { env, objects, calls, cookies, callsTo, visit, start, complete, session, post };
}

test('unconfigured sandbox and invalid OAuth state make no upstream requests', async t => {
  const h = fixture(t);
  delete h.env.TIKTOK_CLIENT_SECRET;
  assert.ok((await h.visit('/auth/tiktok/start')).status >= 400);
  h.env.TIKTOK_CLIENT_SECRET = clientSecret;
  assert.ok((await h.complete('absent-cookie')).status >= 400);
  await h.start();
  assert.ok((await h.complete('incorrect-state')).status >= 400);
  assert.equal(h.calls.length, 0);
  assert.equal(await (await h.visit('/existing-page')).text(), 'unrelated asset');
});

test('upload permission is required before linking a test account', async t => {
  const h = fixture(t, { scope: 'user.info.basic' });
  await h.complete(await h.start());
  assert.equal(h.callsTo('/v2/oauth/token/').length, 1);
  assert.equal(h.cookies.has('__Host-idore-tt-session'), false);
  assert.equal(h.callsTo(initPath).length, 0);
});

test('token exchange refuses redirects without contacting their destination', async t => {
  const h = fixture(t, { redirectOn: '/v2/oauth/token/' });
  const response = await h.complete(await h.start());
  assert.equal(response.status, 502);
  assert.equal(response.headers.get('Location'), null);
  assert.equal(h.cookies.has('__Host-idore-tt-session'), false);
  assert.deepEqual(h.calls.map(call => [call.url.href, call.redirect]), [
    ['https://open.tiktokapis.com/v2/oauth/token/', 'manual'],
  ]);
  const message = await response.text();
  assert.match(message, /token exchange/i);
  for (const hidden of [mockRedirect, 'private provider diagnostic', clientSecret, 'fake-test-auth-code']) {
    assert.ok(!message.includes(hidden));
  }
});

test('expired login state is rejected before exchanging the authorization code', async t => {
  const h = fixture(t);
  const state = await h.start();
  const expired = Date.now() + 2 * 24 * 60 * 60 * 1000;
  t.mock.method(Date, 'now', () => expired);
  assert.ok((await h.complete(state)).status >= 400);
  assert.equal(h.callsTo('/v2/oauth/token/').length, 0);
});

test('upload requires a connected session, same-origin confirmation and exact reviewed clip', async t => {
  const h = fixture(t);
  const anonymous = await h.post('upload', { csrf: 'unused', clip: clipHash, confirm: 'inbox' });
  assert.notEqual(anonymous.status, 200);
  const form = await h.session();
  assert.equal((await h.post('upload', form, 'https://elsewhere.example')).status, 403);
  assert.equal((await h.post('upload', { ...form, csrf: 'invalid' })).status, 403);
  assert.equal((await h.post('upload', { ...form, confirm: '' })).status, 400);
  assert.equal((await h.post('upload', { ...form, clip: 'f'.repeat(64) })).status, 409);
  const wrongBytes = Buffer.from(clip);
  wrongBytes[wrongBytes.length - 1] ^= 1;
  assert.equal((await h.post('upload', form, origin, wrongBytes)).status, 409);
  assert.equal((await h.post('upload', form, origin, null)).status, 400);
  assert.equal((await h.visit(landing + '/upload', { method: 'POST', form: new URLSearchParams(form) })).status, 415);
  assert.equal(h.callsTo(initPath).length, 0);
  assert.equal(h.calls.some(r => r.method === 'PUT'), false);
});

test('deployed worker accepts only the pinned local media', async t => {
  const h = fixture(t, { useDeployment: true });
  assert.equal((await h.visit('/sandbox-test/idore-sandbox.mp4')).status, 404);
  const form = await h.session();
  assert.equal(form.clip, '7e1eb940cf5df0df19febd86ed94d549cbf3ff9263247e85bc9c4618bc615bcb');
  assert.equal((await h.post('upload', form)).status, 409);
  assert.equal(h.callsTo(initPath).length, 0);

  // The optional local check exercises the actual clip without publishing it as a fixture.
  if (process.env.IDORE_TEST_MP4) {
    const media = await readFile(process.env.IDORE_TEST_MP4);
    assert.equal(createHash('sha256').update(media).digest('hex'), form.clip);
    assert.equal((await h.post('upload', form, origin, media)).status, 303);
    assert.equal(h.callsTo(initPath).length, 1);
  }
});

test('confirmed clip reaches the inbox once and credentials stay private', async t => {
  const h = fixture(t);
  const form = await h.session();
  const exchange = h.callsTo('/v2/oauth/token/')[0];
  const body = new URLSearchParams(exchange.text);
  assert.equal(exchange.method, 'POST');
  assert.equal(body.get('redirect_uri'), origin + callback);
  assert.equal(body.get('client_secret'), clientSecret);
  assert.equal(body.get('grant_type'), 'authorization_code');
  const uploads = await Promise.all([h.post('upload', form), h.post('upload', form)]);
  assert.ok(uploads.some(r => r.status === 303));
  await h.post('status', form);
  const init = h.callsTo(initPath);
  assert.equal(init.length, 1);
  assert.equal(init[0].headers.get('Authorization'), 'Bearer ' + accessToken);
  assert.deepEqual(JSON.parse(init[0].text).source_info, {
    source: 'FILE_UPLOAD', video_size: clip.length, chunk_size: clip.length, total_chunk_count: 1,
  });
  const fileUploads = h.calls.filter(c => c.method === 'PUT');
  assert.equal(fileUploads.length, 1);
  assert.equal(fileUploads[0].headers.get('Authorization'), null);
  assert.equal(fileUploads[0].headers.get('Content-Range'), 'bytes 0-' + (clip.length - 1) + '/' + clip.length);
  assert.deepEqual(Buffer.from(fileUploads[0].bytes), clip);
  assert.ok(h.callsTo(statusPath).length > 0);
  const page = await h.visit(samplePage);
  const text = await page.text();
  assert.match(text, /Delivered to TikTok inbox/i);
  assert.match(page.headers.get('Cache-Control'), /no-store/);
  const browserVisible = text + JSON.stringify([...page.headers]) + JSON.stringify([...h.cookies]);
  for (const value of [openId, accessToken, clientSecret, uploadUrl, 'fake-test-auth-code', 'fake-ignored-refresh-token']) {
    assert.ok(!browserVisible.includes(value), 'private data escaped into client response');
  }
  const stored = JSON.stringify([...h.objects.values()].map(o => [...o.storage.entries]));
  assert.ok(!stored.includes(accessToken));
  assert.ok(!stored.includes(uploadUrl));
});

test('KAI draft uses reviewed repository bytes, explicit confirmation and one account receipt', async t => {
  const media = await readFile(new URL('../site' + kaiPath, import.meta.url));
  assert.equal(media.length, kaiSize);
  assert.equal(createHash('sha256').update(media).digest('hex'), kaiDigest);
  const h = fixture(t, { useDeployment: true, kaiAsset: media });
  const form = await h.session('kai');
  const send = fields => h.visit(landing + '/upload', { method: 'POST', form: new URLSearchParams(fields) });
  assert.equal((await send({ ...form, confirm: '' })).status, 400);
  assert.equal((await send({ ...form, csrf: 'invalid' })).status, 403);
  assert.equal((await send({ ...form, url: origin + '/different-video.mp4' })).status, 400);
  assert.equal(h.callsTo(initPath).length, 0);
  const results = await Promise.all([send(form), send(form)]);
  assert.ok(results.some(res => res.status === 303 && new URL(res.headers.get('location'), origin).href === origin + landing));
  assert.equal(h.callsTo(initPath).length, 1);
  const init = JSON.parse(h.callsTo(initPath)[0].text);
  assert.equal(init.source_info.source, 'FILE_UPLOAD');
  assert.equal(init.source_info.video_size, kaiSize);
  const transfers = h.calls.filter(call => call.method === 'PUT');
  assert.equal(transfers.length, 1);
  assert.equal(createHash('sha256').update(transfers[0].bytes).digest('hex'), kaiDigest);
  assert.equal(transfers[0].headers.get('Authorization'), null);
  assert.equal((await h.post('status', form)).status, 303);
  assert.match(await (await h.visit(landing)).text(), /Delivered to TikTok inbox/i);
  assert.doesNotMatch(await (await h.visit(samplePage)).text(), /Delivered to TikTok inbox/i);
});

test('changed KAI bytes in static storage are rejected before contacting TikTok', async t => {
  const media = await readFile(new URL('../site' + kaiPath, import.meta.url));
  media[media.length - 1] ^= 1;
  const h = fixture(t, { useDeployment: true, kaiAsset: media });
  const form = await h.session('kai');
  const result = await h.visit(landing + '/upload', { method: 'POST', form: new URLSearchParams(form) });
  assert.equal(result.status, 409);
  assert.equal(h.callsTo(initPath).length, 0);
  assert.equal(h.calls.some(call => call.method === 'PUT'), false);
});

test('losing a TikTok initialization response never causes an automatic or manual duplicate', async t => {
  const h = fixture(t, { dropInitResponse: true });
  const form = await h.session();
  await h.post('upload', form);
  await h.post('upload', form);
  assert.equal(h.callsTo(initPath).length, 1);
  assert.equal(h.calls.filter(c => c.method === 'PUT').length, 0);
});

test('redirecting signed file upload is never followed or resent', async t => {
  const h = fixture(t, { redirectOn: '/video/' });
  const form = await h.session();
  await h.post('upload', form);
  await h.post('upload', form);
  assert.equal(h.callsTo(initPath).length, 1);
  const transfers = h.calls.filter(call => call.method === 'PUT');
  assert.equal(transfers.length, 1);
  assert.equal(transfers[0].url.href, uploadUrl);
  assert.equal(transfers[0].redirect, 'manual');
  assert.equal(transfers[0].headers.get('Authorization'), null);
  assert.equal(h.calls.some(call => call.url.origin === new URL(mockRedirect).origin), false);
  const page = await (await h.visit(samplePage)).text();
  assert.doesNotMatch(page, /Delivered to TikTok inbox|private provider diagnostic/i);
});

test('legacy KAI receipt with no publish ID permits exactly one explicit pre-transfer recovery', async t => {
  const h = fixture(t, { useDeployment: true, dropInitResponse: true });
  const form = await h.session('kai');
  const storage = [...h.objects].find(([key]) => key.endsWith(':' + kaiDigest))[1].storage;
  await storage.put('attempt', { sid: 'prior-session', attempt: 'prior-attempt', state: 'uncertain' });
  const review = await (await h.visit(landing)).text();
  assert.match(review, /action="\/sandbox\/tiktok\/upload"/);
  const send = fields => h.visit(landing + '/upload', { method: 'POST', form: new URLSearchParams(fields) });
  await send({ ...form, confirm: '' });
  assert.equal(h.callsTo(initPath).length, 0);
  await Promise.all([send(form), send(form)]);
  await send(form);
  assert.equal(h.callsTo(initPath).length, 1);
  assert.equal(h.calls.filter(call => call.method === 'PUT').length, 0);
  assert.doesNotMatch(await (await h.visit(landing)).text(), /action="\/sandbox\/tiktok\/upload"/);
});

test('legacy receipt containing a publish ID is polled without resending video', async t => {
  const h = fixture(t);
  const form = await h.session('kai');
  const storage = [...h.objects].find(([key]) => key.endsWith(':' + kaiDigest))[1].storage;
  await storage.put('attempt', { sid: 'prior-session', attempt: 'prior-attempt',
    state: 'uncertain', publishId: 'v_inbox_file~v2.legacy' });
  const html = await (await h.visit(landing)).text();
  assert.match(html, /action="\/sandbox\/tiktok\/status"/);
  assert.doesNotMatch(html, /action="\/sandbox\/tiktok\/upload"/);
  await h.visit(landing + '/upload', { method: 'POST', form: new URLSearchParams(form) });
  await h.post('status', form);
  assert.equal(h.callsTo(initPath).length, 0);
  assert.equal(h.calls.filter(call => call.method === 'PUT').length, 0);
  assert.equal(JSON.parse(h.callsTo(statusPath)[0].text).publish_id, 'v_inbox_file~v2.legacy');
});

test('a failed signed PUT keeps the durable publish ID and cannot trigger a second PUT', async t => {
  let rowAtPut;
  const h = fixture(t, { onPut(objects) {
    rowAtPut = [...objects.values()].map(object => object.storage.entries.get('attempt')).find(Boolean);
    return new Response('private transfer diagnostic', { status: 503 });
  } });
  const form = await h.session();
  await h.post('upload', form);
  assert.equal(rowAtPut?.publishId, 'v_inbox_file~v2.12345');
  assert.equal(rowAtPut.stage, 'transfer_started');
  await h.post('upload', form);
  assert.equal(h.callsTo(initPath).length, 1);
  assert.equal(h.calls.filter(call => call.method === 'PUT').length, 1);
  const html = await (await h.visit(samplePage)).text();
  assert.match(html, /action="\/sandbox\/tiktok\/status"/);
  assert.doesNotMatch(html, /private transfer diagnostic|action="\/sandbox\/tiktok\/upload"/);
});

test('an untrusted transfer address keeps the provider ID for status and sends no bytes', async t => {
  const h = fixture(t, { signedUploadUrl: 'https://upload.example/video/?private-capability=123' });
  const form = await h.session();
  await h.post('upload', form);
  await h.post('upload', form);
  assert.equal(h.callsTo(initPath).length, 1);
  assert.equal(h.calls.filter(call => call.method === 'PUT').length, 0);
  const html = await (await h.visit(samplePage)).text();
  assert.match(html, /action="\/sandbox\/tiktok\/status"/);
  assert.doesNotMatch(html, /private-capability|upload\.example/);
  await h.post('status', form);
  assert.equal(JSON.parse(h.callsTo(statusPath)[0].text).publish_id, 'v_inbox_file~v2.12345');
});

for (const status of [200, 400]) {
  test('HTTP ' + status + ' TikTok business error is explained without leaking provider details', async t => {
    const h = fixture(t, { initReply: () => Response.json({
      error: { code: 'scope_not_authorized', message: 'private provider message', log_id: 'private-provider-trace' },
    }, { status }) });
    const form = await h.session();
    await h.post('upload', form);
    await h.post('upload', form);
    const html = await (await h.visit(samplePage)).text();
    assert.match(html, /permission|scope_not_authorized/i);
    assert.match(html, new RegExp('HTTP ' + status));
    assert.doesNotMatch(html, /private provider message|private-provider-trace/);
    assert.equal(h.callsTo(initPath).length, 1);
    assert.equal(h.calls.filter(call => call.method === 'PUT').length, 0);
  });
}

for (const address of [
  'https://open-upload-us.tiktokapis.com/upload/?upload_id=demo',
  'https://upload.us.tiktokapis.com/video?upload_id=demo',
]) {
  test('accepts the TikTok FILE_UPLOAD endpoint ' + new URL(address).host + new URL(address).pathname, async t => {
    const h = fixture(t, { signedUploadUrl: address });
    const form = await h.session();
    await h.post('upload', form);
    const puts = h.calls.filter(call => call.method === 'PUT');
    assert.equal(puts.length, 1);
    assert.equal(puts[0].url.href, address);
    assert.equal(puts[0].headers.get('Authorization'), null);
    assert.equal(puts[0].redirect, 'manual');
  });
}

const feedPath = landing + '/feed?clips=' + clipHash;
const automationPath = landing + '/automation';
function film(overrides = {}) {
  return {kind:'character', generation_method:'photo_sequence', created_at:new Date().toISOString(), sha256:clipHash,
    video_path:'generated/video-' + clipHash.slice(0,24) + '.mp4', ...overrides};
}

async function openGallery(h) {
  const auth = await h.complete(await h.start(true));
  assert.equal(auth.headers.get('Location'), '/gallery.html');
  const state = await (await h.visit(feedPath)).json();
  assert.equal(state.connected, true);
  assert.equal(state.canEnable, true);
  return state;
}

function setAutomation(h, csrf, enabled, requestOrigin = origin) {
  return h.visit(automationPath, {method:'POST', requestOrigin, form:new URLSearchParams({csrf, enabled})});
}

async function putNewFilm(archive, source) {
  // A new GitHub Pages deployment is observed only after the opt-in timestamp.
  await new Promise(resolve => setTimeout(resolve, 8));
  archive.items = [film(source)];
}

function coordinator(h) {
  return [...h.objects].find(([key]) => key.startsWith('automation:'))[1];
}

test('gallery is account-scoped and the current archive is never sent on enable', async t => {
  const archive = {items:[film()]};
  const h = fixture(t, {archive});
  assert.deepEqual(await (await h.visit(feedPath)).json(), {connected:false, receipts:{}});
  const {csrf} = await openGallery(h);
  assert.equal((await setAutomation(h, csrf, '1', 'https://attacker.example')).status, 403);
  assert.equal((await setAutomation(h, 'bad', '1')).status, 403);
  assert.equal((await setAutomation(h, csrf, '1')).status, 200);
  await coordinator(h).alarm();
  assert.equal(h.callsTo(initPath).length, 0);
  assert.ok(coordinator(h).storage.alarmAt > Date.now());
  assert.equal((await h.visit(landing + '/feed?clips=not-a-hash')).status, 400);
  const envData = JSON.stringify([...h.objects.values()].map(o => [...o.storage.entries]));
  assert.ok(!envData.includes(accessToken) && !envData.includes('fake-ignored-refresh-token'));
  h.cookies.clear();
  assert.deepEqual(await (await h.visit(feedPath)).json(), {connected:false, receipts:{}});
});

test('gallery alarm sends new photo sequences once when the browser is closed and exposes real receipt times', async t => {
  const archive = {items:[]};
  let phase = 'SEND_TO_USER_INBOX';
  const h = fixture(t, {archive, statusReply:() => phase});
  const {csrf} = await openGallery(h);
  await setAutomation(h, csrf, '1');
  const agent = coordinator(h);
  h.cookies.delete('__Host-idore-tt-session');
  await putNewFilm(archive, {kind:'api_test'});
  await agent.alarm();
  assert.equal(h.callsTo(initPath).length, 0);
  await putNewFilm(archive, {generation_method:'legacy'});
  await agent.alarm();
  assert.equal(h.callsTo(initPath).length, 0);
  await putNewFilm(archive);
  await Promise.all([agent.alarm(), agent.alarm()]);
  const feed = await (await h.visit(feedPath)).json();
  assert.equal(feed.receipts[clipHash].state, 'delivered');
  assert.ok(Date.parse(feed.receipts[clipHash].deliveredAt) >= Date.parse(feed.receipts[clipHash].acceptedAt));
  assert.equal(h.callsTo(initPath).length,1);
  assert.equal(h.calls.filter(x => x.method === 'PUT').length,1);
  assert.ok(!JSON.stringify(feed).includes('v_inbox_file'));
  phase = 'PUBLISH_COMPLETE';
  await agent.storage.put('watches', {[clipHash]:Date.now() - 1});
  await agent.alarm();
  const posted = (await (await h.visit(feedPath)).json()).receipts[clipHash];
  assert.equal(posted.state, 'published');
  assert.equal(posted.deliveredAt, feed.receipts[clipHash].deliveredAt);
  assert.ok(Date.parse(posted.publishedAt));
  assert.equal((await setAutomation(h, csrf, '0')).status, 200);
  assert.equal(agent.storage.alarmAt, null);
  assert.equal((await agent.storage.get('automation')).box, undefined);
  await agent.alarm();
  assert.equal(h.callsTo(initPath).length,1);
});

test('background grant refresh privately rotates both tokens and stops on removed access', async t => {
  const archive = {items:[]};
  let revoked = false;
  const h = fixture(t, {archive, tokenTTL:90, refresh:() => revoked ?
    Response.json({error:'invalid_grant'}, {status:400}) :
    Response.json({open_id:openId, scope:'video.upload,user.info.basic', expires_in:90, refresh_expires_in:20000,
      access_token:'fresh-token', refresh_token:'rotated-refresh'})});
  const {csrf} = await openGallery(h);
  await setAutomation(h, csrf, '1');
  await putNewFilm(archive);
  const agent = coordinator(h);
  await agent.alarm();
  assert.equal(h.callsTo(initPath)[0].headers.get('Authorization'), 'Bearer fresh-token');
  const saved = JSON.stringify(await agent.storage.get('automation'));
  assert.ok(!saved.includes('fresh-token') && !saved.includes('rotated-refresh'));
  revoked = true;
  await agent.alarm();
  const latestRequest = h.callsTo('/v2/oauth/token/').at(-1);
  assert.equal(new URLSearchParams(latestRequest.text).get('refresh_token'), 'rotated-refresh');
  const state = await (await h.visit(feedPath)).json();
  assert.equal(state.automation.enabled, false);
  assert.equal(state.automation.problem, 'reconnect_required');
  assert.equal((await agent.storage.get('automation')).box, undefined);
});

test('auto sends reject changed archive bytes and respect a definitive quota rejection', async t => {
  const archive = {items:[], bytes:Buffer.from('bad')};
  const h = fixture(t, {archive, initReply:() => Response.json({error:{code:'spam_risk_too_many_pending_share'}}, {status:403})});
  const {csrf} = await openGallery(h);
  await setAutomation(h, csrf, '1');
  await putNewFilm(archive);
  const agent = coordinator(h);
  await agent.alarm();
  assert.equal(h.callsTo(initPath).length, 0);
  let result = await (await h.visit(feedPath)).json();
  assert.equal(result.automation.problem, 'media_unavailable');
  delete archive.bytes;
  await agent.storage.delete('handled:' + clipHash);
  await agent.alarm();
  await agent.alarm();
  result = await (await h.visit(feedPath)).json();
  assert.equal(h.callsTo(initPath).length, 1);
  assert.equal(result.automation.problem, 'quota_limited');
  assert.equal(result.receipts[clipHash].state, 'rejected');
  assert.ok(Date.parse(result.receipts[clipHash].failedAt));
});
