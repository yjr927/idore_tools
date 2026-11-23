// Each photo is held in a separate expiring object. GitHub sees only the public job ID.
const ISSUER = 'https://token.actions.githubusercontent.com';
const REPO = 'yjr927/idore_tools';
const BRANCH = 'refs/heads/main';
const WORKFLOW = REPO + '/.github/workflows/photo-generation.yml@' + BRANCH;
const SUBJECTS = new Set([
  'repo:' + REPO + ':ref:' + BRANCH,
  'repo:yjr927@117725385/idore_tools@1454660637:ref:' + BRANCH,
]);
const ROUTE = /^\/api\/photo\/([a-f0-9]{64})\/(init|status|input|claim|result|fail)$/;
const CLIENT = new Set(['GET status', 'POST input', 'GET result']);
const RUNNER = new Set(['POST init', 'GET input', 'POST claim', 'POST result', 'POST fail']);
const FAILURE = new Set(['missing_key', 'input_timeout', 'invalid_input', 'provider_auth',
  'quota_exceeded', 'rate_limited', 'provider_rejected', 'submission_uncertain',
  'invalid_result', 'delivery_failed', 'runner_error']);
const MIME = new Set(['image/png', 'image/jpeg', 'image/webp']);
const MAX_REFERENCES = 6 * 1024 * 1024;
const MAX_INPUT = Math.ceil(MAX_REFERENCES / 3) * 4 + 64 * 1024;
const MAX_OUTPUT = 12 * 1024 * 1024;
const CHUNK = 96 * 1024;
const LIFETIME = 30 * 60 * 1000;
const HEADERS = {'Cache-Control':'private, no-store', 'X-Content-Type-Options':'nosniff',
  'Referrer-Policy':'no-referrer', 'Content-Security-Policy':"default-src 'none'; frame-ancestors 'none'"};
const encoder = new TextEncoder();
const decoder = new TextDecoder('utf-8', {fatal:true});
const json = (body, status = 200) => Response.json(body, {status, headers:HEADERS});
const problem = (status, code) => json({error:code}, status);
const contentType = req => (req.headers.get('Content-Type') || '').split(';')[0].trim().toLowerCase();

async function sha256Hex(value) {
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', encoder.encode(value)));
  return Array.from(digest, b => b.toString(16).padStart(2, '0')).join('');
}

function unbase64url(value) {
  return Uint8Array.from(atob(value.replace(/-/g, '+').replace(/_/g, '/')), ch => ch.charCodeAt(0));
}

let cachedKeys;
let pendingKeys;
async function githubKeys() {
  if (cachedKeys?.expires > Date.now()) return cachedKeys.keys;
  if (!pendingKeys) pendingKeys = (async () => {
    const response = await fetch(ISSUER + '/.well-known/jwks', {
      redirect:'error', headers:{Accept:'application/json', 'User-Agent':'idore-photo-studio/1.0'},
      signal:AbortSignal.timeout(10000),
    });
    if (!response.ok) throw new Error('identity_unavailable');
    const {keys} = await response.json();
    if (!Array.isArray(keys) || !keys.length) throw new Error('identity_unavailable');
    cachedKeys = {keys, expires:Date.now() + 300_000};
    return keys;
  })().finally(() => { pendingKeys = undefined; });
  return pendingKeys;
}

async function runnerIdentity(token, jobId) {
  if (token.length > 16384 || !/^[\w-]+\.[\w-]+\.[\w-]+$/.test(token)) return null;
  const [header, payload, signature] = token.split('.');
  let h, p;
  try {
    h = JSON.parse(decoder.decode(unbase64url(header)));
    p = JSON.parse(decoder.decode(unbase64url(payload)));
  } catch { return null; }
  const now = Date.now() / 1000;
  if (h?.alg !== 'RS256' || typeof h.kid !== 'string' || h.crit !== undefined ||
      p?.iss !== ISSUER || p.aud !== 'idore-photo:' + jobId || !SUBJECTS.has(p.sub) ||
      p.repository !== REPO || p.repository_id !== '1454660637' || p.repository_owner_id !== '117725385' ||
      p.workflow_ref !== WORKFLOW || p.ref !== BRANCH || !['workflow_dispatch','push'].includes(p.event_name) ||
      p.run_attempt !== '1' || typeof p.run_id !== 'string' || !/^\d{1,22}$/.test(p.run_id) ||
      !Number.isFinite(p.exp) || !Number.isFinite(p.nbf) || !Number.isFinite(p.iat) ||
      p.exp <= now || p.nbf > now + 30 || p.iat > now + 30) return null;
  // A deployment smoke check can touch only that push run's disposable object.
  if (p.event_name === 'push' && jobId !== await sha256Hex(await sha256Hex('idore-photo-smoke:' + p.run_id))) return null;
  const jwk = (await githubKeys()).find(k => k.kid === h.kid && k.kty === 'RSA' &&
    (!k.use || k.use === 'sig') && (!k.alg || k.alg === 'RS256'));
  if (!jwk) return null;
  try {
    const key = await crypto.subtle.importKey('jwk', jwk, {name:'RSASSA-PKCS1-v1_5', hash:'SHA-256'}, false, ['verify']);
    const ok = await crypto.subtle.verify('RSASSA-PKCS1-v1_5', key, unbase64url(signature), encoder.encode(header + '.' + payload));
    return ok ? p.run_id : null;
  } catch { return null; }
}

