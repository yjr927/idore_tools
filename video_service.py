"""Private, explicitly authorized OpenRouter video generation (stdlib only).

The transport accepts Request plus timeout/max_bytes and returns status, headers,
bytes. Tests inject it; the default transport never follows redirects. Submission
is recorded before sending and is never automatically retried.
"""
from __future__ import annotations

import base64
import binascii
import copy
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit, parse_qs
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

BASE = 'https://openrouter.ai/api/v1'
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_VIDEO_BYTES = 128 * 1024 * 1024
MODEL_PATTERN = re.compile(r'bytedance/seedance-[a-z0-9][a-z0-9.\-]{0,80}\Z')
REMOTE_PATTERN = re.compile(r'[A-Za-z0-9_-]{1,160}\Z')
LOCAL_PATTERN = re.compile(r'video-[a-f0-9]{32}\Z')
TERMINAL = {'completed', 'failed', 'cancelled', 'expired', 'submission_uncertain'}
PUBLIC_FIELDS = {'id', 'idempotency_key', 'status', 'created_at', 'updated_at',
                 'model', 'prompt', 'mode', 'duration', 'resolution', 'aspect_ratio',
                 'generate_audio', 'media_count', 'result_url', 'cost', 'error',
                 'last_checked_at'}


class VideoError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _transport(request, *, timeout, max_bytes):
    """Credential-bearing requests are restricted again at the I/O boundary."""
    _validate_endpoint(request.full_url, request.get_method())
    try:
        response = build_opener(_NoRedirect).open(request, timeout=timeout)
    except HTTPError as exc:
        # Do not read, interpolate, or expose an upstream error body.
        return exc.code, dict(exc.headers or {}), b''
    with response:
        if response.geturl() != request.full_url:
            raise VideoError('视频服务返回了不受支持的跳转。', 502)
        raw = response.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise VideoError('视频服务返回的文件过大。', 502)
        return response.status, dict(response.headers), raw


def _validate_endpoint(url, method):
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == 'https' and parsed.hostname == 'openrouter.ai'
                 and parsed.port in (None, 443) and not parsed.username
                 and not parsed.password and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise VideoError('不允许的视频服务地址。', 400)
    path = parsed.path
    allowed = ((method == 'GET' and path in ('/api/v1/key', '/api/v1/videos/models'))
               or (method == 'POST' and path == '/api/v1/videos')
               or (method == 'GET' and re.fullmatch(
                   r'/api/v1/videos/[A-Za-z0-9_-]{1,160}(?:/content)?', path)))
    if not allowed:
        raise VideoError('不允许的视频服务操作。', 400)


def _text(value, label, maximum, minimum=1):
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        raise VideoError(label + '格式无效。')
    return value.strip()


def _safe_prompt(prompt, key=''):
    # Keep readable job metadata, never inline media or download capabilities.
    value = re.sub(r'data:[^\s]+', '[图片数据已省略]', prompt, flags=re.I)
    value = re.sub(r'https?://[^\s]+', '[链接已省略]', value, flags=re.I)
    if key:
        value = value.replace(key, '[密钥已省略]')
    value = re.sub(r'\bsk-(?:or-)?[A-Za-z0-9_-]{12,}', '[密钥已省略]', value)
    return value


