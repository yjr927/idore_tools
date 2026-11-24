"""Plan and edit a verified TikTok motion remake with approved character photography."""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.archive_generated_videos import merge, probe
from scripts.named_motion import (DEFERRED_DATE, NAMED, NAMED_METHOD, SCHEDULE, SCHEDULE_DATE, SELECTION,
                                 matches_named_film, named_source)
from scripts.pipeline_state import now, touch
from scripts.run_pipeline import REPOSITORY, SITE, STATE, checkpoint, read

METHOD = 'agent_motion'
MAX_MEDIA = 16 * 1024 * 1024
NEW_YORK = ZoneInfo('America/New_York')
DAILY_SLOTS = ((9, 'morning'), (14, 'afternoon'), (18, 'evening'))
DAILY_METHODS = {METHOD, 'original_character_edit', NAMED_METHOD}
ACTIVE_STAGES = {'agent_planning', 'plan_ready', 'preparing_motion', 'rendering_clay', 'generating_styles',
                 'generating_video', 'rendering_sequence'}


def due_slots(at):
    local = at.astimezone(NEW_YORK)
    if local.date() == DEFERRED_DATE:
        return []
    if local.date() == SCHEDULE_DATE:
        return list(NAMED) if local.hour >= SCHEDULE['start_hour'] else []
    return [f'{local.date()}-{name}' for hour, name in DAILY_SLOTS if local.hour >= hour]


def current_slot(at):
    slots = due_slots(at)
    return slots[-1] if slots else None


def next_slot(at, films, rows, attempted=()):
    """Allocate a due slot using completed media, including work resumed from an earlier day."""
    local_day = at.astimezone(NEW_YORK).date()
    due = due_slots(at)
    occupied = set(attempted)
    completed = set()
    for film in films:
        digest = film.get('sha256')
        if (local_day == SCHEDULE_DATE or film.get('generation_method') == NAMED_METHOD) and not matches_named_film(film):
            continue
        if (film.get('kind') != 'character' or film.get('generation_method') not in DAILY_METHODS
                or not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest)
                or film.get('video_path') != 'generated/video-' + digest[:24] + '.mp4'):
            continue
        occupied.add(film.get('sequence_slot'))
        stamp = film.get('generated_at') or film.get('created_at')
        try:
            generated = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        except (TypeError, AttributeError, ValueError):
            continue
        if generated.tzinfo and generated.astimezone(NEW_YORK).date() == local_day:
            completed.add(digest)
    if len(completed) >= len(due):
        return None
    for row in rows:
        slot = row.get('sequence_slot')
        if row.get('generation_method') not in (METHOD, NAMED_METHOD) or not slot:
            continue
        if local_day == SCHEDULE_DATE and slot not in NAMED:
            continue
        if slot in NAMED and local_day != SCHEDULE_DATE and not row.get('remote_id'):
            continue
        if row.get('status') == 'needs_attention':
            occupied.add(slot)
        if row.get('status') in ACTIVE_STAGES and slot not in occupied:
            return slot
    return next((slot for slot in due if slot not in occupied), None)


def source_file(site, photo):
    url = urlsplit(photo.get('image_url', ''))
    match = re.fullmatch(r'/yjr927/idore_tools/[a-f0-9]{40}/site/(characters/.+)', unquote(url.path))
    if (url.scheme != 'https' or url.netloc != 'raw.githubusercontent.com' or url.query or
            url.fragment or not match or not re.fullmatch(r'[0-9a-f]{64}', photo.get('sha256', ''))):
        return None
    relative = PurePosixPath(match[1])
    if '..' in relative.parts or relative.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp'):
        return None
    path = site.joinpath(*relative.parts)
    if (not path.resolve().is_relative_to((site / 'characters').resolve()) or not path.is_file() or
            path.is_symlink() or not 0 < path.stat().st_size <= MAX_MEDIA):
        return None
    return path if hashlib.sha256(path.read_bytes()).hexdigest() == photo['sha256'] else None


