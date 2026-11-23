import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import test from 'node:test';
import {listFilms, readFilm} from '../worker/catalog.mjs';

const sample = new Uint8Array([0,0,0,12,102,116,121,112,105,115,111,109]);
const hash = createHash('sha256').update(sample).digest('hex');
const path = 'generated/video-' + hash.slice(0,24) + '.mp4';
const row = {sha256:hash, video_path:path, title:'Gallery', kind:'character',
  generation_method:'photo_sequence', created_at:'2026-11-22T21:20:00Z'};
const manifest = items => JSON.stringify({schema_version:1,items});
const url = 'https://yjr927.github.io/idore_tools/';

test('only content-addressed archive records can be used, including older view-only records', async t => {
  let body = manifest([null, {...row, video_path:'https://example.com/a.mp4'}, row, row]);
  t.mock.method(globalThis, 'fetch', async request => {
    assert.equal(request.url, url + 'data/creations.json');
    assert.equal(request.redirect, 'manual');
    return new Response(body);
  });
  const [film] = await listFilms();
  assert.deepEqual(await listFilms(), [{hash, path:'/'+path, title:'Gallery', generatedAt:'2026-11-22T21:20:00.000Z', autoEligible:true}]);
  for (const patch of [{kind:'api_test'}, {generation_method:undefined}, {created_at:'abc'}]) {
    body = manifest([{...row,...patch}]);
    assert.equal((await listFilms())[0].autoEligible, false);
  }
  assert.equal(film.hash, hash);
});

test('require a bounded verified MP4 from the fixed Pages archive', async t => {
  const film = {path:'/'+path,hash};
  let response = () => new Response(sample);
  t.mock.method(globalThis, 'fetch', async request => {
    assert.equal(request.url, url + path);
    return response();
  });
  assert.deepEqual(await readFilm(null, 'https://example.org', film), sample);
  await assert.rejects(readFilm(null, null, {...film,path:'//example.org/video'}));
  response = () => new Response(new Uint8Array([...sample, 0]));
  await assert.rejects(readFilm(null, null, film), /fingerprint/);
  response = () => new Response(new Uint8Array(12));
  await assert.rejects(readFilm(null, null, film), /MP4/);
  response = () => new Response(sample, {headers:{'content-length':String(17*1024*1024)}});
  await assert.rejects(readFilm(null, null, film), /too large/);
  response = () => new Response(null, {status:302,headers:{Location:'https://example.org'}});
  await assert.rejects(readFilm(null, null, film), /unavailable/);
});