def _media_url(value, kind):
    value = _text(value, '素材地址', 8 * 1024 * 1024)
    if value.startswith('data:'):
        if kind != 'image_url':
            raise VideoError('参考视频需要 HTTPS 文件直链，暂不接收本地视频。')
        match = re.fullmatch(r'data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=]+)', value)
        if not match or len(match[2]) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
            raise VideoError('图片须为 PNG、JPEG 或 WebP，且每张不超过 5MB。')
        try:
            raw = base64.b64decode(match[2], validate=True)
        except (binascii.Error, ValueError):
            raise VideoError('图片编码无效。') from None
        valid = ((match[1] == 'png' and raw.startswith(b'\x89PNG\r\n\x1a\n'))
                 or (match[1] == 'jpeg' and raw.startswith(b'\xff\xd8\xff'))
                 or (match[1] == 'webp' and raw.startswith(b'RIFF') and raw[8:12] == b'WEBP'))
        if not raw or len(raw) > MAX_IMAGE_BYTES or not valid:
            raise VideoError('图片内容与格式不符，或超过 5MB。')
        return value
    if len(value) > 4096 or any(ord(c) < 33 for c in value):
        raise VideoError('请使用有效的 HTTPS 素材直链。')
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or '').lower().rstrip('.')
        # Official player CDN links can be MP4 without a filename extension.
        # Keep this exception narrower than TikTok share pages.
        tiktok_cdn_video = (kind == 'video_url'
            and re.fullmatch(r'v[0-9]+(?:-[a-z0-9]+)*\.(?:[a-z]{2}\.)?tiktok\.com', host) is not None
            and re.fullmatch(r'/video/tos/[A-Za-z0-9/_-]+/?', unquote(parsed.path)) is not None
            and parse_qs(parsed.query).get('mime_type') == ['video_mp4'])
        # TikWM's verified public MP4 response uses this exact CDN family and
        # hashed path prefix. Do not extend this to arbitrary TikTok subdomains.
        tiktok_cdn_video = tiktok_cdn_video or (kind == 'video_url'
            and re.fullmatch(r'v[0-9]+[a-z]?\.tiktokcdn-us\.com', host) is not None
            and re.fullmatch(r'/[a-fA-F0-9]{32}/[a-fA-F0-9]{8}/video/tos/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)+/?',
                             unquote(parsed.path)) is not None
            and parse_qs(parsed.query).get('mime_type') == ['video_mp4'])
        if (parsed.scheme != 'https' or not host or parsed.username or parsed.password
                or parsed.port not in (None, 443) or parsed.fragment):
            raise ValueError()
        if ('.' not in host or host == 'localhost' or host.endswith(('.localhost', '.local', '.internal'))
                or (not tiktok_cdn_video and any(host == domain or host.endswith('.' + domain)
                       for domain in ('tiktok.com', 'douyin.com', 'iesdouyin.com')))):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is None and re.fullmatch(r'[0-9.]+', host):
            raise ValueError()
        if address and not address.is_global:
            raise ValueError()
        if kind == 'video_url' and not tiktok_cdn_video and not unquote(parsed.path).lower().endswith(('.mp4', '.webm', '.mov')):
            raise VideoError('参考视频须使用 .mp4、.webm 或 .mov 的 HTTPS 文件直链，不能使用分享页。')
    except ValueError:
        raise VideoError('请使用公开 HTTPS 素材直链；不支持分享页、本机或私有网络地址。') from None
    return value


def _mp4(raw):
    """Validate bounded ISO BMFF structure, not complete codec decodability."""
    if len(raw) < 28 or raw[4:8] != b'ftyp':
        return False
    found = set()
    offset = 0
    while offset + 8 <= len(raw):
        size = int.from_bytes(raw[offset:offset + 4], 'big')
        kind = raw[offset + 4:offset + 8]
        header = 8
        if size == 1:
            if offset + 16 > len(raw):
                return False
            size = int.from_bytes(raw[offset + 8:offset + 16], 'big')
            header = 16
        elif size == 0:
            size = len(raw) - offset
        if size < header or offset + size > len(raw):
            return False
        if kind == b'ftyp' and size < 16:
            return False
        found.add(kind)
        offset += size
    return offset == len(raw) and {b'ftyp', b'moov', b'mdat'} <= found


