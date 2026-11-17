#!/usr/bin/env python3
"""GitHub-only English copy from actual completed video frames, with real sources.

OPENAI_API_KEY is injected by a GitHub Secret. No browser key, local key file,
automatic POST retry, or fallback model is used. Two Responses calls are made:
current US source research, then a structured caption/hashtag draft.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from creative_service import CreativeError, CreativeService, TEXT_MODEL, _image

DEFAULT_MODEL = 'gpt-5.4-mini'
MAX_VIDEO = 90 * 1024 * 1024
MESSAGES = {
    'missing_key': 'GitHub Secret OPENAI_API_KEY is missing.',
    'missing_model': 'The configured OpenAI model is missing or unavailable; no model was substituted.',
    'insufficient_quota': 'OpenAI credits or a project spending limit blocked this request. Check billing before requesting again.',
    'rate_limited': 'OpenAI limited this request. No automatic retry was made.',
    'auth': 'OpenAI rejected the configured GitHub credential or account permission.',
    'invalid_output': 'The copy result did not pass validation. No automatic regeneration was made.',
    'invalid_video': 'A verified completed video and readable sampled frames are required.',
    'cloud_only': 'This operation requires an explicitly requested GitHub cloud run.',
    'retry_blocked': 'This workflow attempt cannot start another paid copy request.',
    'submission_uncertain': 'The OpenAI response could not be confirmed; check usage before requesting again.',
    'provider_error': 'OpenAI did not complete this request. No automatic retry was made.',
}


class PipelineCopyError(Exception):
    def __init__(self, code, status=None):
        self.code = code if code in MESSAGES else 'invalid_output'
        self.status = status if type(status) is int else None
        self.message = MESSAGES[self.code]
        super().__init__(self.message)


def _child_env(env):
    return {key: value for key, value in env.items() if isinstance(value, str)
            and not any(word in key.upper() for word in ('TOKEN', 'SECRET', 'API_KEY'))
            and key not in ('GENERATION_INPUTS', 'CREATIVE_INPUTS', 'PIPELINE_INPUTS')}


def _video_path(item, site_dir):
    if not isinstance(item, dict):
        raise PipelineCopyError('invalid_video')
    ident, relative, expected = item.get('id'), item.get('video_path'), item.get('sha256')
    if (not isinstance(ident, str) or not re.fullmatch(r'video-(?:[a-f0-9]{24}|[a-f0-9]{32})', ident)
            or relative != 'generated/' + ident + '.mp4'
            or not isinstance(expected, str) or not re.fullmatch(r'[a-f0-9]{64}', expected)
            or item.get('status', 'completed') != 'completed'):
        raise PipelineCopyError('invalid_video')
    site = Path(site_dir).resolve(strict=True)
    path = site / relative
    if ((site / 'generated').is_symlink() or path.is_symlink() or not path.is_file()
            or path.resolve().parent != (site / 'generated').resolve()
            or not 24 <= path.stat().st_size <= MAX_VIDEO):
        raise PipelineCopyError('invalid_video')
    raw = path.read_bytes()
    if raw[4:8] != b'ftyp' or hashlib.sha256(raw).hexdigest() != expected:
        raise PipelineCopyError('invalid_video')
    return path


def extract_frames(path, env):
    """Read four chronological frames from this MP4, not from its reference."""
    child_env = _child_env(env)
    result = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
        '-show_entries', 'stream=width,height:format=duration,format_name', '-of', 'json', str(path)],
        check=True, capture_output=True, text=True, timeout=30, env=child_env)
    metadata = json.loads(result.stdout)
    duration = float(metadata['format']['duration'])
    stream = metadata['streams'][0]
    if (not math.isfinite(duration) or not .2 <= duration <= 120
            or 'mp4' not in metadata['format']['format_name']
            or not 64 <= stream['width'] <= 4096 or not 64 <= stream['height'] <= 4096):
        raise PipelineCopyError('invalid_video')
    frames = []
    for t in (.05, duration / 3, duration * 2 / 3, max(.05, duration - .15)):
        result = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-ss', '%.3f' % t,
            '-i', str(path), '-frames:v', '1', '-vf',
            'scale=768:768:force_original_aspect_ratio=decrease', '-f', 'image2pipe',
            '-vcodec', 'mjpeg', '-q:v', '3', 'pipe:1'],
            check=True, capture_output=True, timeout=30, env=child_env)
        raw = result.stdout
        if not 4 <= len(raw) <= 5 * 1024 * 1024 or not raw.startswith(b'\xff\xd8\xff') or not raw.endswith(b'\xff\xd9'):
            raise PipelineCopyError('invalid_video')
        frames.append('data:image/jpeg;base64,' + base64.b64encode(raw).decode('ascii'))
    return frames


def _url(value):
    try:
        parsed = urlsplit(value)
        return (isinstance(value, str) and len(value) <= 4096 and parsed.scheme == 'https'
                and parsed.hostname and not parsed.username and not parsed.password
                and not parsed.fragment and parsed.port in (None, 443))
    except (TypeError, ValueError):
        return False


def _text(value, maximum):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum and not any(ord(c) < 32 and c not in '\n\t' for c in value)


def validate_copy(job, key):
    """Export only intended copy and sources returned by the actual search."""
    try:
        if not isinstance(job, dict) or job.get('status') != 'completed':
            code = job.get('error_code') if isinstance(job, dict) else None
            raise PipelineCopyError(code or ('submission_uncertain' if isinstance(job, dict) and job.get('status') in ('running', 'submission_uncertain') else 'invalid_output'))
        draft, sources = job['draft'], job.get('sources', [])
        captions, tags, note = draft['captions'], draft['hashtags'], draft['trend_note']
        if (not isinstance(captions, list) or len(captions) != 3 or not all(_text(s, 1800) for s in captions)
                or any(not re.search('[A-Za-z]', s) or re.search('[\u3400-\u9fff]', s) for s in captions)
                or not isinstance(tags, list) or not 3 <= len(tags) <= 5
                or not isinstance(note, str) or len(note) > 3500
                or not isinstance(sources, list) or len(sources) > 20):
            raise ValueError()
        exported_sources = []
        for source in sources:
            if not isinstance(source, dict) or not _url(source.get('url')) or not _text(source.get('title'), 300):
                raise ValueError()
            observed = source['observed_at']
            if not isinstance(observed, str) or datetime.fromisoformat(observed.replace('Z', '+00:00')).tzinfo is None:
                raise ValueError()
            exported_sources.append({name: source[name] for name in ('url', 'title', 'observed_at')})
        allowed = {source['url']: source for source in exported_sources}
        hashtags, seen = [], set()
        for tag in tags:
            if (not isinstance(tag, dict) or not isinstance(tag.get('tag'), str)
                    or not re.fullmatch(r'#[A-Za-z0-9_]{1,79}', tag['tag'])
                    or tag['tag'].lower() in seen or not _text(tag.get('reason'), 1000)
                    or tag.get('category') not in ('content', 'brand', 'trend')):
                raise ValueError()
            row = {name: tag.get(name) for name in ('tag', 'reason', 'category', 'source_url', 'observed_at')}
            if row['category'] == 'trend':
                if row['source_url'] not in allowed or row['observed_at'] != allowed[row['source_url']]['observed_at']:
                    raise ValueError()
            else:
                row.update(source_url=None, observed_at=None)
            hashtags.append(row)
            seen.add(tag['tag'].lower())
        caption = captions[0].strip() + '\n\n' + ' '.join(tag['tag'] for tag in hashtags)
        if len(caption) > 2200:
            raise ValueError()
        result = {'caption': captions[0].strip(), 'publication_text': caption, 'captions': captions, 'hashtags': hashtags,
                  'sources': exported_sources, 'trend_note': note,
                  'caption_basis': 'completed_video_frames'}
        if key and key in json.dumps(result, ensure_ascii=False):
            raise ValueError()
        return result
    except PipelineCopyError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        raise PipelineCopyError('invalid_output') from None


def generate_copy(item, env=None, *, site_dir=None, state_dir=None,
                  request_fn=None, frame_extractor=None, default_model=DEFAULT_MODEL):
    """Return validated English copy. Call only after persisting a completed MP4.

    env defaults to the GitHub process environment; tests inject a fake mapping
    and mocked transport. state_dir is private scratch, never a website artifact.
    """
    env = os.environ if env is None else env
    if env.get('GITHUB_ACTIONS') != 'true':
        raise PipelineCopyError('cloud_only')
    key = env.get('OPENAI_API_KEY', '')
    if not isinstance(key, str) or not CreativeService._valid_key(key.strip()):
        raise PipelineCopyError('missing_key')
    key = key.strip()
    selected = env.get('OPENAI_MODEL')
    model = (selected.strip() if isinstance(selected, str) else selected) or default_model
    if not isinstance(model, str) or not TEXT_MODEL.fullmatch(model.strip()):
        raise PipelineCopyError('missing_model')
    model = model.strip()
    try:
        path = _video_path(item, site_dir or ROOT / 'site')
        frames = (frame_extractor or extract_frames)(path, env)
        if not isinstance(frames, list) or len(frames) != 4:
            raise PipelineCopyError('invalid_video')
        frames = [_image(frame) for frame in frames]
        if hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
            raise PipelineCopyError('invalid_video')
    except PipelineCopyError:
        raise
    except Exception:
        raise PipelineCopyError('invalid_video') from None
    character, title = item.get('character') or '', item.get('title') or ''
    if not isinstance(character, str) or len(character) > 200 or not isinstance(title, str) or len(title) > 500:
        raise PipelineCopyError('invalid_output')
    state = Path(state_dir) if state_dir else Path(env.get('RUNNER_TEMP') or tempfile.gettempdir()) / 'pipeline-copy-state'
    run_id = env.get('GITHUB_RUN_ID', 'request')
    if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', run_id):
        raise PipelineCopyError('invalid_output')
    token = 'pipeline-copy-' + item['sha256'][:24] + '-' + run_id
    try:
        service = CreativeService(state, request_fn=request_fn, api_key=key)
        prior = next((job for job in service.jobs()['jobs'] if job.get('idempotency_key') == token), None)
        if env.get('GITHUB_RUN_ATTEMPT', '1') != '1' and prior is None:
            raise PipelineCopyError('retry_blocked')
        job = service.generate_draft({'idempotency_key': token, 'model': model, 'character': character,
            'description': 'Four chronological frames sampled from the actual completed generated video. '
                'Describe only visible content; do not assume that it perfectly copies any source dance. '
                'No audio was analyzed. Public label (unverified context): ' + title,
            'trend_context': 'US English audience. Completed generated video; hashtags must fit its visible content. '
                'The title and character name are context, not evidence of current popularity.',
            'frames': frames, 'search_trends': True, 'consent': True})
        return validate_copy(job, key)
    except PipelineCopyError:
        raise
    except CreativeError as exc:
        code = exc.code if exc.code in MESSAGES else ('auth' if exc.status in (401, 403) else 'rate_limited' if exc.status == 429 else 'invalid_output')
        raise PipelineCopyError(code, exc.status) from None
    except Exception:
        raise PipelineCopyError('invalid_output') from None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--item-id', required=True)
    parser.add_argument('--site', default=str(ROOT / 'site'))
    parser.add_argument('--state-dir')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        catalog = json.loads((Path(args.site) / 'data' / 'creations.json').read_text())
        item = next(item for item in catalog['items'] if item.get('id') == args.item_id)
        result = generate_copy(item, site_dir=args.site, state_dir=args.state_dir)
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        print('English copy completed from actual generated-video frames.')
        return 0
    except Exception as exc:
        code = exc.code if isinstance(exc, PipelineCopyError) else 'invalid_output'
        print(json.dumps({'status': 'copy_failed', 'code': code, 'message': MESSAGES[code]}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
