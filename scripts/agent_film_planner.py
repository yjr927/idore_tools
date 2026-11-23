"""Execute a Cloudflare film plan using the project’s OpenRouter credential for inference."""
from __future__ import annotations

from collections import defaultdict
import json
import os
import re
from urllib.error import HTTPError
from urllib.request import Request, build_opener
from urllib.parse import unquote, urlsplit

from creative_service import CreativeService, TEXT_MODEL, _NoRedirect
from replication_service import _effective_reference
from scripts.pipeline_copy import DEFAULT_ROUTER_MODEL
from scripts.trend_quality import canonical_video, eligible_trend, verified_peers

# Each paid decision sees a bounded, gender-balanced visual shortlist.
MAX_BUCKET_PHOTOS = 24
MAX_TRENDS = 16
FRAMINGS = ['wide', 'medium', 'close']
WORKER = 'https://idore-tools.yjr927.workers.dev/agent/plan/'
OPENROUTER = 'https://openrouter.ai/api/v1/chat/completions'
MAX_RESULT = 256 * 1024


class FilmPlanError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _image_ref(photo):
    if not isinstance(photo, dict) or photo.get('enabled') is not True or photo.get('auto_match_eligible') is not True:
        return None
    if photo.get('auto_match_blockers') or photo.get('material_group') not in ('male', 'female'):
        return None
    if photo.get('cast_type') not in ('solo', 'group') or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', str(photo.get('id', ''))):
        return None
    if not re.fullmatch(r'[0-9a-f]{64}', str(photo.get('sha256', ''))):
        return None
    try:
        uri = urlsplit(photo.get('image_url', ''))
        path = unquote(uri.path)
        if (uri.scheme == 'https' and uri.netloc == 'raw.githubusercontent.com' and not uri.query and not uri.fragment
                and re.fullmatch(r'/yjr927/idore_tools/[0-9a-f]{40}/site/characters/.+\.(?:png|jpg|jpeg|webp)', path, re.I)
                and all(part not in ('', '.', '..') for part in path[1:].split('/'))):
            return uri.geturl()
    except (TypeError, ValueError):
        pass
    return None


def _table(trends, photos, prior_films):
    used_trends = set()
    used_photos = defaultdict(int)
    for film in prior_films:
        used_trends.add(film.get('source_id') or film.get('trend_id'))
        for pid in set(film.get('source_photo_ids') or ()):
            used_photos[pid] += 1
    candidates = {}
    for trend in trends:
        if not eligible_trend(trend):
            continue
        url, _, video_id = canonical_video(trend['url'])
        tid = trend.get('id')
        if not isinstance(tid, str) or tid != 'tiktok-' + video_id or tid in used_trends:
            continue
        group, cast, watched, _ = _effective_reference(trend)
        transformation = trend.get('transformation')
        if (watched and group in ('male', 'female') and cast in ('solo', 'group') and
                transformation in ('none','outfit','makeup','makeup_and_outfit')):
            candidates[tid] = {'id': tid, 'source_url': url, 'material_group': group, 'cast_type': cast,
                'transformation': transformation, 'title': str(trend.get('title', ''))[:130], 'mechanic': str(trend.get('motion_summary', ''))[:650],
                'notes': str(trend.get('recreation_notes', ''))[:450], 'metrics': {
                    key: trend['metrics'][key] for key in ('views', 'likes')},
                'other_creators': len(verified_peers(trend)), 'observed_at': str(trend.get('last_seen_at', ''))[:48]}
    if not candidates:
        raise FilmPlanError('waiting_trend')
    buckets = {(row['material_group'], row['cast_type']) for row in candidates.values()}
    shortlist = {}
    counts = defaultdict(int)
    for photo in sorted((p for p in photos if isinstance(p, dict)), key=lambda p: (used_photos.get(p.get('id'), 0), str(p.get('id', '')))):
        group = (photo.get('material_group'), photo.get('cast_type'))
        if group not in buckets or counts[group] >= MAX_BUCKET_PHOTOS or not _image_ref(photo) or photo['id'] in shortlist:
            continue
        shortlist[photo['id']] = photo
        counts[group] += 1
    candidates = {tid: row for tid, row in candidates.items() if any(
        photo['material_group'] == row['material_group'] and photo['cast_type'] == row['cast_type'] and
        (row['transformation'] == 'none' or photo.get('_agent_styles_ready') is True)
        for photo in shortlist.values())}
    if not candidates:
        raise FilmPlanError('waiting_material')
    selected = sorted(candidates.values(), key=lambda r: (r['observed_at'], r['id']), reverse=True)[:MAX_TRENDS]
    return {row['id']: row for row in selected}, shortlist


