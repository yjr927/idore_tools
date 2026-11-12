"""Free public-page discovery and a persistent, deduplicated candidate library."""
import copy
import threading
import uuid
import re
from datetime import datetime,timezone
from discovery import DiscoveryError,discover_public,import_link,PRESETS

class DiscoveryLibrary:
    def __init__(self,store):
        self.store=store;self.busy=False
        data=store.data.setdefault('discovery',dict(videos=[],runs=[]))
        for run in data['runs']:
            if run['status']=='running':
                run.update(status='interrupted',message='服务重启中断了采集，请重新寻找；已有视频保留。')
        store.save()
    def public(self):
        result=copy.deepcopy(self.store.data['discovery'])
        result['videos']=[v for v in result['videos'] if not re.search(r'carousel|slideshow|photo dump',v['title'],re.I)]
        return result
    def merge(self,rows):
        data=self.store.data['discovery'];existing={r['id']:r for r in data['videos']};added=0
        for row in rows:
            if row['id'] in existing:
                previous=existing[row['id']];keep={k:previous[k] for k in ('first_seen_at','favorite')}
                previous.update(row);previous.update(keep)
            else:
                data['videos'].append(row);existing[row['id']]=row;added+=1
        return added
    def start(self,body):
        kind=body.get('kind','hands');query=body.get('query','')
        if kind not in PRESETS or not isinstance(query,str) or len(query)>150: raise DiscoveryError('搜索条件无效。')
        limit=body.get('limit',12)
        if type(limit) is not int or not 1<=limit<=30: raise DiscoveryError('每次可寻找 1–30 条视频。')
        with self.store.lock:
            if self.busy: raise DiscoveryError('已有采集任务进行中，请稍候。')
            runs=self.store.data['discovery']['runs']
            if runs and (datetime.now(timezone.utc)-datetime.fromisoformat(runs[0]['started_at'])).total_seconds()<30:
                raise DiscoveryError('请等待 30 秒后再采集，避免频繁请求公开网站。')
            self.busy=True
            run=dict(id=uuid.uuid4().hex,status='running',kind=kind,query=query.strip(),limit=limit,
                started_at=datetime.now(timezone.utc).isoformat(),added=0,message='正在读取免费公开趋势页面…',sources=[])
            runs.insert(0,run);self.store.save()
        threading.Thread(target=self._collect,args=(run,),daemon=True).start()
        return copy.deepcopy(run)
    def _collect(self,run):
        try:
            rows,reports=discover_public(run['kind'],run['query'],run['limit'])
            with self.store.lock:
                added=self.merge(rows);ok=sum(r['status']=='ok' for r in reports)
                run.update(status=('succeeded' if ok==len(reports) else 'partial') if rows else ('empty' if ok else 'blocked'),
                    added=added,found=len(rows),sources=reports,
                    message=f'找到 {len(rows)} 条原视频线索，新增 {added} 条。来源为免费公开趋势文章；当前热度、人数和跟拍动作待核验。' if rows else '本次没有找到符合条件的原视频。可换类别或关键词；不会添加示例数据。',
                    finished_at=datetime.now(timezone.utc).isoformat())
                self.store.save()
        except Exception:
            with self.store.lock:
                run.update(status='failed',message='采集未完成，请稍后重试。已有视频保留。');self.store.save()
        finally:self.busy=False
    def check(self,ident):
        row=next((r for r in self.store.data['discovery']['runs'] if r['id']==ident),None)
        if not row:raise DiscoveryError('找不到采集任务。')
        return copy.deepcopy(row)
    def add_link(self,body):
        row=import_link(body.get('url'))
        with self.store.lock:
            added=self.merge([row]);self.store.save()
        return {'added':added,'video':row}
    def favorite(self,ident,body):
        if type(body.get('favorite')) is not bool: raise DiscoveryError('收藏状态无效。')
        row=next((r for r in self.store.data['discovery']['videos'] if r['id']==ident),None)
        if not row: raise DiscoveryError('视频不存在。')
        row['favorite']=body['favorite'];self.store.save();return row