class VideoService:
    def __init__(self, directory, request_fn=None, clock=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.generated = self.directory / 'generated'
        self.generated.mkdir(mode=0o700, exist_ok=True)
        self.path = self.directory / 'video-jobs.json'
        self.lock = threading.RLock()
        self._request_fn = request_fn or _transport
        self._clock = clock or time.time
        self._key = os.environ.get('OPENROUTER_API_KEY', '').strip()
        if not self._valid_key(self._key):
            self._key = ''
        self._models_cache = None
        self._models_at = 0
        self._rows = []
        if self.path.exists():
            try:
                document = json.loads(self.path.read_text(encoding='utf-8'))
                if not isinstance(document, dict) or document.get('schema_version') != 1 or not isinstance(document.get('jobs'), list):
                    raise ValueError()
                for row in document['jobs']:
                    if (not isinstance(row, dict) or not LOCAL_PATTERN.fullmatch(str(row.get('id', '')))
                            or not isinstance(row.get('status'), str)):
                        raise ValueError()
                    self._rows.append(row)
            except (OSError, ValueError, TypeError):
                raise VideoError('本机视频任务记录无法读取，请保留文件并修复后再提交。', 500) from None
            changed = False
            for row in self._rows:
                if row['status'] == 'submitting':
                    row.update(status='submission_uncertain', error='上次提交被中断，可能已计费。请先到 OpenRouter 核对，勿直接重复生成。', updated_at=self._now())
                    changed = True
            if changed:
                self._save()

    @staticmethod
    def _valid_key(value):
        return isinstance(value, str) and 8 <= len(value) <= 512 and all(33 <= ord(c) <= 126 for c in value)

    def _now(self):
        return datetime.fromtimestamp(self._clock(), timezone.utc).isoformat()

    def _save(self):
        tmp = self.path.with_name('.video-jobs-' + uuid.uuid4().hex + '.tmp')
        try:
            descriptor = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as target:
                json.dump({'schema_version': 1, 'jobs': self._rows}, target, ensure_ascii=False, indent=2)
                target.flush()
                os.fsync(target.fileno())
            tmp.replace(self.path)
        except OSError:
            raise VideoError('无法保存本机视频任务，请检查存储空间。', 500) from None
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _public(self, row):
        public = {key: value for key, value in row.items() if key in PUBLIC_FIELDS}
        if row.get('_remote_id'):
            public['remote_id'] = row['_remote_id']
        return copy.deepcopy(public)

    def _find(self, ident):
        if not isinstance(ident, str) or not LOCAL_PATTERN.fullmatch(ident):
            raise VideoError('视频任务不存在。', 404)
        row = next((item for item in self._rows if item['id'] == ident), None)
        if row is None:
            raise VideoError('视频任务不存在。', 404)
        return row

    def _network(self, path, method='GET', payload=None, *, key=None, binary=False):
        url = BASE + path
        _validate_endpoint(url, method)
        token = self._key if key is None else key
        if path != '/videos/models' and not token:
            raise VideoError('请先连接 OpenRouter API Key。', 401)
        headers = {'Accept': 'video/mp4' if binary else 'application/json', 'User-Agent': 'idore-video/1.0'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        data = None
        if payload is not None:
            data = json.dumps(payload, separators=(',', ':')).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        request = Request(url, data=data, headers=headers, method=method)
        maximum = MAX_VIDEO_BYTES if binary else MAX_JSON_BYTES
        try:
            status, response_headers, raw = self._request_fn(request, timeout=60 if binary else 40, max_bytes=maximum)
        except VideoError:
            raise
        except (URLError, TimeoutError, OSError, socket.timeout):
            raise VideoError('无法确认视频服务响应，请稍后查询任务，避免重复提交。', 502) from None
        except Exception:
            raise VideoError('视频服务响应异常，请稍后查询任务。', 502) from None
        if (not isinstance(status, int) or not isinstance(response_headers, dict)
                or not isinstance(raw, bytes) or len(raw) > maximum):
            raise VideoError('视频服务响应无效或过大。', 502)
        if status not in (200, 201, 202):
            message = {401: 'OpenRouter API Key 无效或已过期。', 402: 'OpenRouter 余额不足，请充值后重新操作。',
                       403: 'OpenRouter 未授权这项操作。', 404: 'OpenRouter 未找到该模型或任务。',
                       429: 'OpenRouter 请求过多，请稍后重试。',
                       400: 'OpenRouter 拒绝了当前模型参数或素材，请核对后创建新请求。',
                       422: 'OpenRouter 无法处理当前参数或素材。'}.get(status, 'OpenRouter 暂未返回有效结果，请稍后查询任务。')
            raise VideoError(message, status if status in (400, 401, 402, 403, 404, 422, 429) else 502)
        if binary:
            content_type = next((str(value).split(';', 1)[0].strip().lower() for name, value in response_headers.items()
                                 if name.lower() == 'content-type'), '')
            if content_type not in ('video/mp4', 'application/octet-stream') or not _mp4(raw):
                raise VideoError('下载结果不是有效的 MP4 文件，未保存。', 502)
            return raw
        try:
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError()
        except (UnicodeError, ValueError, TypeError):
            raise VideoError('视频服务没有返回有效 JSON，请查询任务后再操作。', 502) from None
        return result

    def connection(self):
        with self.lock:
            return {'connected': bool(self._key), 'provider': 'OpenRouter', 'key_storage': 'memory'}

    def connect(self, body):
        if not isinstance(body, dict) or set(body) != {'api_key'}:
            raise VideoError('请只提交 OpenRouter API Key。')
        key = body['api_key']
        if not self._valid_key(key):
            raise VideoError('API Key 格式无效。')
        result = self._network('/key', key=key)
        if not isinstance(result.get('data'), dict):
            raise VideoError('无法验证 OpenRouter API Key。', 502)
        with self.lock:
            self._key = key
            return self.connection()

    def disconnect(self):
        with self.lock:
            self._key = ''
            return self.connection()

    def _models(self, fresh=False):
        if not fresh and self._models_cache is not None and self._clock() - self._models_at < 300:
            return copy.deepcopy(self._models_cache)
        # Public capability lookup does not need to send credentials.
        response = self._network('/videos/models', key='')
        rows = response.get('data')
        if not isinstance(rows, list):
            raise VideoError('无法读取视频模型能力，请稍后重试。', 502)
        models = [copy.deepcopy(row) for row in rows if isinstance(row, dict)
                  and isinstance(row.get('id'), str) and MODEL_PATTERN.fullmatch(row['id'])]
        self._models_cache = models
        self._models_at = self._clock()
        return copy.deepcopy(models)

    def models(self):
        with self.lock:
            return {'models': self._models()}

    def jobs(self):
        with self.lock:
            return {'jobs': [self._public(row) for row in self._rows]}

    def _request(self, body):
        allowed = {'idempotency_key', 'model', 'prompt', 'mode', 'duration', 'resolution',
                   'aspect_ratio', 'generate_audio', 'first_frame', 'last_frame', 'references', 'consent'}
        if not isinstance(body, dict) or set(body) - allowed:
            raise VideoError('视频生成请求包含不支持的字段。')
        if body.get('consent') is not True:
            raise VideoError('请确认允许传输所选素材并产生视频生成费用。')
        idempotency = _text(body.get('idempotency_key'), '请求标识', 128)
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', idempotency):
            raise VideoError('请求标识格式无效。')
        if self._key and self._key in idempotency:
            raise VideoError('请求标识不能包含 API Key。')
        model = _text(body.get('model'), '模型', 120)
        if not MODEL_PATTERN.fullmatch(model):
            raise VideoError('请选择明确的 bytedance/seedance 模型，不会自动替换模型。')
        prompt = _text(body.get('prompt'), '生成描述', 12000)
        if self._key and self._key in prompt:
            raise VideoError('生成描述不能包含 API Key。')
        mode = body.get('mode')
        if mode not in ('text', 'frames', 'references'):
            raise VideoError('请选择文字、首尾帧或参考素材生成方式。')
        duration = body.get('duration')
        if type(duration) is not int or not 1 <= duration <= 120:
            raise VideoError('视频时长必须为有效整数秒。')
        resolution = _text(body.get('resolution'), '分辨率', 20)
        aspect = _text(body.get('aspect_ratio'), '画幅', 20)
        if type(body.get('generate_audio')) is not bool:
            raise VideoError('请明确选择是否生成音频。')
        payload = dict(model=model, prompt=prompt, duration=duration, resolution=resolution,
                       aspect_ratio=aspect, generate_audio=body['generate_audio'])
        first, last, references = body.get('first_frame'), body.get('last_frame'), body.get('references')
        if mode == 'text':
            if first or last or references:
                raise VideoError('文字模式不能同时提交图片或视频素材。')
        elif mode == 'frames':
            if references:
                raise VideoError('首尾帧和参考素材不能同时提交。')
            if not first:
                raise VideoError('首尾帧模式至少需要首帧图片。')
            if aspect == 'adaptive' and re.fullmatch(r'bytedance/seedance-2\.5(?:-[a-z0-9-]+)?', model):
                # Seedance 2.5 derives its first-frame ratio. OpenRouter accepts omission here.
                del payload['aspect_ratio']
            payload['frame_images'] = [dict(type='image_url', image_url={'url': _media_url(first, 'image_url')}, frame_type='first_frame')]
            if last:
                payload['frame_images'].append(dict(type='image_url', image_url={'url': _media_url(last, 'image_url')}, frame_type='last_frame'))
        else:
            if first or last:
                raise VideoError('参考素材模式不能同时提交首尾帧。')
            if not isinstance(references, list) or not 1 <= len(references) <= 4:
                raise VideoError('请提供 1–4 个参考素材。')
            payload['input_references'] = []
            for item in references:
                if not isinstance(item, dict) or set(item) != {'type', 'url'} or item['type'] not in ('image_url', 'video_url'):
                    raise VideoError('参考素材仅支持明确的图片或视频类型。')
                kind = item['type']
                payload['input_references'].append({'type': kind, kind: {'url': _media_url(item['url'], kind)}})
            # Official Seedance generation 2+ contract supports typed video/image refs.
            if not re.fullmatch(r'bytedance/seedance-[2-9][0-9]*(?:\.[0-9]+)*(?:-[a-z0-9-]+)?', model):
                raise VideoError('参考素材模式需要支持参考输入的 Seedance 2 或更新模型。')
            payload['provider'] = {'only': ['seed', 'byteplus']}
        fingerprint = hashlib.sha256(json.dumps({'mode': mode, 'payload': payload}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return idempotency, mode, payload, fingerprint

    def _capabilities(self, payload):
        models = self._models(fresh=True)
        model = next((row for row in models if row['id'] == payload['model']), None)
        if model is None:
            raise VideoError('当前 OpenRouter 模型列表中没有这个 Seedance 视频模型。')
        for key, name in (('duration', 'supported_durations'), ('resolution', 'supported_resolutions'),
                          ('aspect_ratio', 'supported_aspect_ratios')):
            supported = model.get(name)
            if key in payload and (not isinstance(supported, list) or payload[key] not in supported):
                raise VideoError('所选时长、分辨率或画幅未经模型当前能力支持，请刷新模型后重选。')
        if 'frame_images' in payload:
            supported = model.get('supported_frame_images')
            if not isinstance(supported, list) or any(frame['frame_type'] not in supported for frame in payload['frame_images']):
                raise VideoError('该模型未声明支持所选首尾帧，请更换生成方式。')
        if payload['generate_audio'] and model.get('generate_audio') is False:
            raise VideoError('该模型未声明支持生成音频。')

    def _apply(self, row, result):
        remote = result.get('id')
        if (not isinstance(remote, str) or not REMOTE_PATTERN.fullmatch(remote)
                or (self._key and self._key in remote)):
            raise VideoError('视频服务没有返回可安全查询的任务编号。', 502)
        if row.get('_remote_id') and row['_remote_id'] != remote:
            raise VideoError('视频服务返回的任务编号不匹配。', 502)
        status = result.get('status')
        if status not in ('pending', 'in_progress', 'completed', 'failed', 'cancelled', 'expired'):
            raise VideoError('视频服务返回的任务状态未知，请到 OpenRouter 核对。', 502)
        row.update(_remote_id=remote, status=status, updated_at=self._now())
        row.pop('error', None)
        usage = result.get('usage')
        cost = usage.get('cost') if isinstance(usage, dict) else None
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost) and 0 <= cost <= 1e6:
            row['cost'] = cost
        if status == 'failed':
            row['error'] = 'OpenRouter 视频生成失败。请在账户中核对原因与计费后，再创建新的生成请求。'
        elif status == 'cancelled':
            row['error'] = 'OpenRouter 视频任务已取消。请先在账户中核对计费；不会自动重新提交。'
        elif status == 'expired':
            row['error'] = 'OpenRouter 视频任务已过期，无法下载。请先在账户中核对结果与计费；不会自动重新提交。'

    def submit(self, body):
        with self.lock:
            idempotency, mode, payload, fingerprint = self._request(body)
            prior = next((row for row in self._rows if row.get('idempotency_key') == idempotency), None)
            if prior:
                if prior.get('_request_hash') != fingerprint:
                    raise VideoError('同一请求标识不能提交不同内容，请为新的生成操作使用新标识。', 409)
                return self._public(prior)
            if not self._key:
                raise VideoError('请先连接 OpenRouter API Key。', 401)
            self._capabilities(payload)
            row = dict(id='video-' + uuid.uuid4().hex, idempotency_key=idempotency,
                       status='submitting', created_at=self._now(), updated_at=self._now(),
                       model=payload['model'], prompt=_safe_prompt(payload['prompt'], self._key), mode=mode,
                       duration=payload['duration'], resolution=payload['resolution'], aspect_ratio=payload.get('aspect_ratio', 'adaptive'),
                       generate_audio=payload['generate_audio'], media_count=len(payload.get('frame_images', payload.get('input_references', []))),
                       result_url=None, _request_hash=fingerprint, _last_poll=self._clock())
            self._rows.insert(0, row)
            self._save()  # Must complete before any potentially billable request.
            try:
                result = self._network('/videos', 'POST', payload)
                self._apply(row, result)
            except VideoError as exc:
                if exc.status in (400, 401, 402, 403, 404, 422, 429):
                    row.update(status='failed', error=str(exc), updated_at=self._now())
                else:
                    row.update(status='submission_uncertain', error='提交结果不确定，可能已计费。请先到 OpenRouter 核对，勿直接重复生成。', updated_at=self._now())
                self._save()
                raise
            self._save()
            return self._public(row)

    def refresh(self, ident):
        with self.lock:
            row = self._find(ident)
            if row['status'] in TERMINAL:
                return self._public(row)
            remote = row.get('_remote_id')
            if not isinstance(remote, str) or not REMOTE_PATTERN.fullmatch(remote):
                raise VideoError('任务编号待确认，请到 OpenRouter 核对后恢复已有任务。', 409)
            checked = row.get('_last_poll', 0)
            if self._clock() - checked < 30:
                return self._public(row)
            row.update(_last_poll=self._clock(), last_checked_at=self._now())
            self._save()
            try:
                self._apply(row, self._network('/videos/' + remote))
            except VideoError as exc:
                row['error'] = str(exc)
                row['updated_at'] = self._now()
                self._save()
                raise
            self._save()
            return self._public(row)

    def download(self, ident):
        with self.lock:
            row = self._find(ident)
            filename = row['id'] + '.mp4'
            target = self.generated / filename
            if row.get('result_url') == '/generated/' + filename and target.is_file():
                return self._public(row)
            if row['status'] != 'completed':
                raise VideoError('视频还未完成，请先查询生成状态。', 409)
            remote = row.get('_remote_id')
            if not isinstance(remote, str) or not REMOTE_PATTERN.fullmatch(remote):
                raise VideoError('任务编号无效，无法下载。', 409)
            raw = self._network('/videos/' + remote + '/content', binary=True)
            tmp = self.generated / ('.' + uuid.uuid4().hex + '.tmp')
            try:
                descriptor = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, 'wb') as output:
                    output.write(raw)
                    output.flush()
                    os.fsync(output.fileno())
                tmp.replace(target)
            except OSError:
                raise VideoError('视频已生成，但无法保存本机文件，请检查存储空间后重新下载。', 500) from None
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            row.update(result_url='/generated/' + filename, updated_at=self._now())
            row.pop('error', None)
            self._save()
            return self._public(row)

    def recover(self, remote_id, model=''):
        """Attach an existing provider task. This never submits a generation."""
        if (not isinstance(remote_id, str) or not REMOTE_PATTERN.fullmatch(remote_id)
                or remote_id.startswith('sk-') or (self._key and self._key in remote_id)):
            raise VideoError('请填写 OpenRouter 的有效任务编号，不能填写网址。')
        if model and (not isinstance(model, str) or not MODEL_PATTERN.fullmatch(model)):
            raise VideoError('恢复任务的模型编号无效。')
        with self.lock:
            prior = next((row for row in self._rows if row.get('_remote_id') == remote_id), None)
            if prior:
                return self._public(prior)
            row = dict(id='video-' + uuid.uuid4().hex, status='pending', created_at=self._now(),
                       updated_at=self._now(), model=model, prompt='恢复已有 OpenRouter 视频任务',
                       mode='recovered', result_url=None, _remote_id=remote_id)
            self._rows.insert(0, row)
            self._save()
            return self._public(row)