def _slate(model, slot, candidates, photos):
    visual = []
    for pid, photo in photos.items():
        visual.append({'photo_id': pid, 'role_id': photo.get('role_id') or pid, 'name': str(photo.get('name', ''))[:100],
                       'ip': str(photo.get('ip', ''))[:100], 'material_group': photo['material_group'],
                       'cast_type': photo['cast_type'], 'styles_ready': photo.get('_agent_styles_ready') is True, 'image_url': photo['image_url']})
    return {'model': model, 'slot': slot, 'trends': list(candidates.values()), 'photos': visual}


def _post(url, payload, key=None):
    if url not in (WORKER + 'request', WORKER + 'accept', OPENROUTER) or (key is not None) != (url == OPENROUTER):
        raise FilmPlanError('invalid_output')
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
    if key is not None:
        headers['Authorization'] = 'Bearer ' + key
    request = Request(url, method='POST', data=json.dumps(payload, separators=(',', ':')).encode(), headers=headers)
    try:
        with build_opener(_NoRedirect).open(request, timeout=120 if key else 25) as response:
            if response.status != 200 or response.geturl() != url:
                raise FilmPlanError('submission_uncertain' if key else 'planner_unavailable')
            raw = response.read(MAX_RESULT + 1)
    except HTTPError as exc:
        if url == OPENROUTER:
            errors = {401:'auth', 403:'auth', 402:'insufficient_quota', 404:'missing_model', 429:'rate_limited'}
            raise FilmPlanError(errors.get(exc.code, 'submission_uncertain' if exc.code >= 500 else 'provider_error')) from None
        raise FilmPlanError('invalid_output' if exc.code in (400,422) else 'planner_unavailable') from None
    except OSError:
        raise FilmPlanError('submission_uncertain' if key else 'planner_unavailable') from None
    try:
        result = json.loads(raw) if len(raw) <= MAX_RESULT else None
        if not isinstance(result, dict):
            raise ValueError()
    except (ValueError, UnicodeError):
        raise FilmPlanError('invalid_output') from None
    return result


def _inference(envelope, slate):
    # Cloudflare composes the storyboard prompt. The credential holder controls the destination,
    # billing cap, tool access and image URLs. An unexpected envelope never reaches OpenRouter.
    if not isinstance(envelope, dict) or envelope.get('model') != slate['model']:
        raise FilmPlanError('invalid_output')
    messages, fmt = envelope.get('messages'), envelope.get('response_format')
    if (not isinstance(fmt, dict) or fmt.get('type') != 'json_schema' or not isinstance(messages, list)
            or len(messages) != 2 or not all(isinstance(m, dict) for m in messages)
            or [m.get('role') for m in messages] != ['system','user']
            or not isinstance(messages[0].get('content'), str) or len(messages[0]['content']) > 18000):
        raise FilmPlanError('invalid_output')
    allowed = {p['image_url'] for p in slate['photos']}
    received = set()
    parts = messages[1].get('content')
    if not isinstance(parts, list) or not 1 <= len(parts) <= 2 * len(slate['photos']) + 3:
        raise FilmPlanError('invalid_output')
    clean = []
    for part in parts:
        if not isinstance(part, dict):
            raise FilmPlanError('invalid_output')
        if part.get('type') == 'text' and isinstance(part.get('text'), str) and len(part['text']) <= 56000:
            clean.append({'type':'text', 'text':part['text']})
        elif part.get('type') == 'image_url' and isinstance(part.get('image_url'), dict):
            url = part['image_url'].get('url')
            if not isinstance(url, str) or url not in allowed or url in received:
                raise FilmPlanError('invalid_output')
            received.add(url)
            clean.append({'type':'image_url', 'image_url':{'url':url, 'detail':'low'}})
        else:
            raise FilmPlanError('invalid_output')
    if received != allowed:
        raise FilmPlanError('invalid_output')
    return {'model': slate['model'], 'messages':[{'role':'system', 'content':messages[0]['content']},
            {'role':'user', 'content':clean}], 'response_format':fmt, 'max_tokens':3000,
            'provider':{'require_parameters': True, 'allow_fallbacks':False}}


