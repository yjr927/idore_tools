import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash, generateKeyPairSync, sign} from 'node:crypto';
import {PhotoJob, photoRequest} from '../worker/photo.mjs';

const sha = s => createHash('sha256').update(s).digest('hex');
const secret = 'a'.repeat(64);
const id = sha(secret);
const {privateKey, publicKey} = generateKeyPairSync('rsa',{modulusLength:2048});
const jwk = {...publicKey.export({format:'jwk'}), kid:'test', alg:'RS256'};
const encoded = obj => Buffer.from(JSON.stringify(obj)).toString('base64url');
const png = Buffer.from([137,80,78,71,13,10,26,10,1,2,3,4]);
const input = {prompt:'保留原照片的构图', images:[{mime:'image/png',data:png.toString('base64')}]};

function runner(job = id, changes = {}) {
  const now = Math.floor(Date.now() / 1000), branch = 'refs/heads/main';
  const repo = 'yjr927/idore_tools';
  const header = encoded({alg:'RS256',kid:'test'});
  const payload = encoded({iss:'https://token.actions.githubusercontent.com', aud:'idore-photo:'+job,
    sub:'repo:yjr927@117725385/idore_tools@1454660637:ref:'+branch, repository:repo,
    repository_id:'1454660637', repository_owner_id:'117725385', ref:branch, run_id:'1234', run_attempt:'1',
    workflow_ref:repo+'/.github/workflows/photo-generation.yml@'+branch, event_name:'workflow_dispatch',
    iat:now-5, nbf:now-5, exp:now+600, ...changes});
  return header+'.'+payload+'.'+sign('RSA-SHA256', Buffer.from(header+'.'+payload), privateKey).toString('base64url');
}

function fixture(t) {
  t.mock.method(globalThis, 'fetch', async url => {
    assert.equal(url,'https://token.actions.githubusercontent.com/.well-known/jwks');
    return Response.json({keys:[jwk]});
  });
  const objects = new Map();
  const ns = {idFromName:key=>key, get(key) {
    if (!objects.has(key)) {
      const data = new Map();
      const store = {kv:{get:k=>structuredClone(data.get(k)), put:(k,v)=>data.set(k,structuredClone(v)), delete:k=>data.delete(k)},
        transactionSync:fn=>fn(), setAlarm:async time=>{store.alarmAt=time;}, deleteAll:async()=>{data.clear();}};
      const job = new PhotoJob({storage:store});
      objects.set(key,{job,store,data});
    }
    return {fetch:(url, opts)=>objects.get(key).job.fetch(new Request(url,{...opts, duplex:'half'}))};
  }};
  return {objects, request(op, method, token, body, type='application/json', job=id) {
    const req = new Request('https://studio.example/api/photo/'+job+'/'+op, {
      method, headers:{Authorization:'Bearer '+token,'Content-Type':type,'X-Runner-Id':'forged'},
      body:body === undefined ? undefined : (type === 'application/json' ? JSON.stringify(body) : body),
    });
    return photoRequest(req,{PHOTO_JOBS:ns});
  }};
}

test('private input moves through one trusted reservation, then only its client can read output', async t=>{
  const {request,objects} = fixture(t);
  assert.equal((await request('status','GET',secret)).status,404);
  assert.equal((await request('input','POST',secret,input)).status,404);
  assert.equal(objects.get(id).data.size,0);
  assert.equal((await request('init','POST',secret)).status,401);
  const token=runner();
  assert.deepEqual(await (await request('init','POST',token)).json(),{status:'waiting'});
  assert.ok(objects.get(id).store.alarmAt > Date.now()+29*60000);
  assert.equal((await request('init','POST',runner(id,{run_id:'1235'}))).status,409);
  assert.equal((await request('status','GET','b'.repeat(64))).status,401);
  assert.equal((await request('input','POST',secret,{...input,images:[{mime:'image/jpeg',data:input.images[0].data}]})).status,422);
  assert.equal((await request('input','POST',secret,input)).status,200);
  assert.equal((await request('input','POST',secret,input)).status,409);
  assert.equal((await request('input','GET',secret)).status,401);
  assert.deepEqual(await (await request('input','GET',token)).json(),input);
  const claims = await Promise.all([request('claim','POST',token),request('claim','POST',token)]);
  assert.deepEqual(claims.map(r=>r.status).sort(),[200,409]);
  assert.equal(objects.get(id).data.has('part0'),false);
  assert.equal((await request('input','GET',token)).status,409);
  const largePng = Buffer.concat([png,Buffer.alloc(100*1024)]);
  assert.equal((await request('result','POST',token,largePng,'image/png')).status,200);
  assert.equal(objects.get(id).data.has('part1'),true);
  assert.equal((await request('result','POST',token,png,'image/png')).status,200);
  assert.deepEqual(await (await request('fail','POST',token,{code:'delivery_failed'})).json(),{status:'ready'});
  const result = await request('result','GET',secret);
  assert.equal(result.headers.get('Content-Type'),'image/png');
  assert.ok(result.headers.get('Cache-Control').includes('no-store'));
  assert.deepEqual(Buffer.from(await result.arrayBuffer()),largePng);
  const obj = objects.get(id);
  obj.store.kv.put('meta',{...obj.store.kv.get('meta'),expires:Date.now()-1});
  assert.equal((await request('result','GET',secret)).status,404);
  await obj.job.alarm();
  assert.equal(obj.data.size,0);
});

test('OIDC pins signature, audience, repository IDs, ref, workflow, run and event', async t=>{
  const {request} = fixture(t);
  for (const changes of [{aud:'wrong'}, {iss:'untrusted'}, {workflow_ref:'other'}, {ref:'refs/heads/test'},
    {run_attempt:'2'}, {event_name:'pull_request'}, {event_name:'push'}, {repository:'other'}, {repository_id:'12'},
    {sub:'repo:other:ref:refs/heads/main'}, {exp:0}]) {
    assert.equal((await request('init','POST',runner(id,changes))).status,401);
  }
  const damaged=runner().split('.');
  damaged[2]='AA'+damaged[2].slice(2);
  assert.equal((await request('init','POST',damaged.join('.'))).status,401);
  assert.equal((await request('init','POST',runner(id,{sub:'repo:yjr927/idore_tools:ref:refs/heads/main'}))).status,200);
  const smokeId=sha(sha('idore-photo-smoke:1234'));
  assert.equal((await request('init','POST',runner(smokeId,{event_name:'push'}),undefined,'application/json',smokeId)).status,200);
});

test('rejected input and failed jobs retain no private files or arbitrary upstream text', async t=>{
  const {request,objects} = fixture(t);
  const token=runner();
  await request('init','POST',token);
  assert.equal((await request('input','POST',secret,{...input,images:new Array(5).fill(input.images[0])})).status,422);
  assert.equal((await request('input','POST',secret,{...input,prompt:'   '})).status,422);
  assert.equal((await request('input','POST',secret,input)).status,200);
  assert.equal((await request('fail','POST',token,{code:'private upstream message'})).status,422);
  assert.deepEqual(await (await request('fail','POST',token,{code:'missing_key'})).json(),{status:'failed',code:'missing_key'});
  assert.equal(objects.get(id).data.has('part0'),false);
  assert.equal((await request('result','GET',secret)).status,409);
});