export async function photoRequest(request, env) {
  const url = new URL(request.url), match = ROUTE.exec(url.pathname);
  if (url.protocol !== 'https:' || url.search || !match) return problem(404, 'not_found');
  const [, id, op] = match, action = request.method + ' ' + op;
  if (!CLIENT.has(action) && !RUNNER.has(action)) return problem(405, 'method_not_allowed');
  const auth = request.headers.get('Authorization') || '';
  if (!auth.startsWith('Bearer ')) return problem(401, 'unauthorized');
  const token = auth.slice(7);
  const headers = new Headers();
  if (CLIENT.has(action)) {
    if (!/^[a-f0-9]{64}$/.test(token) || await sha256Hex(token) !== id) return problem(401, 'unauthorized');
  } else {
    let run;
    try { run = await runnerIdentity(token, id); }
    catch { return problem(503, 'identity_unavailable'); }
    if (!run) return problem(401, 'unauthorized');
    headers.set('X-Runner-Id', run);
  }
  // Control operations have no payload. Let the runtime compute its own
  // transfer headers when forwarding a real file stream.
  const hasBody = request.method === 'POST' && ['input','result','fail'].includes(op);
  if (hasBody && request.headers.has('Content-Type')) headers.set('Content-Type', request.headers.get('Content-Type'));
  try {
    const stub = env.PHOTO_JOBS.get(env.PHOTO_JOBS.idFromName(id));
    const response = await stub.fetch(new Request('https://photo.internal/' + op, {
      method:request.method, headers, ...(hasBody && request.body ? {body:request.body, duplex:'half'} : {}),
    }));
    return response.status >= 500 ? problem(503, 'storage_unavailable') : response;
  } catch { return problem(503, 'storage_unavailable'); }
}