def _copy(value, limit):
    if (not isinstance(value, str) or not 1 <= len(value.strip()) <= limit or not re.search('[A-Za-z]', value)
            or any(not char.isprintable() for char in value) or re.search(r'https?://|www\.|@|#|[\u3400-\u9fff]', value, re.I)):
        raise FilmPlanError('invalid_output')
    value = value.strip()
    if value.lower().strip(" .!?,") in {'wait for it', 'watch till the end', 'main character energy', 'who else can relate'}:
        raise FilmPlanError('invalid_output')
    return value


def _validate(raw, candidates, photos, model):
    if not isinstance(raw, dict):
        raise FilmPlanError('invalid_output')
    if raw.get('decision') == 'hold':
        raise FilmPlanError('no_fit')
    tid = raw.get('trend_id')
    trend = candidates.get(tid) if isinstance(tid, str) else None
    shots = raw.get('shots')
    if raw.get('decision') != 'render' or not trend or not isinstance(shots, list) or len(shots) != 3:
        raise FilmPlanError('invalid_output')
    chosen_ids = set()
    ordered = []
    for shot in shots:
        if not isinstance(shot, dict) or not isinstance(shot.get('photo_id'), str) or shot.get('framing') not in FRAMINGS:
            raise FilmPlanError('invalid_output')
        photo = photos.get(shot['photo_id'])
        if (not photo or any(photo[k] != trend[k] for k in ('material_group','cast_type')) or
                (trend['transformation'] != 'none' and photo.get('_agent_styles_ready') is not True)):
            raise FilmPlanError('invalid_output')
        chosen_ids.add(photo['id'])
        overlay = _copy(shot.get('overlay'), 68)
        if len(overlay.split()) > 9:
            raise FilmPlanError('invalid_output')
        ordered.append({'photo_id': photo['id'], 'framing': shot['framing'], 'overlay': overlay})
    caption = _copy(raw.get('caption'), 150)
    tags = raw.get('hashtags')
    if (len(chosen_ids) != 1 or len({s['overlay'].casefold() for s in ordered}) != 3 or
            caption.casefold() == ordered[0]['overlay'].casefold() or not isinstance(tags, list) or not 2 <= len(tags) <= 4 or
            any(not isinstance(h, str) or not re.fullmatch(r'#[A-Za-z0-9_]{1,45}', h) for h in tags) or
            len({h.lower() for h in tags}) != len(tags) or any(h.lower() in ('#fyp','#viral','#trending','#foryou') for h in tags)):
        raise FilmPlanError('invalid_output')
    return {'provider': 'openrouter', 'model': model, 'execution_host':'cloudflare', 'trend_id': tid, 'source_id': tid, 'source_url': trend['source_url'],
            'material_group': trend['material_group'], 'cast_type': trend['cast_type'],
            'photo_ids': list(dict.fromkeys(shot['photo_id'] for shot in ordered)), 'shots': ordered, 'hook': ordered[0]['overlay'],
            'caption': caption, 'hashtags': tags}


def plan_film(trends, photos, *, slot, prior_films=(), client=None, api_key=None, model=None):
    """Plan and validate on Cloudflare; only the single OpenRouter request carries a credential."""
    candidates, shortlist = _table(trends, photos, prior_films)
    model = model or os.environ.get('OPENROUTER_TEXT_MODEL') or DEFAULT_ROUTER_MODEL
    if not isinstance(model, str) or not model.startswith('openai/') or not TEXT_MODEL.fullmatch(model[7:]):
        raise FilmPlanError('missing_model')
    key = os.environ.get('OPENROUTER_API_KEY', '') if api_key is None else api_key
    if not CreativeService._valid_key(key):
        raise FilmPlanError('missing_key')
    send = client or _post
    slate = _slate(model, slot, candidates, shortlist)
    envelope = send(WORKER + 'request', slate)
    reply = send(OPENROUTER, _inference(envelope, slate), key)
    try:
        choice = reply['choices'][0]
        raw = choice['message']['content']
        if choice.get('finish_reason') != 'stop' or not isinstance(raw, str) or not 1 <= len(raw) <= 12000:
            raise ValueError()
    except (KeyError, IndexError, TypeError, ValueError):
        raise FilmPlanError('invalid_output') from None
    accepted = send(WORKER + 'accept', {'slate': slate, 'response': raw})
    return _validate(accepted, candidates, shortlist, model)
