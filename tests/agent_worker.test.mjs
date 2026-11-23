import {test} from 'node:test';
import assert from 'node:assert/strict';
import worker from '../worker/index.mjs';

const id = '7704859708200389902';
const image = 'https://raw.githubusercontent.com/yjr927/idore_tools/' + 'a'.repeat(40) + '/site/characters/Elias/Elias%E6%95%B4%E4%BD%93.png';
function sample() {
  return {slot:'2026-11-22-evening', model:'openai/gpt-5.4-mini',
    trends:[{id:'tiktok-' + id, source_url:'https://www.tiktok.com/@example/video/' + id, title:'Glance back',
      mechanic:'Turns to the camera', notes:'A single cut', material_group:'male',cast_type:'solo',
      metrics:{likes:1000,views:20000}, other_creators:2, observed_at:'2026-11-22T17:02:00Z'}],
    photos:[{photo_id:'elias',name:'Elias',role_id:'elias',ip:'Muyu',material_group:'male',cast_type:'solo',image_url:image}]};
}
function script() {
  return {decision:'render', trend_id:'tiktok-' + id,
    shots:['He saw the camera','One more turn','Caught again'].map((overlay,i) => ({photo_id:'elias', framing:['wide','close','medium'][i],overlay})),
    caption:'That last glance says everything.',hashtags:['#Elias','#glance']};
}
function call(endpoint, input, method = 'POST') {
  return worker.fetch(new Request('https://idore-tools.yjr927.workers.dev/agent/plan/' + endpoint,
    {method, headers:{'Content-Type':'application/json'}, body:method === 'POST' ? JSON.stringify(input) : undefined}), {});
}
test('compiles a fixed multimodal prompt with pinned images and no model host credentials', async t => {
  t.mock.method(globalThis,'fetch', () => { throw Error('unexpected external I/O'); });
  const s = sample();
  const res = await call('request',s);
  assert.equal(res.status,200);
  const payload = await res.json();
  assert.deepEqual(Object.keys(payload).sort(),['messages','model','response_format']);
  assert.equal(payload.response_format.json_schema.strict,true);
  assert.equal(payload.messages[0].role,'system');
  assert.equal(payload.messages[1].content.at(-1).image_url.url,image);
  assert.equal(payload.model,s.model);
  assert.equal((await call('request',null,'GET')).status,405);
});
test('bounded requests and pinned input reject unrelated URLs and wrong source IDs', async () => {
  for (const bad of ['https://evil.example/pic.jpg',image.replace('a'.repeat(40),'main'),
    image.replace('/Elias/','/../'),image + '?foo=1',image.replace('/Elias/','/%2e%2e/')]) {
    const s = sample(); s.photos[0].image_url = bad;
    assert.equal((await call('request',s)).status,422);
  }
  const s = sample();
  s.trends[0].source_url += '0';
  assert.equal((await call('request',s)).status,422);
  assert.equal((await call('request',' '.repeat(270000))).status,413);
  s.model='x/other';
  assert.equal((await (await call('request',s)).json()).error,'missing_model');
});
test('accepts only matched photo, valid copy and source, handles hold without side effects', async t => {
  t.mock.method(globalThis,'fetch', () => { throw Error('unexpected external I/O'); });
  const s = sample(), plan = script();
  const accept = value => call('accept', {slate:s, response:JSON.stringify(value)});
  const reply = await accept(plan);
  assert.deepEqual(await reply.json(),plan);
  assert.deepEqual(await (await accept({decision:'hold'})).json(),{decision:'hold'});
  for (const mutate of [
    x => {x.trend_id='other';},
    x => {x.shots[0].photo_id='unknown';},
    x => {x.shots[0].overlay='watch till the end';},
    x => {x.hashtags[0]='#viral';},
    x => {x.caption=x.shots[0].overlay;},
  ]) {
    const bad=script(); mutate(bad);
    assert.equal((await accept(bad)).status,422);
  }
  s.photos[0].cast_type = 'group';
  assert.equal((await accept(plan)).status,422);
  s.photos[0].cast_type = 'solo';
  s.trends[0].transformation = 'outfit';
  assert.equal((await accept(plan)).status,422);
  s.photos[0].styles_ready = true;
  assert.equal((await accept(plan)).status,200);
});