def approved_photos(catalog, site):
    photos = [dict(p) for p in catalog if p.get('enabled') is True and p.get('auto_match_eligible') is True
              and not p.get('auto_match_blockers') and p.get('cast_type') == 'solo'
              and p.get('material_group') in ('male', 'female') and p.get('id')
              and p.get('asset_kind') in ('portrait', 'single_character') and source_file(site, p)]
    by_id = {p['id']: p for p in photos}
    for p in photos:
        pair = [by_id.get(p.get(axis + '_photo_id')) for axis in ('before', 'after')]
        valid = all(v and v['material_group'] == p['material_group'] and
                    (v.get('role_id') or v['id']) == (p.get('role_id') or p['id']) for v in pair)
        p['_agent_styles_ready'] = bool(valid and pair[0]['sha256'] != pair[1]['sha256'])
        if p['_agent_styles_ready']:
            p['_agent_style_urls'] = [v['image_url'] for v in pair]
    return photos


def text_frame(text, destination):
    from PIL import Image, ImageDraw, ImageFont
    frame = Image.new('RGBA', (720, 1280))
    draw = ImageDraw.Draw(frame)
    try:
        font = ImageFont.truetype('DejaVuSans-Bold.ttf', 38)
    except OSError:
        font = ImageFont.load_default(size=38)
    lines = ['']
    for word in text.split():
        line = (lines[-1] + ' ' + word).strip()
        if draw.textlength(line, font=font) > 575 and lines[-1]:
            lines.append(word)
        else:
            lines[-1] = line
    text = '\n'.join(lines)
    if text:
        box = draw.multiline_textbbox((360, 945), text, anchor='mm', font=font, spacing=8, align='center')
        draw.rounded_rectangle((box[0]-16, box[1]-10, box[2]+16, box[3]+12), radius=12, fill=(12, 12, 18, 180))
        draw.multiline_text((360, 945), text, anchor='mm', font=font, fill='white', spacing=8, align='center')
    frame.save(destination)


def finish_cut(motion, shots, output):
    scales = {'wide': (720, 1280), 'medium': (792, 1408), 'close': (864, 1536)}
    args = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
            '-filter_complex_threads', '2', '-i', str(motion)]
    filters = ['[0:v]fps=30,scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,'
               'setsar=1,tpad=stop_mode=clone:stop_duration=0.25,split=3[m0][m1][m2]']
    for i, shot in enumerate(shots):
        label = output.parent / f'shot-{i}.png'
        text_frame(shot['overlay'], label)
        args.extend(['-i', str(label)])
        width, height = scales[shot['framing']]
        filters.append(f'[m{i}]trim=start={i*2}:end={i*2+2},setpts=PTS-STARTPTS,scale={width}:{height},'
                       f'crop=720:1280:(iw-ow)/2:(ih-oh)*0.38,setsar=1[v{i}]')
        filters.append(f'[v{i}][{i+1}:v]overlay=eof_action=repeat,format=yuv420p[s{i}]')
    filters.append('[s0][s1][s2]concat=n=3:v=1:a=0[out]')
    args.extend(['-filter_complex', ';'.join(filters), '-map', '[out]', '-an', '-r', '30', '-c:v', 'libx264',
                 '-preset', 'fast', '-crf', '21', '-maxrate', '5M', '-bufsize', '8M',
                 '-threads', '2', '-movflags', '+faststart', str(output)])
    subprocess.run(args, check=True, capture_output=True, timeout=180)
    duration, resolution, aspect = probe(output)
    if (not 0 < output.stat().st_size < MAX_MEDIA or not 5.98 <= duration <= 6.05
            or resolution != '720p' or aspect != '9:16'):
        raise ValueError('Final cut does not match the six-second vertical delivery format')


def update(document, row, save, status, stage, message, code=''):
    if (row.get('status'), row.get('stage'), row.get('message'), row.get('error_code')) != (status, stage, message, code):
        row.update(status=status, stage=stage, message=message, error_code=code)
        touch(row)
        save(document)


def capture_inputs(discovery, photos, consumed):
    from replication_service import _effective_reference
    from scripts.trend_quality import eligible_trend
    verified = [s for s in discovery.get('videos', []) if eligible_trend(s) and s.get('id') not in consumed]
    motion = []
    for s in verified:
        material, cast, watched, _ = _effective_reference(s)
        if (watched and cast == 'solo' and material in ('male', 'female') and s.get('kind') in ('hands', 'transition')
                and s.get('transformation') in ('none', 'outfit', 'makeup', 'makeup_and_outfit')):
            candidates = [p for p in photos if p['material_group'] == material and
                          (s['transformation'] == 'none' or p.get('_agent_styles_ready'))]
            if candidates:
                motion.append(s)
    return verified, motion