async function boundedBody(request, limit) {
  if (!request.body || Number(request.headers.get('Content-Length')) > limit) return null;
  const reader = request.body.getReader(), chunks = [];
  let size = 0;
  try {
    for (;;) {
      const {done, value} = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > limit) { await reader.cancel(); return null; }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
  return bytes;
}

function imageSignature(bytes, mime) {
  if (mime === 'image/jpeg') return bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff;
  if (mime === 'image/png') return [137,80,78,71,13,10,26,10].every((v,i) => bytes[i] === v);
  return mime === 'image/webp' && String.fromCharCode(...bytes.slice(0,4)) === 'RIFF' &&
    String.fromCharCode(...bytes.slice(8,12)) === 'WEBP';
}

function validInput(bytes) {
  try {
    const value = JSON.parse(decoder.decode(bytes));
    if (!value || Object.keys(value).length !== 2 || typeof value.prompt !== 'string' ||
        !value.prompt.trim() || [...value.prompt].length > 4000 ||
        !Array.isArray(value.images) || value.images.length < 1 || value.images.length > 4) return false;
    let total = 0;
    for (const image of value.images) {
      if (!image || Object.keys(image).length !== 2 || !MIME.has(image.mime) ||
          typeof image.data !== 'string' || image.data.length < 4 || image.data.length % 4 ||
          /[^a-zA-Z0-9+/=]/.test(image.data)) return false;
      const raw = atob(image.data);
      total += raw.length;
      if (total > MAX_REFERENCES || !imageSignature(Uint8Array.from(raw.slice(0,12), ch => ch.charCodeAt(0)), image.mime)) return false;
    }
    return true;
  } catch { return false; }
}

export class PhotoJob {
  constructor(ctx) {
    this.storage = ctx.storage;
    this.kv = ctx.storage.kv;
  }

  // These keys hold either the request JSON or the output raster; never both.
  storeFile(meta, bytes) {
    this.eraseFile(meta);
    for (let start = 0; start < bytes.length; start += CHUNK) {
      this.kv.put('part' + meta.parts++, bytes.slice(start, start + CHUNK));
    }
  }

  eraseFile(meta) {
    for (let i = 0; i < meta.parts; i++) this.kv.delete('part' + i);
    meta.parts = 0;
  }

  fileResponse(meta, type) {
    const chunks = Array.from({length:meta.parts}, (_, i) => this.kv.get('part' + i));
    return new Response(new Blob(chunks, {type}), {headers:{...HEADERS, 'Content-Type':type}});
  }

  async fetch(request) {
    const action = request.method + ' ' + new URL(request.url).pathname.slice(1);
    const run = request.headers.get('X-Runner-Id');
    if (RUNNER.has(action) !== Boolean(run) || (!CLIENT.has(action) && !RUNNER.has(action))) return problem(403, 'forbidden');
    if (action === 'POST init') {
      // Set the alarm before storing any private data, including on a failed init.
      if (!this.kv.get('meta')) {
        const expires = Date.now() + LIFETIME;
        await this.storage.setAlarm(expires);
        if (!this.kv.get('meta')) this.kv.put('meta', {run, expires, status:'waiting', parts:0});
      }
    }
    let meta = this.kv.get('meta');
    if (!meta || meta.expires <= Date.now()) return problem(404, 'not_found');
    if (run && run !== meta.run) return problem(409, 'different_run');
    const summary = () => json(meta.code ? {status:meta.status, code:meta.code} : {status:meta.status});
    if (action === 'POST init' || action === 'GET status') return summary();
    if (action === 'GET result' || action === 'GET input') {
      const result = action === 'GET result';
      if (meta.status !== (result ? 'ready' : 'uploaded')) return problem(409, 'wrong_status');
      return this.fileResponse(meta, result ? meta.mime : 'application/json');
    }
    const expected = {'POST input':'waiting', 'POST claim':'uploaded', 'POST result':'generating'}[action];
    if (action === 'POST result' && meta.status === 'ready') return summary();
    if (expected && meta.status !== expected) return problem(409, 'wrong_status');

    const type = contentType(request);
    let bytes, code;
    if (action !== 'POST claim') {
      if (action === 'POST result' ? !MIME.has(type) : type !== 'application/json') return problem(415, 'invalid_type');
      const limit = action === 'POST result' ? MAX_OUTPUT : action === 'POST input' ? MAX_INPUT : 512;
      bytes = await boundedBody(request, limit);
      if (!bytes) return problem(413, 'body_too_large');
      if (action === 'POST input' && !validInput(bytes)) return problem(422, 'invalid_input');
      if (action === 'POST result' && !imageSignature(bytes, type)) return problem(422, 'invalid_result');
      if (action === 'POST fail') {
        try { code = JSON.parse(decoder.decode(bytes)).code; } catch { /* disallow arbitrary text */ }
        if (!FAILURE.has(code)) return problem(422, 'invalid_code');
      }
    }
    // Recheck after streaming the body. Claiming and removing the only upload are atomic.
    return this.storage.transactionSync(() => {
      meta = this.kv.get('meta');
      if (!meta || meta.expires <= Date.now()) return problem(404, 'not_found');
      if (run && run !== meta.run) return problem(409, 'different_run');
      if (action === 'POST result' && meta.status === 'ready') return summary();
      if (expected && meta.status !== expected) return problem(409, 'wrong_status');
      if (action === 'POST fail') {
        if (meta.status === 'ready' || meta.status === 'failed') return summary();
        this.eraseFile(meta);
        Object.assign(meta, {status:'failed', code});
      } else if (action === 'POST claim') {
        this.eraseFile(meta);
        meta.status = 'generating';
      } else {
        this.storeFile(meta, bytes);
        if (action === 'POST input') meta.status = 'uploaded';
        else Object.assign(meta, {status:'ready', mime:type});
      }
      this.kv.put('meta', meta);
      return summary();
    });
  }

  async alarm() { await this.storage.deleteAll(); }
}
