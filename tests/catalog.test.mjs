import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import test from 'node:test';
import {listFilms, readFilm} from '../worker/catalog.mjs';

const sample = new Uint8Array([0,0,0,12,102,116,121,112,105,115,111,109]);
const hash = createHash('sha256').update(sample).digest('hex');
const path = 'generated/video-' + hash.slice(0,24) + '.mp4';
const row = {sha256:hash, video_path:path, title:'Gallery', kind:'character',
  generation_method:'agent_motion', planner:'openai', trend_verified:true, material_group:'female',
  source_photo_ids:['approved-a'], source_metrics:{views:1200000,likes:150000},
  source_url:'https://www.tiktok.com/@original/video/1234567890123456789',
  source_peers:[{url:'https://www.tiktok.com/@peer1/video/2234567890123456789',note:'same turn'},
    {url:'https://www.tiktok.com/@peer2/video/3234567890123456789',note:'same beat'}], created_at:'2026-11-22T21:20:00Z'};
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
  for (const patch of [{kind:'api_test'}, {generation_method:'photo_sequence'}, {created_at:'abc'}, {planner:'openrouter'},
    {material_group:'unknown'}, {trend_verified:false}, {source_metrics:{views:999999,likes:150000}},
    {source_metrics:{views:1200000,likes:99999}}, {source_peers:[row.source_peers[0], row.source_peers[0]]}]) {
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

test('named motion enrollment is limited to the exact two references and never enables global delivery', async t => {
  const momoko = {...row, sequence_slot:'2026-11-23-momoko', source_url:'https://www.tiktok.com/@itsmomoko.jp/video/7703265627972128021',
    trend_id:'tiktok-7703265627972128021', created_at:'2026-11-23T14:01:00Z', generated_at:'2026-11-23T14:01:00Z',
    generation_method:'named_reference_motion', trend_verified:false, selection_basis:'user_selected_reference',
    sequence_type:'selected_reference', cast_type:'solo', planner_provider:'openrouter', planner_model:'openai/gpt-5.4-mini',
    execution_host:'cloudflare', motion_reference_method:'source_motion', timestamp_basis:'generation_completed',
    duration:6.042, bytes:1234567, source_photo_sha256:['1'.repeat(64)]};
  const bay = {...momoko, sequence_slot:'2026-11-23-bay', source_url:'https://www.tiktok.com/@mybaybie/video/7709762212679683342',
    material_group:'male', trend_id:'tiktok-7709762212679683342', motion_reference_method:'observed_description'};
  let item;
  t.mock.method(globalThis, 'fetch', async () => new Response(manifest([item])));
  for (const eligible of [momoko, bay]) {
    item = eligible;
    const [film] = await listFilms();
    assert.equal(film.namedSlot, item.sequence_slot);
    assert.equal(film.autoEligible, false);
  }
  for (const patch of [
    {sequence_slot:bay.sequence_slot}, {sequence_slot:'2026-11-24-momoko'}, {source_url:bay.source_url}, {trend_id:bay.trend_id},
    {material_group:'male'}, {planner_model:'other/model'}, {planner_provider:'test'}, {selection_basis:'suggested'},
    {execution_host:'local'}, {trend_verified:true}, {source_photo_sha256:[]}, {source_photo_ids:[]},
    {motion_reference_method:'photo_sequence'}, {duration:0}, {bytes:17*1024*1024}, {timestamp_basis:'workflow_update'},
    {created_at:'2026-11-23T13:59:00Z',generated_at:'2026-11-23T13:59:00Z'},
    {created_at:'2026-11-24T05:00:00Z',generated_at:'2026-11-24T05:00:00Z'}, {generated_at:'2026-11-24T12:00:00Z'},
  ]) {
    item = {...momoko,...patch};
    const [film] = await listFilms();
    assert.equal(film.namedSlot, undefined, JSON.stringify(patch));
    assert.equal(film.autoEligible, false);
  }
});
