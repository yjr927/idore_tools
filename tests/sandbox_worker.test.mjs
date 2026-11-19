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

// The transaction queue also models concurrently arriving requests to one object.
class MemoryStorage {
  entries = new Map();
  queue = Promise.resolve();
  async get(key) { return structuredClone(this.entries.get(key)); }
  async put(key, value) { this.entries.set(key, structuredClone(value)); }
  async delete(key) { return this.entries.delete(key); }
  async deleteAll() { this.entries.clear(); }
  async setAlarm() {}
  transaction(fn) {
    const result = this.queue.then(() => fn(this));
    this.queue = result.catch(() => {});
    return result;
  }
}

function fixture(t, { scope = 'video.upload,user.info.basic', dropInitResponse = false, useDeployment = false, kaiAsset } = {}) {
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
          objects.set(id, { storage, fetch: (input, options) => instance.fetch(new Request(input, options)) });
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
    calls.push({ url, method: req.method, headers: req.headers, bytes, text });
    if (req.url === uploadUrl && req.method === 'PUT') return new Response(null, { status: 201 });
    assert.equal(url.origin, 'https://open.tiktokapis.com');
    switch (url.pathname) {
      case '/v2/oauth/token/':
        return Response.json({ access_token: accessToken, token_type: 'Bearer', open_id: openId,
          refresh_token: 'fake-ignored-refresh-token', expires_in: 86400, scope });
      case '/v2/user/info/':
        return ok({ user: { open_id: openId, display_name: 'Sandbox Tester' } });
      case initPath:
        if (dropInitResponse) throw new Error('Mocked lost initialization response');
        return ok({ publish_id: 'v_inbox_file~v2.12345', upload_url: uploadUrl });
      case statusPath:
        return ok({ status: 'SEND_TO_USER_INBOX' });
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
  async function start() {
    const res = await visit('/auth/tiktok/start');
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
