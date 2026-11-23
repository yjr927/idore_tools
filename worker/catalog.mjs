const PAGES = 'https://yjr927.github.io/idore_tools/';
const MANIFEST_LIMIT = 4 * 1024 * 1024;
const VIDEO_LIMIT = 16 * 1024 * 1024;
const HASH = /^[a-f0-9]{64}$/;
// These slots are independently enrolled for one account in sandbox.mjs.
const NAMED_REFERENCES = new Map([
  ['2026-11-23-momoko', {id:'tiktok-7703265627972128021', url:'https://www.tiktok.com/@itsmomoko.jp/video/7703265627972128021', cast:'female'}],
  ['2026-11-23-bay', {id:'tiktok-7709762212679683342', url:'https://www.tiktok.com/@mybaybie/video/7709762212679683342', cast:'male'}],
]);
const NAMED_START = Date.parse('2026-11-23T09:00:00-05:00');
const NAMED_END = Date.parse('2026-11-24T00:00:00-05:00');

async function readBounded(response, limit) {
  const length = response.headers.get('content-length');
  if (!response.ok || response.status !== 200 || !response.body ||
      (length !== null && (!/^\d+$/.test(length) || Number(length) > limit))) {
    await response.body?.cancel();
    throw new Error('Archive resource unavailable or too large.');
  }
  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  for (;;) {
    const {value, done} = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > limit) {
      await reader.cancel();
      throw new Error('Archive resource exceeds the transfer limit.');
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes;
}

function archivePath(hash) {
  if (typeof hash !== 'string' || !HASH.test(hash)) throw new Error('Invalid archive identifier.');
  return '/generated/video-' + hash.slice(0, 24) + '.mp4';
}

function pagesRequest(path) {
  return new Request(new URL(path.slice(1), PAGES), {
    redirect: 'manual', signal: AbortSignal.timeout(30000)
  });
}

// Only final motion remakes admitted by the verified trend and OpenAI planning pipeline may be sent.
function eligibleRemake(item) {
  if (item.kind !== 'character' || item.generation_method !== 'agent_motion' ||
      item.planner !== 'openai' || item.trend_verified !== true ||
      !['male', 'female'].includes(item.material_group) || (!Array.isArray(item.source_photo_ids) || !item.source_photo_ids.length) ||
      !Number.isSafeInteger(item.source_metrics?.views) || item.source_metrics.views < 1000000 ||
      !Number.isSafeInteger(item.source_metrics?.likes) || item.source_metrics.likes < 100000) return false;
  const video = /^https:\/\/www\.tiktok\.com\/@([\w.-]{1,32})\/video\/(\d+)$/i;
  const original = video.exec(item.source_url);
  if (!original || !Array.isArray(item.source_peers)) return false;
  const creators = new Set([original[1].toLowerCase()]);
  const videos = new Set([original[2]]);
  for (const peer of item.source_peers) {
    const match = video.exec(peer?.url);
    if (!match || (typeof peer?.note !== 'string' || !peer.note.trim()) || videos.has(match[2])) continue;
    const creator = match[1].toLowerCase();
    if (creators.has(creator)) continue;
    creators.add(creator);
    videos.add(match[2]);
  }
  return creators.size >= 3;
}

function eligibleNamedSlot(item, created) {
  const ref = NAMED_REFERENCES.get(item.sequence_slot);
  if (!ref || item.trend_id !== ref.id || item.source_url !== ref.url ||
      item.sequence_type !== 'selected_reference' || item.selection_basis !== 'user_selected_reference' ||
      item.kind !== 'character' || item.generation_method !== 'named_reference_motion' || item.trend_verified !== false ||
      item.cast_type !== 'solo' || item.material_group !== ref.cast || item.planner !== 'openai' ||
      item.planner_provider !== 'openrouter' || typeof item.planner_model !== 'string' ||
      !/^openai\/[\w.-]+$/.test(item.planner_model) || item.execution_host !== 'cloudflare' ||
      !['source_motion', 'observed_description'].includes(item.motion_reference_method) ||
      item.timestamp_basis !== 'generation_completed' || !Number.isFinite(created) ||
      created < NAMED_START || created >= NAMED_END || Date.parse(item.generated_at) !== created ||
      !Number.isSafeInteger(item.bytes) || item.bytes < 12 || item.bytes > VIDEO_LIMIT ||
      !Number.isFinite(item.duration) || item.duration < 5.5 || item.duration > 6.5) return null;
  if (!Array.isArray(item.source_photo_ids) || item.source_photo_ids.length !== 1 ||
      typeof item.source_photo_ids[0] !== 'string' || !item.source_photo_ids[0].trim() ||
      !Array.isArray(item.source_photo_sha256) || item.source_photo_sha256.length !== 1 ||
      !HASH.test(item.source_photo_sha256[0])) return null;
  return item.sequence_slot;
}

// Use the published repository manifest even if its data changes between Worker deployments.
export async function listFilms() {
  const bytes = await readBounded(await fetch(pagesRequest('/data/creations.json')), MANIFEST_LIMIT);
  const manifest = JSON.parse(new TextDecoder().decode(bytes));
  if (manifest.schema_version !== 1 || !Array.isArray(manifest.items)) {
    throw new Error('Unrecognized film archive.');
  }
  const films = new Map();
  for (const item of manifest.items) {
    if (!item || typeof item.sha256 !== 'string' || !HASH.test(item.sha256)) continue;
    const hash = item.sha256;
    const path = archivePath(hash);
    if (item.video_path !== path.slice(1) || films.has(hash)) continue;
    const timestamp = typeof item.created_at === 'string' &&
      /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(item.created_at)
      ? Date.parse(item.created_at) : NaN;
    const generatedAt = Number.isFinite(timestamp) ? new Date(timestamp).toISOString() : null;
    const namedSlot = eligibleNamedSlot(item, timestamp);
    films.set(hash, {
      hash, path, title: String(item.title || item.character || 'idore').slice(0, 160), generatedAt,
      autoEligible: Boolean(generatedAt && eligibleRemake(item)),
      ...(namedSlot ? {namedSlot} : {})
    });
  }
  return [...films.values()];
}

export async function readFilm(_env, _origin, film) {
  if (!film || film.path !== archivePath(film.hash)) throw new Error('Invalid archive film.');
  const bytes = await readBounded(await fetch(pagesRequest(film.path)), VIDEO_LIMIT);
  if (bytes.length < 12 || new TextDecoder().decode(bytes.subarray(4, 8)) !== 'ftyp') {
    throw new Error('Archive film is not an MP4.');
  }
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  const actual = Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, '0')).join('');
  if (actual !== film.hash) throw new Error('Archive film fingerprint mismatch.');
  return bytes;
}