def attach_plan(row, plan, motion, photos):
    from replication_service import _effective_reference
    from scripts.trend_quality import verified_peers
    source = next(s for s in motion if s['id'] == plan['source_id'])
    character = next(p for p in photos if p['id'] == plan['photo_ids'][0])
    named = NAMED.get(row['sequence_slot'])
    if named:
        source = named_source(row['sequence_slot'], source)
        if source is None or plan.get('selection_basis') != SELECTION:
            raise ValueError('Reference does not match the scheduled selection')
        group, cast = source['material_group'], source['cast_type']
    else:
        group, cast, _, _ = _effective_reference(source)
    shots = plan['shots']
    if (source['url'] != plan['source_url'] or plan['provider'] != 'openrouter' or not plan['model'].startswith('openai/')
            or plan.get('execution_host') != 'cloudflare'
            or plan['photo_ids'] != [character['id']] or group != character['material_group']
            or (source['transformation'] != 'none' and not character['_agent_styles_ready'])
            or plan['material_group'] != group or cast != 'solo' or len(shots) != 3
            or not all(s['photo_id'] == character['id'] and s['framing'] in ('wide', 'medium', 'close')
                       and isinstance(s['overlay'], str) for s in shots)):
        raise ValueError('Plan does not match the approved role and source')
    peers = verified_peers(source)
    if source['transformation'] != 'none':
        row['style_reference_urls'] = list(character['_agent_style_urls'])
    row.update(planner='openai', planner_model=plan['model'], planner_provider=plan['provider'],
               execution_host=plan['execution_host'], generation_method=NAMED_METHOD if named else METHOD,
               sequence_type='selected_reference' if named else 'trend_remake',
               trend_id=source['id'], trend_verified=not bool(named), source_title=source['title'], source_url=source['url'],
               source_metrics={k: (source.get('metrics') or {}).get(k) for k in ('views', 'likes')},
               source_metric_display={k: (source.get('metric_display') or {}).get(k) for k in ('views', 'likes')},
               source_observed_at=source.get('last_seen_at') or source.get('first_seen_at'),
               source_peers=peers, peer_count=len(peers), source_photo_ids=[character['id']],
               source_photo_sha256=[character['sha256']], source_roles=[character['name']],
               title=character['name'] + ' · ' + source['title'][:120], character=character['name'],
               character_id=character['id'], material_group=group, cast_type=cast,
               shots=shots, caption=plan['caption'], hashtags=plan['hashtags'], hook=plan['hook'])
    if named:
        row.update({k: source[k] for k in ('selection_basis','source_material_group','source_transformation','casting_basis')})
    return source, character


