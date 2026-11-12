import base64
from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import Store, Server, APIError

CATALOG=Path(__file__).resolve().parents[1]/'library'/'catalog.json'
BUNDLED=json.loads(CATALOG.read_text()) if CATALOG.exists() else {'assets':[],'trends':[]}

class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(self.tmp.name)
        self.server=Server(('127.0.0.1',0),self.store)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.port=self.server.server_port
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.tmp.cleanup()
    def req(self,path,method='GET',body=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=3)
        h={'Content-Type':'application/json'};h.update(headers or {})
        conn.request(method,path,json.dumps(body) if body is not None else None,h)
        res=conn.getresponse();raw=res.read();status=res.status;content_type=res.getheader('Content-Type');conn.close()
        return status,json.loads(raw) if content_type and 'json' in content_type else raw
    def create(self,**kw):
        status,job=self.req('/api/jobs','POST',{'trend_id':'trend-1',**kw})
        self.assertEqual(status,201,job);return job
    def upload(self,**kw):
        body=dict(name='Owned still',kind='image',duration=12,aspect_ratio='9:16',tags=['morning','coffee','routine'],rights='owned',approved=True,
                  data_url='data:image/png;base64,'+base64.b64encode(b'\x89PNG\r\n\x1a\n'+b'example').decode())
        body.update(kw);return self.req('/api/assets','POST',body)
    def test_seeds_and_persistence(self):
        status,state=self.req('/api/state');self.assertEqual(status,200)
        self.assertEqual(len(state['trends']),6+len(BUNDLED['trends']));self.assertEqual(len(state['assets']),6+len(BUNDLED['assets']))
        self.assertTrue(all(t['is_demo'] for t in state['trends']))
        self.assertEqual(Store(self.tmp.name).data,state)
    def test_idore_character_templates(self):
        for trend in self.store.data['trends']:
            if not trend.get('is_template'): continue
            plan = self.store.match({'trend_id':trend['id'], 'use_demo':False})
            self.assertEqual(plan['owned_ratio'],1)
            selected = [self.store.find('assets',s['asset_id']) for s in plan['segments']]
            self.assertEqual(len({a['character'] for a in selected}),1)
            self.assertEqual(len({a['id'] for a in selected}),len(trend['beats']))
            for asset in selected:
                self.assertTrue((self.store.media / asset['url'].split('/')[-1]).is_file())
    def test_demo_toggle_and_rights(self):
        _,plan=self.req('/api/match','POST',{'trend_id':'trend-1'})
        self.assertEqual(plan['owned_ratio'],1)
        _,plan=self.req('/api/match','POST',{'trend_id':'trend-1','use_demo':False})
        self.assertEqual(plan['owned_ratio'],0)
        self.store.data['assets'][0]['rights']='unconfirmed';self.store.data['assets'][5]['approved']=False
        _,plan=self.req('/api/match','POST',{'trend_id':'trend-1','asset_ids':['asset-demo-1','asset-demo-6']})
        self.assertEqual(plan['owned_ratio'],0)
    def test_no_overlap_and_aspect(self):
        _,plan=self.req('/api/match','POST',{'trend_id':'trend-1','asset_ids':['asset-demo-1']})
        self.assertEqual([s['source_in'] for s in plan['segments']],[0,4,8])
        self.req('/api/assets/asset-demo-1','PATCH',{'duration':4})
        _,plan=self.req('/api/match','POST',{'trend_id':'trend-1','asset_ids':['asset-demo-1']})
        self.assertEqual(plan['generation_seconds'],8)
        self.req('/api/config','PUT',{'aspect_ratio':'16:9'})
        _,plan=self.req('/api/match','POST',{'trend_id':'trend-1'})
        self.assertEqual(plan['eligible_assets'],0)
    def test_upload_and_delete(self):
        status,asset=self.upload();self.assertEqual(status,201,asset)
        status,raw=self.req(asset['url']);self.assertEqual(status,200);self.assertTrue(raw.startswith(b'\x89PNG'))
        _,plan=self.req('/api/match','POST',{'trend_id':'trend-1','asset_ids':[asset['id']],'use_demo':False})
        self.assertEqual(plan['owned_ratio'],1);self.assertEqual(plan['segments'][0]['render_mode'],'still_image')
        status,_=self.req('/api/assets/'+asset['id'],'DELETE',{});self.assertEqual(status,200)
        self.assertEqual(self.req(asset['url'])[0],404)
    def test_invalid_upload(self):
        self.assertEqual(self.upload(rights='unconfirmed')[0],400)
        self.assertEqual(self.upload(tags=[])[0],400)
        self.assertEqual(self.upload(approved='yes')[0],400)
        self.assertEqual(self.upload(data_url='data:image/png;base64,YmFk')[0],400)
        self.assertEqual(self.upload(kind='video')[0],400)
    def test_job_lifecycle_and_export(self):
        job=self.create(idempotency_key='one')
        same=self.create(idempotency_key='one');self.assertEqual(job['id'],same['id'])
        path='/api/jobs/'+job['id']
        self.assertEqual(self.req(path+'/review','POST',{'decision':'approved'})[0],409)
        old=(datetime.now(timezone.utc)-timedelta(seconds=5)).isoformat()
        self.store.data['jobs'][0]['run_started_at']=old
        _,job=self.req(path);self.assertEqual(job['status'],'pending_review')
        _,job=self.req(path+'/review','POST',{'decision':'revise','note':'Add detail'})
        self.assertEqual(job['status'],'needs_revision')
        _,job=self.req(path+'/retry','POST',{});self.assertEqual(job['status'],'queued')
        _,job=self.req(path+'/cancel','POST',{});self.assertEqual(job['status'],'cancelled')
        _,manifest=self.req(path+'/export');self.assertEqual(manifest['videos_generated'],0)
        self.assertFalse(manifest['published']);self.assertIsNone(manifest['job']['result_url'])
        self.assertEqual(self.req('/api/assets/asset-demo-1','DELETE',{})[0],409)
    def test_limit_and_configuration(self):
        self.create();self.create();self.create()
        self.assertEqual(self.req('/api/jobs','POST',{'trend_id':'trend-1'})[0],429)
        for body in ({'mode':'live'},{'generation_provider':'seedance'},{'max_daily_jobs':True},{'minimum_owned_ratio':2},{'api_base_url':'https://user:secret@example.com'}):
            self.assertEqual(self.req('/api/config','PUT',body)[0],400)
        self.assertEqual(self.req('/api/config','PUT',{'api_base_url':'https://example.com/v1','generation_model':'pending-verification'})[0],200)
    def test_request_security(self):
        self.assertEqual(self.req('/api/state',headers={'Host':'evil.example'})[0],403)
        self.assertEqual(self.req('/api/jobs','POST',{},headers={'Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.req('/api/jobs','POST',{},headers={'Content-Type':'text/plain'})[0],415)
        self.assertEqual(self.req('/api/jobs','POST',{},headers={'Sec-Fetch-Site':'cross-site'})[0],403)
        self.assertEqual(self.req('/%2e%2e/server.py')[0],400)
        self.assertEqual(self.req('/media/../../server.py')[0],400)
        self.assertEqual(self.req('/server.py')[0],404)
    def test_invalid_json_and_ids(self):
        conn=http.client.HTTPConnection('127.0.0.1',self.port)
        conn.request('POST','/api/jobs','{bad',{'Content-Type':'application/json'})
        res=conn.getresponse();self.assertEqual(res.status,400);res.read();conn.close()
        self.assertEqual(self.req('/api/match','POST',{'trend_id':'missing'})[0],404)
        self.assertEqual(self.req('/api/match','POST',{'trend_id':'trend-1','asset_ids':['missing']})[0],404)
        self.assertEqual(self.req('/api/match','POST',{'trend_id':'trend-1','use_demo':'false'})[0],400)
    def test_empty_selection_and_global_demo_disable(self):
        _,plan=self.req('/api/match','POST',{'trend_id':'trend-1','asset_ids':[]})
        self.assertEqual(plan['eligible_assets'],0)
        self.assertEqual(plan['generation_seconds'],12)
        self.req('/api/config','PUT',{'demo_assets_enabled':False})
        _,plan=self.req('/api/match','POST',{'trend_id':'trend-1','use_demo':True})
        self.assertEqual(plan['eligible_assets'],len(BUNDLED['assets']))
        self.assertEqual(plan['owned_ratio'],0)
        _,asset=self.upload(approved=False)
        _,plan=self.req('/api/match','POST',{'trend_id':'trend-1','asset_ids':[asset['id']]})
        self.assertEqual(plan['owned_ratio'],0)
        self.req('/api/assets/'+asset['id'],'PATCH',{'approved':True})
        _,plan=self.req('/api/match','POST',{'trend_id':'trend-1','asset_ids':[asset['id']]})
        self.assertEqual(plan['owned_ratio'],1)
        self.assertFalse(any(seg.get('is_demo') for seg in plan['segments']))
    def test_bad_inputs_return_json_errors(self):
        cases=[('/api/config','PUT',{'api_base_url':'http://['}),
               ('/api/config','PUT',{'minimum_owned_ratio':float('nan')}),
               ('/api/config','PUT',{'aspect_ratio':{}}),
               ('/api/config','PUT',{'api_key':'never-store'}),
               ('/api/jobs','POST',{'trend_id':'trend-1','idempotency_key':True}),
               ('/api/match','POST',{'trend_id':'trend-1','asset_ids':{}}),
               ('/api/assets/asset-demo-1','PATCH',{'tags':[{}]}),
               ('/api/assets/asset-demo-1','PATCH',{'duration':False}),
               ('/api/assets/asset-demo-1','PATCH',{'name':'x'*201}),
               ('/api/assets/asset-demo-1','PATCH',{'url':'https://example.com'})]
        for path,method,body in cases:
            with self.subTest(body=body):
                status,response=self.req(path,method,body)
                self.assertEqual(status,400,response);self.assertIsInstance(response['error'],str)
        self.assertNotIn('api_key',self.store.data['config'])
    def test_body_size_rejected_before_read(self):
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=3)
        conn.request('POST','/api/assets',b'',{'Content-Type':'application/json','Content-Length':str(51*1024*1024)})
        response=conn.getresponse();self.assertEqual(response.status,413)
        self.assertIn('error',json.loads(response.read()));conn.close()
    def test_job_persists_simulation_and_idempotency(self):
        job=self.create(idempotency_key='persisted')
        self.store.data['jobs'][0]['run_started_at']=(datetime.now(timezone.utc)-timedelta(seconds=2)).isoformat()
        self.store.save()
        restored=Store(self.tmp.name);restored.progress()
        self.assertEqual(restored.data['jobs'][0]['status'],'simulating')
        same=restored.create_job({'trend_id':'trend-2','idempotency_key':'persisted'})
        self.assertEqual(same['id'],job['id'])
        self.assertEqual(same['trend_id'],'trend-1')
        self.assertEqual(len(restored.data['jobs']),1)

    def test_media_byte_ranges(self):
        _,asset=self.upload()
        raw=(self.store.media/Path(asset['url']).name).read_bytes();size=len(raw)
        def request(value=None,method='GET',extra=None):
            conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=3)
            headers={} if value is None else {'Range':value}
            headers.update(extra or {})
            conn.request(method,asset['url'],headers=headers)
            response=conn.getresponse();body=response.read();result=(response.status,dict(response.getheaders()),body)
            conn.close();return result
        status,headers,body=request()
        self.assertEqual(status,200);self.assertEqual(body,raw);self.assertEqual(headers['Accept-Ranges'],'bytes')
        for value,start,end in [('bytes=0-3',0,3),('bytes=4-',4,size-1),('bytes=-4',size-4,size-1),
                                ('bytes=0-999999',0,size-1),('bytes=-999999',0,size-1),('bytes=2-2',2,2)]:
            with self.subTest(value=value):
                status,headers,body=request(value)
                self.assertEqual(status,206);self.assertEqual(body,raw[start:end+1])
                self.assertEqual(headers['Content-Range'],f'bytes {start}-{end}/{size}')
                self.assertEqual(int(headers['Content-Length']),end-start+1)
                self.assertEqual(headers['X-Content-Type-Options'],'nosniff')
                self.assertIn('Content-Security-Policy',headers)
                status,headers,body=request(value,'HEAD')
                self.assertEqual(status,206);self.assertEqual(body,b'')
                self.assertEqual(int(headers['Content-Length']),end-start+1)
        for value in ['bytes=999999-','bytes=5-2','bytes=-0','bytes=-','bytes=0-2,4-6','items=0-2','bytes=x-y', 'bytes='+('9'*5000)+'-']:
            with self.subTest(value=value[:50]):
                status,headers,body=request(value)
                self.assertEqual(status,416);self.assertEqual(headers['Content-Range'],f'bytes */{size}')
                self.assertIn('error',json.loads(body))
                status,headers,body=request(value,'HEAD')
                self.assertEqual(status,416);self.assertEqual(body,b'')
        status,headers,body=request(None,'HEAD')
        self.assertEqual(status,200);self.assertEqual(body,b'');self.assertEqual(int(headers['Content-Length']),size)
        status,headers,body=request('bytes=0-2',extra={'If-Range':'"unknown-validator"'})
        self.assertEqual(status,200);self.assertEqual(body,raw)
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=3)
        conn.putrequest('GET',asset['url']);conn.putheader('Range','bytes=0-2');conn.putheader('Range','bytes=4-6');conn.endheaders()
        response=conn.getresponse();self.assertEqual(response.status,416);response.read();conn.close()

    def test_approved_remains_simulation(self):
        job=self.create();self.store.data['jobs'][0]['run_started_at']=(datetime.now(timezone.utc)-timedelta(seconds=5)).isoformat()
        _,job=self.req('/api/jobs/'+job['id']+'/review','POST',{'decision':'approved'})
        self.assertEqual(job['status'],'approved');self.assertTrue(job['is_simulation']);self.assertIsNone(job['result_url'])

if __name__=='__main__':unittest.main()

