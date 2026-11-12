"""Real video discovery, with provenance and conservative metadata-only assessment."""
import json
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError

SOURCES = [
    ('SocialPilot 趋势精选','https://www.socialpilot.co/blog/tiktok-trends'),
    ('Later 趋势精选','https://later.com/blog/tiktok-trends/'),
]
PRESETS = {
    'hands': ['hand', 'dance', 'shoulder', 'gesture', 'bounce', 'choreograph'],
    'transition': ['transition', 'costume', 'makeup', 'glow', 'outfit', 'reveal'],
    'pov': ['pov', 'lip', 'expression', 'manifest'],
    'all': [],
}
class DiscoveryError(Exception): pass

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DiscoveryError('数据服务发生重定向，请检查服务状态。')

def request_json(url, token='', body=None):
    headers={'Accept':'application/json','User-Agent':'idore-local/1.1'}
    if token: headers['Authorization']='Bearer '+token
    if body is not None: headers['Content-Type']='application/json'
    req=Request(url, data=None if body is None else json.dumps(body).encode(), headers=headers)
    try:
        with build_opener(NoRedirect).open(req,timeout=25) as res:
            raw=res.read(8*1024*1024+1)
        if len(raw)>8*1024*1024: raise DiscoveryError('数据响应过大，请减少采集数量。')
        return json.loads(raw)
    except HTTPError as exc:
        message={401:'API Key 无效，请重新连接。',402:'数据服务额度不足。',403:'数据服务未授权此操作。',429:'数据服务请求过多，请稍后重试。'}.get(exc.code,'数据服务返回错误，请稍后重试。')
        raise DiscoveryError(message) from None
    except (URLError,TimeoutError,OSError,ValueError):
        raise DiscoveryError('无法读取数据服务。请检查网络后重试；已有视频仍会保留。') from None

def canonical_video(value):
    if not isinstance(value,str): raise DiscoveryError('请提供 TikTok 原视频链接。')
    try: u=urlsplit(value.strip())
    except ValueError: raise DiscoveryError('视频链接无效。')
    try: port=u.port
    except ValueError: raise DiscoveryError('视频链接端口无效。')
    if u.scheme!='https' or u.hostname not in ('www.tiktok.com','tiktok.com') or u.username or u.password or port not in (None,443):
        raise DiscoveryError('仅支持 https://www.tiktok.com/@作者/video/数字 的完整链接。')
    m=re.fullmatch(r'/@([A-Za-z0-9_.-]{1,100})/video/(\d{10,25})/?',u.path)
    if not m: raise DiscoveryError('请复制完整的视频链接，不支持主页、标签页或短链接。')
    return f'https://www.tiktok.com/@{m[1]}/video/{m[2]}',m[1],m[2]

def nonnegative(value):
    return value if isinstance(value,(int,float)) and not isinstance(value,bool) and 0<=value<=1e15 else None

def normalize(item,kind,observed_at):
    url,author,ident=canonical_video(item.get('webVideoUrl') or item.get('url'))
    music=item.get('musicMeta') or {}; video=item.get('videoMeta') or {}
    if not isinstance(music,dict): music={}
    if not isinstance(video,dict): video={}
    created=item.get('createTimeISO')
    if not created and isinstance(item.get('createTime'),(int,float)):
        try: created=datetime.fromtimestamp(item['createTime'],timezone.utc).isoformat()
        except (ValueError,OSError,OverflowError): pass
    try:
        parsed=datetime.fromisoformat(str(created).replace('Z','+00:00'))
        if parsed.tzinfo is None: raise ValueError()
        created=parsed.isoformat()
    except (ValueError,TypeError): created=None
    title=str(item.get('text') or item.get('title') or 'TikTok 视频候选')[:2000]
    duration=nonnegative(video.get('duration'))
    return dict(id='tiktok-'+ident,url=url,video_id=ident,author=author,title=title,kind=kind,
        published_at=created,first_seen_at=observed_at,last_seen_at=observed_at,
        music_id=str(music.get('musicId') or '')[:100],music_name=str(music.get('musicName') or '')[:300],
        duration=duration,metrics={k:nonnegative(item.get(v)) for k,v in [('views','playCount'),('likes','diggCount'),('comments','commentCount'),('shares','shareCount')]},
        source='公开页面',source_url=url,
        assessment='待看原片',assessment_note='搜索与音乐信息仅作为线索；尚未核验人数、动作重复性、固定机位及 AI 复刻效果。',
        motion_verified=False,trend_verified=False,favorite=False)

