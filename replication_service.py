"""Select an owned character and an observed TikTok video before paid replication.

Plans, reference media URLs, and upload bytes stay in memory. Only explicit manual
asset classifications are persisted. VideoService owns paid-job idempotency.
"""
from __future__ import annotations
import base64
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid
from zoneinfo import ZoneInfo
from discovery import canonical_video, DiscoveryError
from video_service import MAX_IMAGE_BYTES, VideoError, _media_url

ROOT = Path(__file__).resolve().parent
CANDIDATE_URL = 'https://raw.githubusercontent.com/yjr927/idore_tools/main/site/data/discovery.json'
MAX_CANDIDATES_BYTES = 8 * 1024 * 1024
PLAN_TTL = 15 * 60
MATERIALS = {'male': '男生素材', 'female': '女生素材', 'mixed': '男女混合素材', 'unknown': '素材分组待确认'}
SUBJECTS = {'solo': '单人', 'group': '多人', 'unknown': '人数待确认'}
ASSET_ID = re.compile(r'asset-[a-f0-9]{16}\Z')
PLAN_ID = re.compile(r'replication-[a-f0-9]{32}\Z')


class ReplicationError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _transport(request, *, timeout, max_bytes):
    if request.full_url != CANDIDATE_URL or request.get_method() != 'GET' or request.has_header('Authorization'):
        raise ReplicationError('不允许的候选同步地址。')
    try:
        response = build_opener(_NoRedirect).open(request, timeout=timeout)
    except HTTPError as exc:
        return exc.code, {}, b''
    with response:
        if response.geturl() != CANDIDATE_URL:
            raise ReplicationError('候选来源返回了跳转，本机数据保留。', 502)
        raw = response.read(max_bytes + 1)
        return response.status, dict(response.headers), raw


