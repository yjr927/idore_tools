import fs from 'node:fs/promises';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import {webcrypto, createHash} from 'node:crypto';

export async function run() {
  const app = (await fs.readFile(new URL('../site/create.js', import.meta.url), 'utf8')).replace(/\ninit\(\);\s*$/, '');
  const html = await fs.readFile(new URL('../site/create.html', import.meta.url), 'utf8');
  const bytes = Uint8Array.from([0,0,0,24,102,116,121,112,105,115,111,109,0,0,0,0]);
  const manifest = {status:'reviewed',character:'KAI',source_id:'7706633304702848276',model:'bytedance/seedance-2.5',duration:6,
    media_url:'https://raw.githubusercontent.com/yjr927/idore_tools/'+'a'.repeat(40)+'/reference-assets/clay-bizzyboom.mp4',sha256:createHash('sha256').update(bytes).digest('hex')};
  const model = {id:manifest.model,supported_durations:[4,6,8],supported_resolutions:['480p','720p'],supported_aspect_ratios:['16:9','9:16']};
  const checks = [];
  function harness() {
    const state={manifest:structuredClone(manifest),posts:[],requests:[],ambiguous:false};
    class Element {
      constructor(tag='div') { this.tagName=tag;this.value='';this.hidden=false;this.disabled=false;this.checked=false;this.textContent='';this.className='';this.children=[];this.listeners={};this.duration=6;this.videoWidth=576;this.videoHeight=1024; }
      append(...items) { this.children.push(...items); }
      replaceChildren(...items) { this.children=items; }
      addEventListener(type, fn) { this.listeners[type]=fn; }
      setAttribute(name,value) { this[name]=value; }
      removeAttribute(name) { this[name]=''; }
      load() { if(this.src&&this.onloadedmetadata)queueMicrotask(()=>this.onloadedmetadata?.()); }
    }
    const elements=new Map([...html.matchAll(/id="([^"]+)"/g)].map(match=>['#'+match[1],new Element()]));
    const local=new Map(), session=new Map();
    function storage(map){return{getItem:k=>map.get(k)||null,setItem:(k,v)=>map.set(k,String(v)),removeItem:k=>map.delete(k)}}
    function response(url, raw, status=200) { return {ok:status===200,status,url,headers:{get:()=>null},text:async()=>typeof raw==='string'?raw:JSON.stringify(raw),json:async()=>raw,body:{getReader:()=>{let done=false;return{read:async()=>{if(done)return{done:true};done=true;return{done:false,value:bytes}},cancel:async()=>{}}}}}; }
    const location={href:'https://yjr927.github.io/idore_tools/create.html',origin:'https://yjr927.github.io',pathname:'/idore_tools/create.html',search:'',hash:'',assign:value=>{state.redirect=value;}};
    const context=vm.createContext({document:{querySelector:s=>elements.get(s),createElement:tag=>new Element(tag)},location,history:{replaceState(){}},localStorage:storage(local),sessionStorage:storage(session),navigator:{locks:{request:async(_n,_o,fn)=>fn({})}},window:{addEventListener(){}},URL,URLSearchParams,crypto:webcrypto,TextEncoder,TextDecoder,Uint8Array,Date,Intl,Map,Set,Math,JSON,Object,Number,String,Error,Promise,console,setTimeout,clearTimeout,setInterval:()=>0,btoa:value=>Buffer.from(value,'binary').toString('base64'),
      fetch:async(url,options={})=>{state.requests.push({url,options});if(url.endsWith('/clay-reference.json'))return response(url,state.manifest||'',state.manifest?200:404);if(url===manifest.media_url)return response(url,bytes);if(url.endsWith('/videos/models'))return response(url,{data:[model,{...model,id:'bytedance/seedance-2.0-fast'}]});if(url.endsWith('/videos')){state.posts.push(JSON.parse(options.body));if(state.ambiguous)throw Error('fake-network');return response(url,{id:'gen-test-clay-123',status:'pending'});}throw Error('Unexpected non-mocked request');}});
    vm.runInContext(app,context);
    const evalApp=code=>vm.runInContext(code,context);
    const ready=async()=>{evalApp('readJournal(); paintConnection();');await evalApp('loadModels()');await evalApp('loadClayPreset()');await evalApp('useClayPreset()');};
    const pay=()=>evalApp("sessionKey='mock-private-key'; document.querySelector('#source-consent').checked=true; document.querySelector('#paid-consent').checked=true; submit({preventDefault(){}})");
    return {state,elements,local,session,location,evalApp,ready,pay};
  }
  {
    const h=harness();h.state.manifest=null;await h.evalApp('loadClayPreset()');assert.equal(h.elements.get('#clay-preset').hidden,true);assert.equal(h.state.posts.length,0);
    for(const patch of [{status:'pending'},{character:'OTHER'},{source_id:'1'},{model:'bytedance/seedance-2.0'},{duration:7},{sha256:'bad'},{media_url:manifest.media_url.replace('a'.repeat(40),'main')},{media_url:manifest.media_url+'?token=x'},{media_url:manifest.media_url.replace('idore_tools','other')}]){assert.equal(h.evalApp('validateClayPreset('+JSON.stringify({...manifest,...patch})+')'),null);}
    checks.push('absent/invalid manifests never expose a preset');
  }
  {
    const h=harness();await h.ready();assert.equal(h.state.posts.length,0);assert.equal(h.elements.get('#reference-material').value,'male');assert.equal(h.elements.get('#reference-cast').value,'solo');assert.equal(h.elements.get('#model').value,manifest.model);assert.equal(h.elements.get('#duration').value,'6');assert.equal(h.elements.get('#source-video').src,manifest.media_url);assert.equal(h.elements.get('#paid-consent').checked,false);assert.equal(h.evalApp('preparedReference.kind'),'clay');assert.match(h.evalApp('motionPrompt()'),/Image 1/);assert.match(h.evalApp('motionPrompt()'),/Video 1/);assert.match(h.evalApp('motionPrompt()'),/warm wood floor/);
    for(const request of h.state.requests.filter(r=>!r.url.startsWith('https://openrouter.ai'))){assert.equal(request.options.credentials,'omit');assert.equal(request.options.headers?.Authorization,undefined);}
    await h.evalApp('connect()');const callback=new URL(new URL(h.state.redirect).searchParams.get('callback_url'));assert.equal(callback.searchParams.get('clay'),manifest.sha256);assert.equal([...h.session.values()].some(value=>value.includes('mock-private-key')),false);
    checks.push('click only verifies/previews real media; exact parameters, clear consent, OAuth choice retained');
  }
  {
    const h=harness();h.evalApp('readJournal();');await h.evalApp('loadClayPreset()');await h.evalApp('useClayPreset()');assert.equal(h.evalApp('clayModelRequested'),true);await h.evalApp('loadModels()');assert.equal(h.elements.get('#model').value,manifest.model);assert.equal(h.state.posts.length,0);
    h.elements.get('#model').value='bytedance/seedance-2.0-fast';h.evalApp('configureModel()');await h.pay();assert.equal(h.state.posts.length,0);assert.match(h.elements.get('#generation-message').textContent,/Seedance 2.5/);
    checks.push('preset before catalog resolves correctly; incompatible model blocks paid submission');
    const restored=harness();restored.location.search='?clay='+manifest.sha256;restored.location.href+=restored.location.search;await restored.evalApp('init()');assert.equal(restored.evalApp('preparedReference.kind'),'clay');assert.equal(restored.elements.get('#model').value,manifest.model);assert.equal(restored.state.posts.length,0);
    checks.push('returning from OAuth restores reviewed clay selection without a paid call');
  }
  {
    const h=harness();h.state.manifest.sha256='0'.repeat(64);await h.ready();assert.equal(h.evalApp('preparedReference'),null);await h.pay();assert.equal(h.state.posts.length,0);assert.match(h.elements.get('#reference-message').textContent,/不一致/);
    const withdrawn=harness();await withdrawn.ready();withdrawn.state.manifest=null;await withdrawn.pay();assert.equal(withdrawn.state.posts.length,0);assert.match(withdrawn.elements.get('#generation-message').textContent,/暂不可用/);
    checks.push('wrong media hash and withdrawn review both stop before POST');
  }
  {
    const h=harness();await h.ready();await h.pay();assert.equal(h.state.posts.length,1);const payload=h.state.posts[0];assert.equal(payload.model,manifest.model);assert.equal(payload.input_references[0].type,'image_url');assert.equal(payload.input_references[1].video_url.url,manifest.media_url);assert.equal(payload.frame_images,undefined);await h.pay();assert.equal(h.state.posts.length,1);
    const saved=[...h.local.values()].join('');assert.equal(saved.includes(manifest.media_url),false);assert.equal(saved.includes('mock-private-key'),false);assert.equal(saved.includes('Task: new reference-to-video'),false);
    checks.push('only explicit consent pays once through original journal; no private material/key/prompt persisted');
  }
  {
    const h=harness();await h.ready();h.state.ambiguous=true;await h.pay();await h.pay();assert.equal(h.state.posts.length,1);assert.equal(h.evalApp('journal[0].status'),'unknown');
    h.elements.get('#reference-input').value='https://www.tiktok.com/@bizzyboom/video/7706633304702848276';h.evalApp('invalidateReference(); classifySource();');assert.match(h.evalApp('knownSourceRejection()'),/已阻止/);assert.equal(h.evalApp('activeClayPreset'),null);
    checks.push('uncertain submission cannot repeat; original real-person rejection stays blocked');
  }
  return {passed:checks.length,checks};
}