def produce(document, catalog, discovery, site, run_id, slot, save, planner=None, motion_renderer=None):
    from scripts.agent_film_planner import FilmPlanError, plan_film
    from scripts.run_pipeline import render_agent_motion
    rows = document.setdefault('items', [])
    manifest = site / 'data' / 'creations.json'
    gallery = read(manifest, {'items': []})
    archived_slots = {f.get('sequence_slot') for f in gallery['items'] if f.get('generation_method') in DAILY_METHODS
                      and (f.get('sequence_slot') not in NAMED or matches_named_film(f))}
    named = NAMED.get(slot)
    row = None if named else next((r for r in rows if r.get('generation_method') == METHOD
                and r.get('status') in ACTIVE_STAGES and r.get('sequence_slot') not in archived_slots), None)
    if not row and not slot:
        return False
    if row is None:
        row = next((r for r in rows if r.get('id') == 'agent-' + slot), None)
    slot = row['sequence_slot'] if row else slot
    if slot in archived_slots:
        return False
    if row is None:
        row = {'id': 'agent-' + slot, 'generation_method': NAMED_METHOD if named else METHOD,
               'created_at': now(), 'sequence_slot': slot, 'title': named['title'] if named else 'TikTok 热门复刻'}
        if named:
            row.update(trend_id=named['source_id'], source_url=named['url'], selection_basis=SELECTION, trend_verified=False)
        rows.insert(0, row)
    if row.get('status') == 'needs_attention':
        return False
    consumed = {f.get('trend_id') for f in gallery['items'] if f.get('generation_method') == METHOD}
    consumed.update(r['trend_id'] for r in rows if r is not row and r.get('trend_id') and r.get('plan_reserved_at'))
    photos = approved_photos(catalog, site)
    if named:
        observed = next((s for s in discovery.get('videos', []) if s.get('id') == named['source_id']), None)
        selected = named_source(slot, observed)
        if selected is None:
            update(document, row, save, 'waiting_material', 'matching', '指定原片缺少可核验的单人动作观察。')
            return False
        verified = [selected]
        motion = [selected] if any(p['material_group'] == selected['material_group'] for p in photos) else []
    else:
        verified, motion = capture_inputs(discovery, photos, consumed)
        row.update(source_scan_count=len(discovery.get('videos', [])), eligible_trend_count=len(verified))
    if row.get('agent_plan') and not motion:
        update(document, row, save, 'needs_attention', 'planning', '已保存分镜的原片或照片未通过最新核验。', 'plan_outdated')
        return False
    if not verified:
        count = row['source_scan_count']
        update(document, row, save, 'waiting_trend', 'matching', f'已检查 {count} 条候选，等待同时达到播放量、点赞数和同玩法创作者门槛的未复刻原片。')
        return False
    if not motion:
        update(document, row, save, 'waiting_material', 'matching', '等待能提取动作、已确认造型变化，并有同分组角色照片的原片。')
        return False
    if not row.get('agent_plan'):
        if row.get('plan_reserved_at'):
            update(document, row, save, 'needs_attention', 'planning', '上次规划调用缺少可恢复结果，已停止重复调用。', 'submission_uncertain')
            return False
        if planner is None and not os.environ.get('OPENROUTER_API_KEY'):
            update(document, row, save, 'waiting_openai', 'planning', '等待 GitHub 配置项目的 OpenRouter 密钥。', 'missing_key')
            return False
        def reserve_inference():
            row['plan_reserved_at'] = now()
            update(document, row, save, 'agent_planning', 'planning', '正在向 OpenRouter 提交分镜策划请求。')

        try:
            if not planner and named:
                from scripts.agent_film_planner import plan_named_film
                planner = plan_named_film
            plan = (planner or plan_film)(motion, photos, slot=slot, prior_films=gallery['items'],
                                          before_inference=reserve_inference)
            source, character = attach_plan(row, plan, motion, photos)
        except (FilmPlanError, KeyError, ValueError, IndexError, StopIteration) as exc:
            code = getattr(exc, 'code', 'invalid_output')
            errors = {'insufficient_quota': '项目的 OpenRouter 余额或额度不足。', 'auth': 'OpenRouter 未接受当前项目凭证。',
                      'rate_limited': '规划模型当前限流。', 'missing_model': '项目尚未配置可用的 OpenAI 系列模型。',
                      'no_fit': 'OpenAI 未找到符合要求的原片与角色组合。', 'invalid_output': 'OpenAI 的分镜未通过素材与文案校验。',
                      'planner_unavailable': 'Cloudflare 上的策划服务暂时无法访问。',
                      'submission_uncertain': '未收到可确认的 OpenAI 规划结果。'}
            if code == 'planner_request_unavailable' and not row.get('plan_reserved_at'):
                # Only the stateless Worker was contacted. A later scheduled run can retry.
                status, message = 'waiting_openai', 'Cloudflare 未能准备分镜请求，尚未调用模型，等待重试。'
            elif code == 'planner_accept_unavailable':
                status, message = 'needs_attention', '模型已返回分镜，Cloudflare 校验未完成，已阻止重复提交。'
            else:
                status, message = 'needs_attention', errors.get(code, 'OpenAI 规划未完成。')
            http_status = getattr(exc, 'status', None)
            if isinstance(http_status, int):
                message += f' (HTTP {http_status})'
            update(document, row, save, status, 'planning', message, code)
            return False
        row['agent_plan'] = plan
        update(document, row, save, 'plan_ready', 'planning', 'OpenAI 分镜、素材及文案已保存。')
    else:
        try:
            source, character = attach_plan(row, row['agent_plan'], motion, photos)
        except (KeyError, ValueError, IndexError, StopIteration):
            update(document, row, save, 'needs_attention', 'planning', '已保存分镜的原片或照片未通过最新核验。', 'plan_outdated')
            return False
    try:
        with tempfile.TemporaryDirectory(prefix='idore-trend-remake-') as scratch:
            directory = Path(scratch)
            if not motion_renderer and named:
                from scripts.run_pipeline import render_named_motion
                motion_renderer = render_named_motion
            raw = (motion_renderer or render_agent_motion)(document, row, source, character, directory)
            if raw is None:
                return False
            update(document, row, save, 'rendering_sequence', 'edit', '动作复刻已完成，正在按 OpenAI 分镜剪辑并加字幕。')
            movie = directory / 'final-cut.mp4'
            finish_cut(raw, row['shots'], movie)
            content = movie.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if any(f['sha256'] == digest for f in gallery['items']):
            raise ValueError('Duplicate media in gallery')
        stamp = now()
        merge(site, [(content, row.get('model') or 'bytedance/seedance-2.5')], {'id': run_id, 'updated_at': stamp}, REPOSITORY)
        gallery = read(manifest)
        film = next(f for f in gallery['items'] if f['sha256'] == digest)
        fields = ('title', 'character', 'material_group', 'cast_type', 'generation_method', 'source_photo_ids',
                  'source_photo_sha256', 'source_roles', 'sequence_type', 'sequence_slot', 'planner', 'planner_model',
                  'planner_provider', 'execution_host', 'trend_id', 'trend_verified', 'source_title', 'source_url', 'source_observed_at', 'source_metrics',
                  'source_metric_display', 'source_peers', 'peer_count', 'hook', 'caption', 'hashtags', 'shots')
        film.update({k: row[k] for k in fields}, generated_at=stamp, timestamp_basis='generation_completed', bytes=len(content))
        if named:
            film.update({k: row[k] for k in ('storyboard_author','copy_provider','validation_host','recovery_method') if k in row})
            film.update({k: row[k] for k in ('selection_basis','source_material_group','source_transformation',
                                             'casting_basis','motion_reference_method')})
        temporary = manifest.with_suffix('.tmp')
        temporary.write_text(json.dumps(gallery, ensure_ascii=False, indent=2) + '\n')
        temporary.replace(manifest)
        row.update(video_path=film['video_path'], poster_path=film['poster_path'], sha256=digest, generated_at=stamp)
        update(document, row, save, 'generated', 'gallery', '成片和 TikTok 文案已保存。')
    except Exception as exc:
        code = getattr(exc, 'code', None) or 'motion_render_failed'
        message = getattr(exc, 'message', '') or '动作生成或后期剪辑未完成。'
        update(document, row, save, 'needs_attention', row.get('stage') or 'video', message, code)
        raise
    return True


