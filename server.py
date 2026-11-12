"""Loopback-only offline planning app. Python 3.10+, no third-party dependencies."""
from __future__ import annotations
import argparse
import base64
import copy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import shutil
import os
import mimetypes
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit, unquote
import uuid
from discovery import DiscoveryError
from discovery_store import DiscoveryLibrary

ROOT = Path(__file__).resolve().parent
CONFIG = dict(region='ALL', category='all', minimum_owned_ratio=.8,
              generation_provider='not_configured', generation_model='', aspect_ratio='9:16',
              default_duration=12, max_daily_jobs=3, api_base_url='', asset_source='local',
              focus_tags=[], model_label='Seedance 2.5', demo_assets_enabled=True)
MAX_BODY = 50 * 1024 * 1024

class APIError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status

def now():
    return datetime.now(timezone.utc).isoformat()

def string(value, field, maximum=200, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise APIError(f'{field} must be text, {1 if not empty else 0}–{maximum} characters.')
    return value.strip()

def number(value, field, low=0, high=3600):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise APIError(f'{field} must be a number between {low} and {high}.')
    return value

def tags(value, empty=False):
    if not isinstance(value, list) or len(value) > 30 or (not value and not empty):
        raise APIError('tags must be a nonempty list of up to 30 tags.')
    return list(dict.fromkeys(string(v, 'tag', 50).lower() for v in value))

def boolean(value, field):
    if type(value) is not bool:
        raise APIError(f'{field} must be true or false.')
    return value

def ratio(value):
    if value not in ('9:16', '16:9', '1:1', '4:5'):
        raise APIError('Unsupported aspect_ratio.')
    return value

def seeds():
    rows = [
        ('晨间仪式感', 'lifestyle', 'US', ['morning','coffee','routine'], '用一个细节，开启今天', 'peach'),
        ('产品细节三连', 'product', 'US', ['product','detail','hands'], '这个细节值得近看', 'mint'),
        ('一杯咖啡的时间', 'food', 'UK', ['coffee','food','pour'], '把日常拍成小电影', 'lavender'),
        ('宠物的反差瞬间', 'pets', 'JP', ['pet','cute','reaction'], '前一秒与后一秒', 'blue'),
        ('桌面焕新记录', 'lifestyle', 'UK', ['desk','product','routine'], '给生活腾出一点空间', 'mint'),
        ('美食制作切片', 'food', 'US', ['food','hands','detail'], '从准备到完成，只需三幕', 'peach')]
    trends = []
    for i,(title,cat,region,tt,hook,tone) in enumerate(rows):
        trends.append(dict(id=f'trend-{i+1}', title=title, category=cat, region=region, views=1250000-i*127000,
            growth=186-i*17, score=96-i*3, duration=12, tags=tt, hook=hook,
            description='示例趋势，仅用于体验流程；不是实时 TikTok 数据。',
            beats=[dict(label=label,duration=4,tags=[tag]) for label,tag in zip(['开场钩子','细节展开','收尾记忆点'],tt)],
            tone=tone,is_demo=True,source_url=None))
    aa = [('晨间咖啡片段','video',['morning','coffee','routine','pour']),('产品近景素材','video',['product','detail','hands']),
          ('厨房备料片段','video',['food','hands','detail']),('宠物日常片段','video',['pet','cute','reaction']),
          ('整洁桌面照片','image',['desk','product','routine']),('自然光生活片段','video',['morning','routine','coffee'])]
    assets=[dict(id=f'asset-demo-{i+1}',name=n,kind=k,duration=16,aspect_ratio='9:16',tags=t,rights='owned',approved=True,
                 is_demo=True,url=None,tone=rows[i][5],created_at=now()) for i,(n,k,t) in enumerate(aa)]
    return dict(trends=trends,assets=assets,jobs=[],config=copy.deepcopy(CONFIG),mode='simulation')

class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.media = self.directory / 'media'
        self.media.mkdir(exist_ok=True)
        self.path = self.directory / 'state.json'
        self.lock = threading.RLock()
        self.data = json.loads(self.path.read_text()) if self.path.exists() else seeds()
        catalog_path = ROOT / 'library' / 'catalog.json'
        if catalog_path.exists() and not self.data.get('idore_catalog_v1'):
            catalog = json.loads(catalog_path.read_text())
            for asset in catalog['assets']:
                shutil.copy2(ROOT / 'library' / Path(asset['url']).name, self.media / Path(asset['url']).name)
            existing = {a['id'] for a in self.data['assets']}
            self.data['assets'].extend(a for a in catalog['assets'] if a['id'] not in existing)
            self.data['trends'] = catalog['trends'] + self.data['trends']
            self.data['idore_catalog_v1'] = True
        self.save()
    def save(self):
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(self.path)
    def progress(self):
        changed = False
        for job in self.data['jobs']:
            if job['status'] in ('queued','simulating'):
                age = time.time() - datetime.fromisoformat(job.get('run_started_at', job['created_at'])).timestamp()
                status = 'pending_review' if age >= 4 else ('simulating' if age >= .8 else 'queued')
                if job['status'] != status:
                    job['status'],job['updated_at'] = status,now()
                    changed = True
        if changed: self.save()
    def find(self, collection, ident):
        for row in self.data[collection]:
            if row['id'] == ident: return row
        raise APIError(f'{collection[:-1]} not found.',404)
    def match(self, body):
        trend = self.find('trends', string(body.get('trend_id'), 'trend_id'))
        ids = body.get('asset_ids')
        if ids is not None:
            if not isinstance(ids,list) or len(ids)>100 or any(not isinstance(i,str) for i in ids):
                raise APIError('asset_ids must be a list of up to 100 asset IDs.')
            for ident in ids: self.find('assets',ident)
        use_demo = boolean(body.get('use_demo',True),'use_demo') and self.data['config']['demo_assets_enabled']
        assets=[a for a in self.data['assets'] if a['rights']=='owned' and a['approved'] and
                a['aspect_ratio']==self.data['config']['aspect_ratio'] and (not a['is_demo'] or use_demo) and
                (ids is None or a['id'] in ids)]
        used,segments,total,owned = {},[],0,0
        for i,beat in enumerate(trend['beats']):
            duration=beat['duration']; required=set(beat['tags']); ranked=[]
            for asset in assets:
                overlap=len(required & set(asset['tags']))/max(len(required),1)
                available=asset['duration']-used.get(asset['id'],0) if asset['kind']=='video' else trend['duration']-used.get(asset['id'],0)
                if overlap and available >= duration:
                    ranked.append((-overlap, available, asset['id'],asset))
            chosen=sorted(ranked,key=lambda x:x[:3])[0] if ranked else None
            seg=dict(index=i,label=beat['label'],duration=duration,start=total,mode='ai',asset_id=None,match_score=0)
            if chosen:
                asset=chosen[3]; start=used.get(asset['id'],0)
                seg.update(mode='owned',asset_id=asset['id'],match_score=-chosen[0],asset_kind=asset['kind'],source_in=start,
                           source_out=start+duration,is_demo=asset['is_demo'],render_mode='still_image' if asset['kind']=='image' else 'clip')
                used[asset['id']]=start+duration; owned+=duration
            segments.append(seg);total+=duration
        notes=['仅模拟分镜计划：不会生成视频、调用外部 API 或发布。', '匹配基于标签，不分析媒体画面。上传视频时长由用户填写。']
        if any(s.get('is_demo') for s in segments): notes.append('含示例素材元数据，没有对应真实媒体。')
        if owned/total < self.data['config']['minimum_owned_ratio']: notes.append('自有素材占比不足，需要补充素材。')
        return dict(trend_id=trend['id'],segments=segments,duration=total,owned_ratio=round(owned/total,4),
                    generation_seconds=total-owned,minimum_generation_seconds=total-owned,eligible_assets=len(assets),notes=notes,
                    prompt=f"原创竖屏短片：{trend['title']}。开场：{trend['hook']}。优先使用已授权自有素材，仅参考抽象节奏，不复制第三方视频、人物或音乐。")
    def config(self, body):
        unknown=set(body)-set(CONFIG)
        if unknown: raise APIError('Unknown config fields: '+', '.join(sorted(unknown)))
        cleaned={}
        for key,value in body.items():
            if key=='minimum_owned_ratio': value=number(value,key,0,1)
            elif key=='default_duration': value=number(value,key,1,120)
            elif key=='max_daily_jobs':
                if type(value) is not int or not 1<=value<=100: raise APIError('max_daily_jobs must be an integer from 1 to 100.')
            elif key=='demo_assets_enabled': value=boolean(value,key)
            elif key=='focus_tags': value=tags(value,True)
            elif key=='aspect_ratio': value=ratio(value)
            else: value=string(value,key,500,True)
            if key=='generation_provider' and value!='not_configured': raise APIError('Live providers are disabled in this build.')
            if key=='asset_source' and value!='local': raise APIError('Only local assets are supported.')
            if key=='category' and value not in ('all','halloween','lifestyle','product','food','pets'): raise APIError('Unknown category.')
            if key=='api_base_url' and value:
                try: u=urlsplit(value)
                except ValueError: raise APIError('Invalid API base URL.')
                if u.scheme not in ('https','http') or not u.hostname or u.username or u.password or u.query or u.fragment: raise APIError('API URL must be an HTTP(S) base URL without credentials or query parameters.')
            cleaned[key]=value
        self.data['config'].update(cleaned);self.save()
        return self.data['config']
    def add_asset(self, body):
        if body.get('rights')!='owned': raise APIError('Confirm you own or have rights to this media before upload.')
        asset=dict(id='asset-'+uuid.uuid4().hex[:16],name=string(body.get('name'),'name'),kind=body.get('kind'),
                   duration=number(body.get('duration',12),'duration',.1,3600),aspect_ratio=ratio(body.get('aspect_ratio','9:16')),
                   tags=tags(body.get('tags')),rights='owned',approved=boolean(body.get('approved',False),'approved'),
                   is_demo=False,tone='blue',created_at=now())
        if asset['kind'] not in ('image','video'): raise APIError('kind must be image or video.')
        data=body.get('data_url')
        if not isinstance(data,str): raise APIError('An image or video data_url is required.')
        match=re.fullmatch(r'data:(image/(?:png|jpeg|webp|gif)|video/(?:mp4|webm|quicktime));base64,([A-Za-z0-9+/=\r\n]+)',data)
        if not match: raise APIError('Unsupported media data URL. Use PNG, JPEG, WebP, GIF, MP4, WebM or MOV.')
        mime=match[1]
        if not mime.startswith(asset['kind']+'/'): raise APIError('Media type does not match kind.')
        try: raw=base64.b64decode(match[2],validate=True)
        except (ValueError,base64.binascii.Error): raise APIError('Invalid base64 media.')
        valid={'image/png':raw.startswith(b'\x89PNG\r\n\x1a\n'),'image/jpeg':raw.startswith(b'\xff\xd8\xff'),
               'image/gif':raw[:6] in (b'GIF87a',b'GIF89a'),'image/webp':raw[:4]==b'RIFF' and raw[8:12]==b'WEBP',
               'video/mp4':len(raw)>12 and raw[4:8]==b'ftyp','video/quicktime':len(raw)>12 and raw[4:8] in (b'ftyp',b'moov',b'mdat'),
               'video/webm':raw.startswith(b'\x1a\x45\xdf\xa3')}
        if not valid[mime]: raise APIError('File signature does not match its declared media type.')
        ext={'image/png':'png','image/jpeg':'jpg','image/webp':'webp','image/gif':'gif','video/mp4':'mp4','video/webm':'webm','video/quicktime':'mov'}[mime]
        filename=asset['id']+'.'+ext
        (self.media/filename).write_bytes(raw)
        asset['url']='/media/'+filename
        self.data['assets'].insert(0,asset);self.save()
        return asset
    def create_job(self, body):
        key=string(body.get('idempotency_key',''),'idempotency_key',120,True)
        if key:
            for job in self.data['jobs']:
                if job.get('idempotency_key')==key: return job
        today=now()[:10]
        if sum(j['created_at'][:10]==today for j in self.data['jobs'])>=self.data['config']['max_daily_jobs']:
            raise APIError('Daily simulation limit reached. You can raise the limit in Settings.',429)
        plan=self.match(body)
        title=string(body.get('title') or self.find('trends',body['trend_id'])['title'],'title')
        prompt=string(body.get('prompt') or plan['prompt'],'prompt',6000)
        stamp=now();gates=['simulation_only','human_review_required','live_generation_not_configured','publishing_disabled']
        demo=any(s.get('is_demo') for s in plan['segments'])
        if demo:gates.append('demo_assets_not_real_media')
        if plan['owned_ratio']<self.data['config']['minimum_owned_ratio']:gates.append('insufficient_owned_footage')
        job=dict(id='job-'+uuid.uuid4().hex[:16],title=title,trend_id=plan['trend_id'],status='queued',created_at=stamp,updated_at=stamp,
                 run_started_at=stamp,segments=plan['segments'],duration=plan['duration'],owned_ratio=plan['owned_ratio'],
                 generation_seconds=plan['generation_seconds'],prompt=prompt,is_simulation=True,contains_demo_assets=demo,
                 review_note='',result_url=None,execution='simulation_only',gates=gates,idempotency_key=key)
        self.data['jobs'].insert(0,job);self.save();return job
    def job_action(self, ident, action, body):
        job=self.find('jobs',ident)
        if action=='review':
            if job['status']!='pending_review': raise APIError('Only pending_review jobs can be reviewed.',409)
            decision=body.get('decision')
            if decision not in ('approved','revise','rejected'): raise APIError('Invalid review decision.')
            job['review_note']=string(body.get('note',''),'note',2000,True)
            job['status']={'approved':'approved','revise':'needs_revision','rejected':'rejected'}[decision]
        elif action=='retry':
            if job['status'] not in ('needs_revision','rejected','cancelled','failed'): raise APIError('This job cannot be retried in its current state.',409)
            job['status']='queued';job['run_started_at']=now();job['review_note']=''
        elif action=='cancel':
            if job['status'] not in ('queued','simulating'): raise APIError('Only running jobs can be cancelled.',409)
            job['status']='cancelled'
        else: raise APIError('Unknown job action.',404)
        job['updated_at']=now();self.save();return job

class Handler(BaseHTTPRequestHandler):
    server_version='LocalPlanner/1.0'
    def log_message(self, fmt, *args): pass
    def response_headers(self, status, content_type, length, extra=None):
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(length))
        self.send_header('X-Content-Type-Options','nosniff');self.send_header('Cache-Control','no-store')
        self.send_header('Content-Security-Policy',"default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-src https://www.tiktok.com; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.send_header('Referrer-Policy','no-referrer')
        for key,value in (extra or {}).items(): self.send_header(key,value)
        self.end_headers()
    def respond(self, data, status=200, content_type='application/json; charset=utf-8', attachment=None, headers=None):
        raw=json.dumps(data,ensure_ascii=False,allow_nan=False).encode() if not isinstance(data,bytes) else data
        extra=dict(headers or {})
        if attachment: extra['Content-Disposition']=f'attachment; filename="{attachment}"'
        self.response_headers(status,content_type,len(raw),extra)
        if self.command!='HEAD':self.wfile.write(raw)
    def serve_media(self, file):
        """Single byte ranges make browser video seeking work without transcoding."""
        with file.open('rb') as source:
            size=os.fstat(source.fileno()).st_size
            start,end,status=0,size-1,200
            headers={'Accept-Ranges':'bytes'}
            ranges=self.headers.get_all('Range',[])
            # No validators are issued; an If-Range validator cannot match.
            if ranges and not self.headers.get('If-Range'):
                value=ranges[0].strip()
                match=re.fullmatch(r'bytes=([0-9]{0,20})-([0-9]{0,20})',value) if len(ranges)==1 else None
                invalid=not match or not any(match.groups()) or size==0
                if not invalid:
                    first,last=match.groups()
                    if first:
                        start=int(first);end=min(int(last),size-1) if last else size-1
                        invalid=start>=size or end<start
                    else:
                        suffix=int(last)
                        invalid=suffix==0
                        start=max(0,size-suffix);end=size-1
                if invalid:
                    return self.respond({'error':'Invalid or unsatisfiable single byte range.'},416,
                                        headers={'Accept-Ranges':'bytes','Content-Range':f'bytes */{size}'})
                status=206
                headers['Content-Range']=f'bytes {start}-{end}/{size}'
            length=max(0,end-start+1)
            content_type=mimetypes.guess_type(file.name)[0] or 'application/octet-stream'
            self.response_headers(status,content_type,length,headers)
            if self.command=='HEAD': return
            source.seek(start)
            remaining=length
            while remaining:
                chunk=source.read(min(64*1024,remaining))
                if not chunk:break
                self.wfile.write(chunk)
                remaining-=len(chunk)
    def secure(self, write=False):
        host=self.headers.get('Host','')
        if host not in (f'localhost:{self.server.server_port}',f'127.0.0.1:{self.server.server_port}'):
            raise APIError('Only this localhost origin is allowed.',403)
        if write:
            origin=self.headers.get('Origin')
            if origin and origin!='http://'+host:raise APIError('Cross-origin writes are not allowed.',403)
            if self.headers.get('Sec-Fetch-Site') not in (None,'same-origin','none'):raise APIError('Cross-origin writes are not allowed.',403)
            if self.headers.get('Content-Type','').split(';')[0].strip()!='application/json':raise APIError('Content-Type must be application/json.',415)
    def body(self):
        if self.headers.get('Transfer-Encoding'):raise APIError('Chunked requests are not supported.')
        try:length=int(self.headers.get('Content-Length','0'))
        except ValueError:raise APIError('Invalid Content-Length.')
        if not 0<=length<=MAX_BODY:raise APIError('Request exceeds 50 MB limit.',413)
        try:body=json.loads(self.rfile.read(length),parse_constant=lambda v: (_ for _ in ()).throw(ValueError(v)))
        except (ValueError,UnicodeDecodeError):raise APIError('Malformed JSON body.')
        if not isinstance(body,dict):raise APIError('JSON body must be an object.')
        return body
    def do_GET(self):self.dispatch()
    def do_HEAD(self):self.dispatch()
    def do_POST(self):self.dispatch()
    def do_PUT(self):self.dispatch()
    def do_PATCH(self):self.dispatch()
    def do_DELETE(self):self.dispatch()
    def do_OPTIONS(self):self.respond({'error':'Cross-origin access is disabled.'},403)
    def dispatch(self):
        try:
            write=self.command not in ('GET','HEAD');self.secure(write)
            path=unquote(urlsplit(self.path).path)
            if '..' in path or '\\' in path or '\x00' in path:raise APIError('Invalid path.',400)
            store=self.server.store
            if path.startswith('/api/discovery'):
                library=self.server.discovery
                if not write and path=='/api/discovery':
                    with store.lock: return self.respond(library.public())
                if write:
                    body=self.body()
                    if self.command=='POST' and path=='/api/discovery/search': return self.respond(library.start(body),202)
                    if self.command=='POST' and path=='/api/discovery/import': return self.respond(library.add_link(body),201)
                    match=re.fullmatch(r'/api/discovery/runs/([A-Za-z0-9]+)',path)
                    if self.command=='POST' and match: return self.respond(library.check(match[1]))
                    match=re.fullmatch(r'/api/discovery/videos/(tiktok-\d+)/favorite',path)
                    if self.command=='PATCH' and match:
                        with store.lock: return self.respond(library.favorite(match[1],body))
                raise APIError('Discovery endpoint not found.',404)
            with store.lock:
                store.progress()
                if not write:
                    if path=='/api/health':return self.respond(dict(status='ok',mode='simulation',network_calls=0))
                    if path=='/api/state':return self.respond(store.data)
                    m=re.fullmatch(r'/api/jobs/([a-zA-Z0-9-]+)(/export)?',path)
                    if m:
                        job=store.find('jobs',m[1])
                        if m[2]:return self.respond(dict(schema_version=1,mode='simulation',videos_generated=0,network_calls=0,published=False,
                            warning='Planning manifest only. No rendered video exists. Approval does not generate or publish.',job=job),attachment=job['id']+'.json')
                        return self.respond(job)
                    if path.startswith('/media/'):
                        filename=path[len('/media/'):]
                        if not re.fullmatch(r'asset-[a-f0-9]{16}\.(png|jpg|webp|gif|mp4|webm|mov)',filename):raise APIError('Media not found.',404)
                        file=store.media/filename
                    else:
                        paths={'/':'index.html','/index.html':'index.html','/discovery.js':'discovery.js','/app.js':'app.js','/styles.css':'styles.css','/mark.svg':'mark.svg'}
                        if path not in paths:raise APIError('Not found.',404)
                        file=ROOT/'static'/paths[path]
                    if not file.is_file() or file.is_symlink():raise APIError('File not found.',404)
                    if path.startswith('/media/'):return self.serve_media(file)
                    return self.respond(file.read_bytes(),content_type=mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
                body=self.body()
                if self.command=='PUT' and path=='/api/config':return self.respond(store.config(body))
                if self.command=='POST' and path=='/api/assets':return self.respond(store.add_asset(body),201)
                if self.command=='POST' and path=='/api/match':return self.respond(store.match(body))
                if self.command=='POST' and path=='/api/jobs':return self.respond(store.create_job(body),201)
                m=re.fullmatch(r'/api/jobs/([a-zA-Z0-9-]+)/(review|retry|cancel)',path)
                if self.command=='POST' and m:return self.respond(store.job_action(m[1],m[2],body))
                m=re.fullmatch(r'/api/assets/([a-zA-Z0-9-]+)',path)
                if m and self.command in ('PATCH','DELETE'):
                    asset=store.find('assets',m[1])
                    if self.command=='DELETE':
                        if any(s.get('asset_id')==asset['id'] for j in store.data['jobs'] for s in j['segments']):raise APIError('Asset is referenced by an existing plan and cannot be deleted.',409)
                        if asset['url']:(store.media/Path(asset['url']).name).unlink(missing_ok=True)
                        store.data['assets'].remove(asset);store.save();return self.respond(dict(deleted=True))
                    if set(body)-{'name','tags','approved','duration','aspect_ratio'}:raise APIError('Unsupported editable asset field.')
                    clean={}
                    for k,v in body.items():
                        clean[k]={'name':lambda:string(v,'name'),'tags':lambda:tags(v),'approved':lambda:boolean(v,'approved'),
                                  'duration':lambda:number(v,'duration',.1,3600),'aspect_ratio':lambda:ratio(v)}[k]()
                    asset.update(clean);store.save();return self.respond(asset)
                raise APIError('Endpoint or method not found.',404)
        except DiscoveryError as exc:self.respond({'error':str(exc)},400)
        except APIError as exc:self.respond({'error':str(exc)},exc.status)
        except (BrokenPipeError,ConnectionResetError):pass
        except Exception:
            import traceback
            traceback.print_exc()
            self.respond({'error':'Unexpected local server error. See the terminal for details.'},500)

class Server(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self, address, store):
        self.store=store
        self.discovery=DiscoveryLibrary(store)
        super().__init__(address, Handler)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--data-dir',type=Path,default=Path(os.environ.get('APP_DATA_DIR',str(ROOT/'data'))))
    parser.add_argument('--open',action='store_true',help='Open this localhost page in your default browser')
    args=parser.parse_args()
    server=Server(('127.0.0.1',args.port),Store(args.data_dir))
    print(f'Local planner: http://127.0.0.1:{server.server_port}\nSimulation only. Ctrl+C to stop.',flush=True)
    if args.open:
        import webbrowser
        threading.Timer(.3,lambda:webbrowser.open(f'http://127.0.0.1:{server.server_port}')).start()
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()

if __name__=='__main__':main()