def import_link(url):
    canonical,author,ident=canonical_video(url)
    data=request_json('https://www.tiktok.com/oembed?'+urlencode({'url':canonical}))
    if data.get('type')!='video': raise DiscoveryError('TikTok 没有返回可用视频。')
    stamp=datetime.now(timezone.utc).isoformat()
    row=normalize({'url':canonical,'title':data.get('title')},'custom',stamp)
    row.update(source='TikTok 官方 oEmbed',source_url=canonical,availability='TikTok 返回了视频预览信息')
    return row


from html.parser import HTMLParser
import html

class ArticleLinks(HTMLParser):
    def __init__(self):
        super().__init__();self.heading='';self.heading_parts=[];self.in_heading=False;self.ignore=0;self.context=[];self.results=[]
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag in ('script','style'): self.ignore+=1
        if tag in ('h2','h3','h4'):
            self.in_heading=True;self.heading_parts=[];self.context=[]
        for key in ('href','cite'):
            value=attrs.get(key,'')
            try:
                url,_,_=canonical_video(value)
                self.results.append(dict(url=url,heading=self.heading,context=' '.join(self.context)[-1600:]))
            except DiscoveryError: pass
    def handle_endtag(self,tag):
        if tag in ('script','style'): self.ignore=max(0,self.ignore-1)
        if tag in ('h2','h3','h4'):
            self.heading=' '.join(self.heading_parts).strip();self.in_heading=False
    def handle_data(self,data):
        if self.ignore:return
        clean=' '.join(data.split())
        if not clean:return
        if self.in_heading:self.heading_parts.append(clean)
        else:self.context.append(clean)

def fetch_article(url):
    req=Request(url,headers={'User-Agent':'Mozilla/5.0 (compatible; idore-local/1.1)'})
    try:
        with build_opener(NoRedirect).open(req,timeout=15) as res:
            raw=res.read(4*1024*1024+1)
        if len(raw)>4*1024*1024: raise DiscoveryError('公开页面过大，已跳过。')
        return raw.decode('utf-8',errors='replace')
    except (HTTPError,URLError,OSError,TimeoutError):
        raise DiscoveryError('公开页面暂不可读取，可能需要登录、验证或稍后重试。') from None

def discover_public(kind,query,limit):
    rows=[];reports=[];seen=set();stamp=datetime.now(timezone.utc).isoformat()
    for name,url in SOURCES:
        try:
            page=fetch_article(url);parser=ArticleLinks();parser.feed(page)
            count=0
            for found in parser.results:
                if found['url'] in seen:continue
                if re.search(r'carousel|slideshow|photo dump',found['heading'],re.I):continue
                text=(found['heading']+' '+found['context']).lower()
                terms=query.lower().split() if query else PRESETS[kind]
                if terms and not any(term in text for term in terms):continue
                if len(rows)>=limit:continue
                row=normalize({'url':found['url'],'title':found['heading']},kind,stamp)
                row.update(source=name,source_url=url,source_heading=found['heading'][:300],
                    source_note='公开趋势文章提供的原视频线索；文章推荐不等于已核验当前热度。',
                    availability='待打开原片确认',assessment_note='按文章标题和玩法描述筛选。尚未观看视频核验单人、重复动作及跟拍一致性。')
                rows.append(row);seen.add(found['url']);count+=1
            reports.append(dict(name=name,url=url,status='ok',found=count,total_links=len(parser.results)))
        except DiscoveryError as exc:
            reports.append(dict(name=name,url=url,status='blocked',message=str(exc),found=0))
    return rows,reports
