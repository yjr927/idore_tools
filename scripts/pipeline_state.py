"""Public production queue. Approval binds the exact media, caption and tags."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


class PipelineError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def revision(item):
    fields = ('id', 'source_id', 'source_url', 'character', 'character_image',
              'material_group', 'cast_type', 'video_path', 'sha256', 'clay_path',
              'caption', 'hashtags', 'status')
    return hashlib.sha256(json.dumps({k: item.get(k) for k in fields},
        sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def touch(item):
    item['updated_at'] = now()
    item['revision'] = revision(item)


def classified(video):
    observed = video.get('material_classification') or {}
    group = observed.get('material_group', video.get('material_group', 'unknown'))
    cast = observed.get('subject_group')
    if not cast:
        solo = (video.get('checks') or {}).get('solo')
        cast = 'solo' if solo is True else 'group' if solo is False else 'unknown'
    return group, cast


def match_character(video, characters):
    group, cast = classified(video)
    if group not in ('male', 'female', 'mixed') or cast not in ('solo', 'group'):
        return None, 'waiting_classification', '等待确认男生／女生素材分组和人数。'
    matches = [c for c in characters if c.get('enabled') is True
        and c.get('material_group') == group and c.get('cast_type') == cast]
    if not matches:
        return None, 'waiting_material', '等待同分组、同人数的角色素材。'
    # Stable ordering makes repeated sync deterministic; never substitute a different group.
    character = sorted(matches, key=lambda c: c['id'])[0]
    if cast != 'solo':
        return character, 'waiting_material', '多人白模跟踪尚未接入，先保留任务和已匹配素材。'
    if video.get('kind') == 'pov':
        return character, 'needs_attention', '此原片主要是表情或口型，身体白模不足以复现，请选择动作素材。'
    transformation = video.get('transformation', 'unknown')
    if transformation == 'unknown':
        return character, 'waiting_classification', '请先确认原片是否有换装或妆容变化。'
    if transformation not in ('none', None) and not all(character.get(k) for k in ('before_prompt', 'after_prompt')):
        return character, 'waiting_material', '变装任务需要这个角色的前后造型要求。'
    if video.get('original_watched') is not True:
        return character, 'waiting_classification', '原片尚未播放核验，暂不提交生成。'
    return character, 'queued', '角色素材已匹配，等待提取动作和生成白模。'


def sync_queue(document, discovery, characters, today=None):
    today = today or datetime.now(ZoneInfo('America/New_York')).date().isoformat()
    items = document.setdefault('items', [])
    index = {i['source_id']: i for i in items if i.get('source_id')}
    selected = []
    # The importer owns the daily quota; consume its actual selected video snapshots.
    for run in discovery.get('runs', []):
        stamp = run.get('observed_at', run.get('started_at', ''))
        try:
            day = datetime.fromisoformat(stamp.replace('Z', '+00:00')).astimezone(ZoneInfo('America/New_York')).date().isoformat()
        except (ValueError, TypeError):
            continue
        if day != today:
            continue
        for v in run.get('video_snapshots', []):
            if v.get('id') and v['id'] not in selected:
                selected.append(v['id'])
    library = {v['id']: v for v in discovery.get('videos', []) if v.get('id')}
    for source_id in selected[:4]:
        video = library.get(source_id)
        if not video:
            continue
        prior = index.get(source_id)
        if prior and prior.get('status') not in ('waiting_classification', 'waiting_material'):
            continue
        character, status, message = match_character(video, characters)
        group, cast = classified(video)
        item = prior or {'id': 'task-' + source_id.removeprefix('tiktok-'), 'created_at': now()}
        item.update(source_id=source_id, source_url=video['url'], title=video.get('title', '动作跟拍')[:200],
                    material_group=group, cast_type=cast, status=status, stage='matching', message=message,
                    character=character['name'] if character else '',
                    character_id=character['id'] if character else '',
                    character_image=character.get('image_url', '') if character else '',
                    transformation=video.get('transformation', 'unknown'),
                    motion_summary=video.get('motion_summary', '')[:2000], kind=video.get('kind', ''),
                    original_watched=video.get('original_watched') is True,
                    observed_at=video.get('first_seen_at', now()))
        touch(item)
        if not prior:
            items.append(item)
            index[source_id] = item
    items.sort(key=lambda item: item.get('created_at', ''), reverse=True)
    return document


def tags_from_input(value):
    if not isinstance(value, str) or len(value) > 1000:
        raise PipelineError('invalid_tags', '标签格式不正确。')
    tags = value.split()
    if not 1 <= len(tags) <= 15 or any(not re.fullmatch(r'#[A-Za-z0-9_]{1,80}', tag) for tag in tags):
        raise PipelineError('invalid_tags', '请填写 1–15 个以 # 开头的英文标签。')
    return list(dict.fromkeys(tags))


def approve(item, expected_revision, caption, hashtags, actor):
    if expected_revision != item.get('revision') or revision(item) != expected_revision:
        raise PipelineError('stale_review', '作品已有更新，请刷新后重新审核。')
    if item.get('status') != 'pending_review' or not item.get('video_path') or not item.get('caption'):
        raise PipelineError('not_ready', '视频和文案都完成后才能审核。')
    if not isinstance(caption, str) or not 1 <= len(caption.strip()) <= 2000:
        raise PipelineError('invalid_caption', '文案需为 1–2000 字符。')
    tags = tags_from_input(hashtags)
    if len(caption.strip() + ' ' + ' '.join(tags)) > 2200:
        raise PipelineError('caption_too_long', '文案与标签合计不能超过 2200 字符。')
    old_tags = {h['tag']: h for h in item.get('hashtags', []) if isinstance(h, dict) and 'tag' in h}
    item.update(caption=caption.strip(), hashtags=[old_tags.get(tag, {'tag': tag, 'reason': '人工审核调整',
        'category': 'content', 'source_url': None, 'observed_at': None}) for tag in tags], status='approved', stage='review')
    touch(item)
    item['approval'] = {'status': 'approved', 'actor': actor, 'approved_at': now(),
                        'revision': item['revision'], 'sha256': item['sha256']}


def approved_content(item):
    approved = item.get('approval') or {}
    shadow = dict(item, status='approved')
    return (approved.get('status') == 'approved' and approved.get('sha256') == item.get('sha256')
            and approved.get('revision') == revision(shadow))