def _date(value):
    if not isinstance(value, str) or len(value) > 60:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _text(value, maximum=6000, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ReplicationError('输入格式无效。')
    return value.strip()


def _eligible(asset):
    return (isinstance(asset, dict) and asset.get('kind') == 'image' and asset.get('rights') == 'owned'
            and asset.get('approved') is True and asset.get('is_demo') is False
            and isinstance(asset.get('id'), str) and ASSET_ID.fullmatch(asset['id']))


def _effective_reference(row):
    """Use watched review overlays unless a newer watched capture knows that axis."""
    watched = row.get('source_kind') == 'chatgpt_work' and row.get('original_watched') is True
    overlay = row.get('material_classification')
    if not isinstance(overlay, dict) or not _date(overlay.get('observed_at')):
        overlay = {}
    review_at = _date(overlay.get('observed_at'))
    observations = row.get('observations')
    observations = observations if isinstance(observations, list) else []

    def evidence(axis):
        found = []
        for entry in observations:
            if not isinstance(entry, dict) or not _date(entry.get('observed_at')):
                continue
            if axis == 'material_group':
                value = entry.get('material_group')
                if not isinstance(value, str) or value not in MATERIALS:
                    continue
            else:
                checks = entry.get('checks')
                if not isinstance(checks, dict) or 'solo' not in checks or (checks['solo'] is not None and type(checks['solo']) is not bool):
                    continue
                value = 'solo' if checks['solo'] is True else 'group' if checks['solo'] is False else 'unknown'
            known = entry.get('original_watched') is True
            found.append((value if known else 'unknown', _date(entry['observed_at']), known))
        if found:
            return max(found, key=lambda item: item[1])
        if axis == 'material_group':
            value = row.get(axis)
            value = value if isinstance(value, str) and value in MATERIALS else 'unknown'
        else:
            checks = row.get('checks') if isinstance(row.get('checks'), dict) else {}
            value = 'solo' if checks.get('solo') is True else 'group' if checks.get('solo') is False else 'unknown'
        # Retained top-level fields have no reliable observation time when a
        # history exists but never carries that field (notably v1 rechecks).
        stamp = None if observations else _date(row.get('last_seen_at'))
        return (value if watched else 'unknown', stamp, watched)

    def effective(axis, choices):
        value, observed_at, seen = evidence(axis)
        reviewed = overlay.get(axis)
        if isinstance(reviewed, str) and reviewed in choices:
            if not (seen and value != 'unknown' and observed_at and review_at and observed_at > review_at):
                reviewed_watched = overlay.get('original_watched') is True
                return (reviewed if reviewed_watched else 'unknown', reviewed_watched)
        return value, seen

    material, material_watched = effective('material_group', MATERIALS)
    subject, subject_watched = effective('subject_group', SUBJECTS)
    return material, subject, material_watched or subject_watched, overlay.get('observed_at')


class ReplicationService:
    def __init__(self, store, video_service, candidate_path=None, request_fn=None):
        self.store = store
        self.video_service = video_service
        self.candidate_path = Path(candidate_path) if candidate_path is not None else ROOT / 'site/data/discovery.json'
        self._request_fn = request_fn or _transport
        self._plans = {}
        self._lock = threading.RLock()
        self.links_path = Path(store.directory) / 'replication-links.json'
        self._links = []
        self.reference_classifications_path = Path(store.directory) / 'replication-reference-classifications.json'
        self._reference_classifications = {}
        if self.reference_classifications_path.exists():
            try:
                path = self.reference_classifications_path
                if path.is_symlink() or path.stat().st_size > 1024 * 1024:
                    raise ValueError()
                document = json.loads(path.read_text(encoding='utf-8'))
                if (set(document) != {'schema_version', 'classifications'} or document['schema_version'] != 1
                        or not isinstance(document['classifications'], list) or len(document['classifications']) > 5000):
                    raise ValueError()
                for item in document['classifications']:
                    if (not isinstance(item, dict) or set(item) != {'reference_id', 'material_group', 'subject_group', 'classified_at'}
                            or not re.fullmatch(r'tiktok-\d{10,25}', str(item.get('reference_id', '')))
                            or not isinstance(item['material_group'], str) or item['material_group'] not in MATERIALS
                            or not isinstance(item['subject_group'], str) or item['subject_group'] not in SUBJECTS
                            or not _date(item['classified_at']) or item['reference_id'] in self._reference_classifications):
                        raise ValueError()
                    self._reference_classifications[item['reference_id']] = item
            except (OSError, ValueError, AttributeError, TypeError, KeyError):
                raise ReplicationError('本机参考视频分类无法读取，请保留文件并修复后再准备复刻。', 500) from None
        if self.links_path.exists():
            try:
                if self.links_path.is_symlink() or self.links_path.stat().st_size > 2 * 1024 * 1024:
                    raise ValueError()
                document = json.loads(self.links_path.read_text(encoding='utf-8'))
                if document.get('schema_version') != 1 or not isinstance(document.get('links'), list):
                    raise ValueError()
                for link in document['links']:
                    if (not isinstance(link, dict) or set(link) - {'request_key_hash', 'asset_id', 'reference_id', 'job_id'}
                            or not re.fullmatch(r'[a-f0-9]{64}', str(link.get('request_key_hash', '')))
                            or not ASSET_ID.fullmatch(str(link.get('asset_id', '')))
                            or not re.fullmatch(r'tiktok-\d{10,25}', str(link.get('reference_id', '')))
                            or ('job_id' in link and not re.fullmatch(r'video-[a-f0-9]{32}', str(link['job_id'])))):
                        raise ValueError()
                self._links = document['links']
            except (OSError, ValueError, AttributeError, TypeError):
                raise ReplicationError('复刻来源记录无法读取，请修复后再提交，避免丢失原任务。', 500) from None

    def _save_links(self):
        temporary = self.links_path.with_name('.replication-links-' + uuid.uuid4().hex + '.tmp')
        try:
            if self.links_path.is_symlink():
                raise OSError()
            descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as target:
                json.dump({'schema_version': 1, 'links': self._links}, target, separators=(',', ':'))
                target.flush()
                os.fsync(target.fileno())
            temporary.replace(self.links_path)
        except OSError:
            raise ReplicationError('无法保存复刻来源记录，请检查本机存储后再操作。', 500) from None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _save_reference_classifications(self):
        path = self.reference_classifications_path
        temporary = path.with_name('.replication-reference-classifications-' + uuid.uuid4().hex + '.tmp')
        try:
            if path.is_symlink():
                raise OSError()
            descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as target:
                json.dump({'schema_version': 1, 'classifications': list(self._reference_classifications.values())}, target,
                          separators=(',', ':'))
                target.flush()
                os.fsync(target.fileno())
            temporary.replace(path)
        except OSError:
            raise ReplicationError('本机参考视频分类未能保存，请检查存储空间。', 500) from None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _assets(self):
        return [asset for asset in self.store.data.get('assets', []) if _eligible(asset)]

    def _asset(self, ident):
        return next((asset for asset in self._assets() if asset['id'] == ident), None)

    def _file(self, asset, read=False):
        value = asset.get('url')
        match = re.fullmatch(r'/media/(asset-[a-f0-9]{16})\.(png|jpg|jpeg|webp)', value) if isinstance(value, str) else None
        if not match or match[1] != asset['id']:
            return None, None, '角色图片文件地址无效。'
        media = Path(self.store.media)
        path = media / (match[1] + '.' + match[2])
        try:
            if media.is_symlink() or path.is_symlink() or path.resolve().parent != media.resolve():
                return None, None, '角色图片文件不能是链接或来自其他目录。'
            descriptor = os.open(str(path), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(descriptor, 'rb') as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode):
                    return None, None, '角色图片文件不可读取。'
                if not 0 < info.st_size <= MAX_IMAGE_BYTES:
                    return None, None, '角色图片须不超过 5MB；请先在素材库准备较小版本。'
                raw = source.read(MAX_IMAGE_BYTES + 1 if read else 32)
            extension = match[2]
            mime = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'webp': 'image/webp'}[extension]
            valid = ((extension == 'png' and raw.startswith(b'\x89PNG\r\n\x1a\n'))
                     or (extension in ('jpg', 'jpeg') and raw.startswith(b'\xff\xd8\xff'))
                     or (extension == 'webp' and raw.startswith(b'RIFF') and raw[8:12] == b'WEBP'))
            if not valid or len(raw) > MAX_IMAGE_BYTES or (read and len(raw) != info.st_size):
                return None, None, '角色图片内容与文件格式不符。'
            return raw, mime, None
        except (OSError, ValueError):
            return None, None, '角色图片文件缺失或不可读取。'

    def _public_asset(self, asset):
        material = asset.get('material_group') if isinstance(asset.get('material_group'), str) and asset.get('material_group') in MATERIALS else 'unknown'
        subject = asset.get('subject_group') if isinstance(asset.get('subject_group'), str) and asset.get('subject_group') in SUBJECTS else 'unknown'
        # No name/appearance-based gender guesses; only explicit stored labels.
        blockers = []
        if material not in ('male', 'female'):
            blockers.append('请先确认角色素材属于男生或女生分组。')
        if subject != 'solo':
            blockers.append('请先确认角色图片只有一个人物／角色。')
        _, _, error = self._file(asset)
        if error:
            blockers.append(error)
        return dict(id=asset['id'], name=str(asset.get('name') or '')[:300],
                    character=str(asset.get('character') or '')[:200], stage=str(asset.get('stage') or '')[:100],
                    url=asset.get('url'), material_group=material, subject_group=subject,
                    material_label=MATERIALS[material], subject_label=SUBJECTS[subject],
                    classification_method=asset.get('classification_method', 'unknown'),
                    ready=not blockers, blockers=blockers)

    def _validate_document(self, document):
        if not isinstance(document, dict) or not isinstance(document.get('videos'), list) or len(document['videos']) > 5000:
            raise ReplicationError('候选数据格式无效，本机数据保留。', 502)
        seen = set()
        for row in document['videos']:
            if not isinstance(row, dict):
                raise ReplicationError('候选数据格式无效，本机数据保留。', 502)
            try:
                _, _, ident = canonical_video(row.get('url'))
            except DiscoveryError:
                raise ReplicationError('候选数据包含无效视频地址，本机数据保留。', 502) from None
            if ident in seen or row.get('id', 'tiktok-' + ident) != 'tiktok-' + ident:
                raise ReplicationError('候选数据包含重复或无效编号，本机数据保留。', 502)
            seen.add(ident)
        return document

    def _document(self):
        try:
            if self.candidate_path.is_symlink() or self.candidate_path.stat().st_size > MAX_CANDIDATES_BYTES:
                raise OSError()
            document = json.loads(self.candidate_path.read_bytes())
            return self._validate_document(document)
        except FileNotFoundError:
            return {'videos': []}
        except (OSError, ValueError, TypeError):
            raise ReplicationError('无法读取本机候选库，请先同步公开采集结果。', 502) from None

    def _references(self):
        result = []
        now = datetime.now(timezone.utc)
        for row in self._document()['videos']:
            url, _, ident = canonical_video(row['url'])
            material, subject, watched, reviewed_at = _effective_reference(row)
            local = self._reference_classifications.get('tiktok-' + ident)
            method = 'public_review' if reviewed_at else 'work_observation' if watched else 'unknown'
            if local:
                # A manual label routes already-watched media; it never invents
                # original-video playback evidence or changes the public row.
                material = local['material_group'] if watched else 'unknown'
                subject = local['subject_group'] if watched else 'unknown'
                reviewed_at = local['classified_at']
                method = 'manual_user' if watched else 'manual_pending_watch'
            first = _date(row.get('first_seen_at')) or _date(row.get('last_seen_at'))
            blockers = []
            if not watched:
                blockers.append('参考视频尚无观看原片的观察记录。')
            if material not in ('male', 'female'):
                blockers.append('参考视频的男生／女生素材分类待确认。')
            if subject != 'solo':
                blockers.append('当前流程只接受已确认单人的参考视频。')
            result.append(dict(id='tiktok-' + ident, url=url, title=str(row.get('title') or '')[:2000],
                               kind=row.get('kind', 'unknown'), trend_category=row.get('trend_category', 'unknown'),
                               transformation=row.get('transformation', 'unknown'),
                               material_group=material, subject_group=subject,
                               material_label=MATERIALS[material], subject_label=SUBJECTS[subject],
                               original_watched=watched, classification_at=reviewed_at,
                               classification_method=method, classification_local=bool(local),
                               first_seen_at=row.get('first_seen_at'), last_seen_at=row.get('last_seen_at'),
                               capture_date=first.astimezone(ZoneInfo('America/New_York')).date().isoformat() if first else None,
                               capture_age_days=max(0, (now - first).days) if first else None,
                               motion_summary=str(row.get('motion_summary') or '')[:2000],
                               ready=not blockers, blockers=blockers))
        result.sort(key=lambda row: (_date(row.get('first_seen_at')) or _date(row.get('last_seen_at'))
                                    or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
        return result

    def catalog(self):
        with self._lock, self.store.lock:
            assets = [self._public_asset(asset) for asset in self._assets()]
            references = self._references()
            blockers = []
            if not assets:
                blockers.append('素材库中还没有已批准的自有角色图片。')
            if not references:
                blockers.append('还没有公开采集的 TikTok 原视频，请先同步结果。')
            return dict(assets=assets, references=references, blockers=blockers)

    def classify_asset(self, asset_id, body):
        if (not isinstance(body, dict) or set(body) - {'material_group', 'subject_group', 'apply_character'}
                or not isinstance(body.get('material_group'), str) or body.get('material_group') not in MATERIALS
                or not isinstance(body.get('subject_group'), str) or body.get('subject_group') not in SUBJECTS
                or type(body.get('apply_character', False)) is not bool):
            raise ReplicationError('请明确选择素材分组、人数和是否应用到同一角色。')
        with self._lock, self.store.lock:
            asset = self._asset(asset_id)
            if asset is None:
                raise ReplicationError('找不到可分类的真实角色图片。', 404)
            character = asset.get('character')
            targets = [asset]
            if body.get('apply_character'):
                if not isinstance(character, str) or not character.strip():
                    raise ReplicationError('这张图片没有角色标识，请只分类当前图片。')
                targets = [item for item in self._assets() if item.get('character') == character]
            originals = [copy.deepcopy(item) for item in targets]
            for item in targets:
                item.update(material_group=body['material_group'], subject_group=body['subject_group'],
                            classification_method='manual', classification_updated_at=datetime.now(timezone.utc).isoformat())
            try:
                self.store.save()
            except Exception:
                for item, original in zip(targets, originals):
                    item.clear()
                    item.update(original)
                raise ReplicationError('素材分类未能保存，请检查本机存储。', 500) from None
            return {'updated': [item['id'] for item in targets], 'asset': self._public_asset(asset)}

    def classify_reference(self, reference_id, body):
        """Save only an explicitly selected local routing label, never a capture."""
        if (not isinstance(body, dict) or set(body) != {'material_group', 'subject_group'}
                or not isinstance(body['material_group'], str) or body['material_group'] not in MATERIALS
                or not isinstance(body['subject_group'], str) or body['subject_group'] not in SUBJECTS):
            raise ReplicationError('请明确选择参考视频的素材分组和单人／多人分类。')
        if not isinstance(reference_id, str) or not re.fullmatch(r'tiktok-\d{10,25}', reference_id):
            raise ReplicationError('找不到可分类的已有参考视频。', 404)
        with self._lock, self.store.lock:
            reference = next((row for row in self._references() if row['id'] == reference_id), None)
            if reference is None:
                raise ReplicationError('找不到可分类的已有参考视频。', 404)
            if (body['material_group'] != 'unknown' or body['subject_group'] != 'unknown') and not reference['original_watched']:
                raise ReplicationError('参考视频尚无观看原片的记录，只能保留待确认分类。', 409)
            prior = self._reference_classifications.get(reference_id)
            if prior and all(prior[key] == body[key] for key in ('material_group', 'subject_group')):
                return {'updated': [reference_id], 'reference': reference}
            self._reference_classifications[reference_id] = dict(reference_id=reference_id, **body,
                classified_at=datetime.now(timezone.utc).isoformat())
            try:
                self._save_reference_classifications()
            except ReplicationError:
                if prior is None:
                    self._reference_classifications.pop(reference_id, None)
                else:
                    self._reference_classifications[reference_id] = prior
                raise
            result = next(row for row in self._references() if row['id'] == reference_id)
            return {'updated': [reference_id], 'reference': result}

    def sync(self):
        request = Request(CANDIDATE_URL, headers={'Accept': 'application/json', 'User-Agent': 'idore-replication/1.0'}, method='GET')
        try:
            status, _, raw = self._request_fn(request, timeout=25, max_bytes=MAX_CANDIDATES_BYTES)
            if status != 200 or not isinstance(raw, bytes) or len(raw) > MAX_CANDIDATES_BYTES:
                raise ReplicationError('无法同步公开候选，本机旧结果保留。', 502)
            document = self._validate_document(json.loads(raw))
        except ReplicationError:
            raise
        except Exception:
            raise ReplicationError('无法读取公开候选，本机旧结果保留。', 502) from None
        with self._lock:
            temporary = self.candidate_path.with_name('.discovery-' + uuid.uuid4().hex + '.tmp')
            try:
                if self.candidate_path.is_symlink():
                    raise OSError()
                self.candidate_path.parent.mkdir(parents=True, exist_ok=True)
                temporary.write_text(json.dumps(document, ensure_ascii=False, allow_nan=False), encoding='utf-8')
                temporary.replace(self.candidate_path)
            except (OSError, ValueError):
                raise ReplicationError('同步结果无法保存，本机旧数据保留。', 500) from None
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return {'synced': True, 'reference_count': len(document['videos']), 'synced_at': datetime.now(timezone.utc).isoformat()}

    def _selection(self, asset_id, reference_id, reference_url, confirmed, extra_prompt):
        blockers = []
        asset = self._asset(asset_id)
        public_asset = self._public_asset(asset) if asset else None
        reference = next((row for row in self._references() if row['id'] == reference_id), None)
        if public_asset is None:
            blockers.append('请选择素材库中已批准的自有角色图片。')
        else:
            blockers.extend(public_asset['blockers'])
        if reference is None:
            blockers.append('请选择已采集到的真实 TikTok 原视频。')
        else:
            blockers.extend(reference['blockers'])
        if (public_asset and reference and public_asset['material_group'] in ('male', 'female')
                and reference['material_group'] in ('male', 'female')
                and public_asset['material_group'] != reference['material_group']):
            blockers.append('角色图片与参考视频必须属于相同的男生／女生素材分组。')
        media = None
        if not reference_url:
            blockers.append('请提供所选原视频的 HTTPS 视频文件直链；TikTok 分享页不能作为视频输入。')
        else:
            try:
                media = _media_url(reference_url, 'video_url')
            except VideoError as exc:
                blockers.append(str(exc))
        if confirmed is not True:
            blockers.append('请确认视频文件确实对应所选 TikTok 原片，并允许作为本次动作参考。')
        raw, mime, error = self._file(asset, read=True) if asset else (None, None, None)
        if error and error not in blockers:
            blockers.append(error)
        prompt = ('Create a new video with one character using the two supplied references for different purposes. '
                  'IMAGE reference defines the character identity: preserve its face, hairstyle, body proportions, gender presentation and outfit. '
                  'VIDEO reference supplies visible gesture/dance action, rhythm, camera framing and movement; use it as motion guidance. '
                  'Keep the character from the image instead of replacing it with the person in the video. No extra characters, text or logos. '
                  'Additional motion preferences: ' + extra_prompt + '\nPreserve the image character identity throughout.')
        return dict(asset=public_asset, reference=reference, blockers=list(dict.fromkeys(blockers)), prompt=prompt), raw, mime, media

    def prepare(self, body):
        allowed = {'asset_id', 'reference_id', 'reference_video_url', 'reference_video_confirmed', 'prompt'}
        if not isinstance(body, dict) or set(body) - allowed:
            raise ReplicationError('复刻准备参数无效。')
        asset_id, reference_id = body.get('asset_id'), body.get('reference_id')
        if asset_id is not None and not isinstance(asset_id, str) or reference_id is not None and not isinstance(reference_id, str):
            raise ReplicationError('素材和参考视频编号格式无效。')
        url = body.get('reference_video_url', '')
        if not isinstance(url, str) or len(url) > 4096 or type(body.get('reference_video_confirmed', False)) is not bool:
            raise ReplicationError('请使用有效视频文件直链并确认其来源。')
        extra = _text(body.get('prompt', ''), empty=True)
        with self._lock, self.store.lock:
            result, raw, _, media = self._selection(asset_id, reference_id, url.strip(), body.get('reference_video_confirmed'), extra)
            now = time.time()
            self._plans = {key: plan for key, plan in self._plans.items() if plan['expires'] > now}
            plan_id = 'replication-' + uuid.uuid4().hex
            expires = now + PLAN_TTL
            if len(self._plans) >= 200:
                oldest = min(self._plans, key=lambda ident: self._plans[ident]['expires'])
                self._plans.pop(oldest, None)
            self._plans[plan_id] = dict(asset_id=asset_id, reference_id=reference_id, video_url=media or url.strip(),
                confirmed=body.get('reference_video_confirmed') is True, extra=extra, expires=expires,
                file_hash=hashlib.sha256(raw).hexdigest() if raw else None,
                classification=(result['asset']['material_group'], result['asset']['subject_group'], result['reference']['material_group'], result['reference']['subject_group'])
                if result['asset'] and result['reference'] else None, initial_blockers=bool(result['blockers']))
            plan = self._plans[plan_id]
            content_fingerprint = hashlib.sha256(json.dumps(dict(
                asset_id=asset_id, reference_id=reference_id, file_hash=plan['file_hash'],
                reference_video_url=plan['video_url'], canonical_prompt=result['prompt'],
                classification=plan['classification']), sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
            return dict(result, plan_id=plan_id, can_submit=not result['blockers'],
                        content_fingerprint=content_fingerprint,
                        required_inputs=['character_image', 'reference_video'], has_reference_video=bool(media),
                        expires_at=datetime.fromtimestamp(expires, timezone.utc).isoformat(),
                        note='模型使用角色图控制身份、视频指导动作；不能保证动作逐帧或节拍完全一致。')

    def submit(self, body):
        allowed = {'plan_id', 'idempotency_key', 'model', 'duration', 'resolution', 'aspect_ratio', 'generate_audio', 'consent'}
        if (not isinstance(body, dict) or set(body) - allowed or body.get('consent') is not True
                or body.get('generate_audio') is not False):
            raise ReplicationError('请确认本次素材传输与视频费用；当前复刻流程不生成音轨。')
        plan_id = body.get('plan_id')
        if not isinstance(plan_id, str) or not PLAN_ID.fullmatch(plan_id):
            raise ReplicationError('复刻准备方案无效，请重新准备。', 404)
        token = body.get('idempotency_key')
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', token):
            raise ReplicationError('生成请求标识格式无效。')
        with self._lock, self.store.lock:
            plan = self._plans.get(plan_id)
            if plan is None or plan['expires'] <= time.time():
                raise ReplicationError('准备方案已过期，请重新确认素材和参考视频。', 409)
            result, raw, mime, media = self._selection(plan['asset_id'], plan['reference_id'], plan['video_url'], plan['confirmed'], plan['extra'])
            if plan['initial_blockers'] or result['blockers']:
                raise ReplicationError('复刻条件尚未满足，请重新准备并确认所有素材。', 409)
            classification = (result['asset']['material_group'], result['asset']['subject_group'], result['reference']['material_group'], result['reference']['subject_group'])
            if classification != plan['classification'] or hashlib.sha256(raw).hexdigest() != plan['file_hash']:
                raise ReplicationError('素材文件或分类已变化，请重新准备，尚未提交生成。', 409)
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            link = next((item for item in self._links if item['request_key_hash'] == token_hash), None)
            if link and (link['asset_id'], link['reference_id']) != (plan['asset_id'], plan['reference_id']):
                raise ReplicationError('相同请求标识已经用于另一组素材，请先查询原任务。', 409)
            if link is None:
                link = dict(request_key_hash=token_hash, asset_id=plan['asset_id'], reference_id=plan['reference_id'])
                self._links.append(link)
            # Retry saving even if an earlier failed write left the link in
            # memory. No potentially paid call may bypass durable source IDs.
            self._save_links()
            image_data = 'data:' + mime + ';base64,' + base64.b64encode(raw).decode('ascii')
            request = {key: body.get(key) for key in ('idempotency_key', 'model', 'duration', 'resolution', 'aspect_ratio', 'generate_audio', 'consent')}
            request.update(mode='references', prompt=result['prompt'], references=[{'type': 'image_url', 'url': image_data}, {'type': 'video_url', 'url': media}])
            try:
                job = self.video_service.submit(request)
            except VideoError as exc:
                raise ReplicationError(str(exc), exc.status) from None
            if isinstance(job.get('id'), str) and re.fullmatch(r'video-[a-f0-9]{32}', job['id']):
                link['job_id'] = job['id']
                self._save_links()
            annotated = self.annotate_jobs({'jobs': [job]})['jobs'][0]
            return {'job': annotated, 'asset_id': plan['asset_id'], 'reference_id': plan['reference_id'], 'plan_id': plan_id}

    def annotate_jobs(self, payload):
        """Add safe source IDs/names to the existing VideoService jobs response."""
        result = copy.deepcopy(payload)
        if not isinstance(result, dict) or not isinstance(result.get('jobs'), list):
            return result
        with self._lock, self.store.lock:
            assets = {asset['id']: asset for asset in self._assets()}
            try:
                references = {row['id']: row for row in self._references()}
            except ReplicationError:
                references = {}
            for job in result['jobs']:
                if not isinstance(job, dict):
                    continue
                token = job.get('idempotency_key')
                token_hash = hashlib.sha256(token.encode()).hexdigest() if isinstance(token, str) else None
                link = next((item for item in self._links if item.get('job_id') == job.get('id') or item['request_key_hash'] == token_hash), None)
                if not link:
                    continue
                job.update(source_asset_id=link['asset_id'], source_reference_id=link['reference_id'])
                asset = assets.get(link['asset_id'])
                reference = references.get(link['reference_id'])
                if asset:
                    job['source_character'] = str(asset.get('character') or '')[:120]
                    job['source_asset_name'] = str(asset.get('name') or '')[:300]
                if reference:
                    job['source_reference_title'] = reference['title'][:300]
            return result