def produce_due(document, catalog, discovery, site, run_id, save, clock, producer=produce):
    count = 0
    attempted = set()
    while len(attempted) < len(DAILY_SLOTS):
        gallery = read(site / 'data' / 'creations.json', {'items': []})
        slot = next_slot(clock(), gallery['items'], document.get('items', []), attempted)
        if not slot:
            break
        attempted.add(slot)
        try:
            generated = producer(document, catalog, discovery, site, run_id, slot, save)
        except Exception:
            # Only move on when this particular failure has already been checkpointed.
            if slot not in NAMED or not any(r.get('sequence_slot') == slot and r.get('status') == 'needs_attention'
                                             for r in document.get('items', [])):
                raise
            generated = False
        if generated:
            count += 1
        elif slot not in NAMED:
            break
    return count


def main():
    run_id = os.environ.get('GITHUB_RUN_ID', '')
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != REPOSITORY
            or os.environ.get('GITHUB_REF') != 'refs/heads/main' or not run_id.isdigit()):
        raise SystemExit('The publisher requires a trusted main workflow.')
    document = read(STATE, {'schema_version': 1, 'items': [], 'connections': {}, 'requests': []})
    catalog = read(SITE / 'data' / 'characters.json')['items']
    discovery = read(SITE / 'data' / 'discovery.json')
    if sys.argv[1:] == ['--preflight']:
        from scripts.trend_quality import eligible_trend
        gallery = read(SITE / 'data' / 'creations.json', {'items': []})
        slot = next_slot(datetime.now(NEW_YORK), gallery['items'], document.get('items', []))
        needs_motion = bool(slot and (slot in NAMED or any(eligible_trend(s) for s in discovery.get('videos', []))))
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                print('motion=' + str(needs_motion).lower(), file=output)
                from scripts.run_pipeline import named_observation_only
                observed_only = slot in NAMED and named_observation_only(SITE)
                print('observed_only=' + str(observed_only).lower(), file=output)
        return
    before = STATE.read_bytes() if STATE.exists() else b''
    try:
        count = produce_due(document, catalog, discovery, SITE, run_id, checkpoint, lambda: datetime.now(NEW_YORK))
    finally:
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                state_changed = STATE.exists() and STATE.read_bytes() != before
                print('changed=' + str(state_changed).lower(), file=output)
    print(f'Archived {count} new motion remakes. See the production queue for remaining work.')


if __name__ == '__main__':
    main()
