import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const context = vm.createContext({
  location: new URL('https://example.test/gallery.html'),
  URL, AbortSignal, fetch: () => new Promise(() => {}),
  document: {
    addEventListener() {},
    querySelector: () => ({addEventListener() {}, setAttribute() {}})
  },
  window: {addEventListener() {}}, setInterval() {}
});
vm.runInContext(readFileSync(new URL('../site/gallery.js', import.meta.url), 'utf8'), context);
const {dailyOutput, validFilms, referenceBlocker, validNamedSchedule, namedStatus} =
  vm.runInContext('({dailyOutput, validFilms, referenceBlocker, validNamedSchedule, namedStatus})', context);

function film(char, stamp, method = 'agent_motion') {
  const sha256 = char.repeat(64);
  const id = 'video-' + sha256.slice(0, 24);
  return {id, sha256, kind: 'character', generation_method: method,
    video_path: 'generated/' + id + '.mp4', generated_at: stamp};
}

test('daily goal counts distinct completed films on the New York day and only confirmed delivery', () => {
  const day = Date.parse('2026-11-23T02:00:00Z');
  const received = film('a', '2026-11-22T05:00:00Z');
  const processing = film('b', '2026-11-23T02:00:00Z', 'original_character_edit');
  const previous = film('c', '2026-11-22T04:59:59Z');
  const archive = {...film('d', '2026-11-23T00:00:00Z'), timestamp_basis: 'workflow_artifact'};
  const legacy = film('e', '2026-11-22T21:00:00Z', 'photo_sequence');
  const invalid = {...film('f', '2026-11-22T21:00:00Z'), video_path: 'elsewhere.mp4'};
  const films = validFilms([received, received, processing, previous, archive, legacy, invalid]);
  const feed = {connected: true, receipts:{
    [received.sha256]: {state: 'delivered'}, [processing.sha256]: {state: 'processing'},
    [previous.sha256]: {state: 'published'}
  }};
  assert.equal(dailyOutput(films, feed, day).generated, 2);
  assert.equal(dailyOutput(films, feed, day).delivered, 1);
  assert.equal(dailyOutput(films, {connected: false}, day).delivered, null);
  assert.equal(dailyOutput(null, feed, day).generated, null);
});

test('older references show the exact missing classification and preserve material blockers', () => {
  const known = {status:'waiting_classification', material_group:'female', cast_type:'solo', transformation:'unknown'};
  assert.equal(referenceBlocker(known), '待核实换装或妆容变化');
  assert.equal(referenceBlocker({...known, material_group:'unknown'}), '待核实素材分组');
  assert.equal(referenceBlocker({...known, material_group:'unknown', cast_type:'unknown'}), '待核实素材分组和人数');
  assert.equal(referenceBlocker({status:'waiting_material', message:'等待同分组、同人数的角色素材。'}), '等待同分组、同人数的角色素材。');
});

test('a named date counts only the scheduled sources and distinct slots, with true inbox receipts', () => {
  const date = '2026-11-23', stamp = date + 'T15:30:00Z';
  const ref = (label, id) => ({title: label, slot: date + '-' + label, source_id: 'tiktok-' + id, url: 'https://www.tiktok.com/@demo/video/' + id});
  const [first, second] = [ref('one', '12345678901'), ref('two', '12345678902')];
  const schedule = validNamedSchedule({date, start_hour:9, references:[first, second]});
  const named = (char, ref) => ({...film(char, stamp, 'named_reference_motion'), sequence_slot:ref.slot,
    trend_id:ref.source_id, selection_basis:'user_selected_reference'});
  const recorded = named('a', first);
  const films = validFilms([recorded, named('b', first), {...named('c', second), trend_id:first.source_id}, film('d', stamp)]);
  const account = {connected:true, receipts:{[recorded.sha256]:{state:'delivered'}}};
  assert.equal(dailyOutput(films, account, Date.parse(stamp), schedule).generated, 1);
  assert.equal(dailyOutput(films, account, Date.parse(stamp), schedule).delivered, 1);
  assert.equal(namedStatus(first, films, [], account), '已送达 TikTok 收件箱');
  assert.equal(namedStatus(second, films, [], account), '已排期');
  assert.equal(namedStatus(second, films, [{...named('c', second),status:'preparing_motion'}], account), '提取动作中');
  assert.throws(() => validNamedSchedule({...schedule, references:[first, first]}));
  const next = '2026-11-24T15:30:00Z';
  const delayed = validFilms([{...named('e', second), generated_at:next}, film('f', next)]);
  assert.equal(dailyOutput(delayed, account, Date.parse(next), schedule).generated, 2);
});
