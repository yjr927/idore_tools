#!/usr/bin/env python3
"""Isolated one-shot diagnostic of the unchanged approved KAI + clay runner."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from urllib.error import HTTPError
from urllib.request import Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import run_clay_video_generation as clay
from video_service import _safe_prompt

TASK = 'kai-openrouter-check-20261124'
REPO = 'yjr927/idore_tools'
BRANCH = 'codex/' + TASK
RESERVATION = TASK + '-paid-reservation'
SOURCE_ID = '7706633304702848276'
OLD_CLAY_URL = 'https://raw.githubusercontent.com/yjr927/idore_tools/cce63dabd044b1a8131fc49158479bffa31aaee5/reference-assets/clay-bizzyboom.mp4'
OLD_CLAY_SHA = '56c856b3d1f4187573caafdb3c71126aa6e95c2a9eafc52c9df8581fdf2219f2'
DEADLINE = datetime(2026, 11, 26, tzinfo=timezone.utc)
MAX_JSON = 1024 * 1024


class Stopped(Exception):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def load(raw):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_JSON:
        raise Stopped('invalid_json')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise Stopped('invalid_json')
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(Stopped('invalid_json')))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise Stopped('invalid_json') from None


def context(now=None):
    if (now or datetime.now(timezone.utc)) >= DEADLINE:
        raise Stopped('start_deadline_passed')
    e = os.environ
    if (e.get('GITHUB_ACTIONS') != 'true' or e.get('GITHUB_REPOSITORY') != REPO
            or e.get('GITHUB_REF') != 'refs/heads/' + BRANCH or e.get('GITHUB_EVENT_NAME') != 'push'
            or e.get('GITHUB_RUN_ATTEMPT') != '1' or not re.fullmatch(r'[0-9]{1,22}', e.get('GITHUB_RUN_ID', ''))):
        raise Stopped('invalid_execution_context')
    path = Path(e.get('GITHUB_EVENT_PATH', ''))
    if path.is_symlink() or not path.is_file() or not 1 <= path.stat().st_size <= MAX_JSON:
        raise Stopped('invalid_execution_context')
    event = load(path.read_bytes())
    if (not isinstance(event, dict) or event.get('created') is not False or event.get('deleted') is not False
            or event.get('ref') != 'refs/heads/' + BRANCH):
        raise Stopped('invalid_execution_context')
    if not clay.VideoService._valid_key(e.get('OPENROUTER_API_KEY', '').strip()):
        raise Stopped('missing_openrouter_configuration')
    return e['GITHUB_RUN_ID']


def artifacts():
    token = os.environ.get('GITHUB_TOKEN', '')
    if not token:
        raise Stopped('missing_github_configuration')
    url = 'https://api.github.com/repos/' + REPO + '/actions/artifacts?name=' + RESERVATION + '&per_page=100'
    req = Request(url, headers={'Authorization':'Bearer ' + token, 'Accept':'application/vnd.github+json',
        'X-GitHub-Api-Version':'2022-11-28', 'User-Agent':'kai-baseline-diagnostic/1.0'})
    try:
        with build_opener(clay.NoRedirect).open(req, timeout=30) as response:
            if response.status != 200 or response.geturl() != url:
                raise Stopped('github_read_failed')
            value = load(response.read(MAX_JSON + 1))
    except HTTPError as exc:
        exc.close()
        raise Stopped('github_read_failed') from None
    except Stopped:
        raise
    except Exception:
        raise Stopped('github_read_failed') from None
    if (not isinstance(value, dict) or type(value.get('total_count')) is not int
            or not isinstance(value.get('artifacts'), list) or value['total_count'] != len(value['artifacts'])
            or any(not isinstance(row, dict) or row.get('name') != RESERVATION for row in value['artifacts'])):
        raise Stopped('invalid_reservation_response')
    return value['artifacts']


def verify_reservation(rows, run_id=None):
    if run_id is None:
        if rows:
            raise Stopped('prior_paid_reservation_exists')
    elif (len(rows) != 1 or rows[0].get('expired') is not False
          or not isinstance(rows[0].get('workflow_run'), dict)
          or str(rows[0]['workflow_run'].get('id')) != run_id):
        raise Stopped('current_paid_reservation_unconfirmed')


def clay_config(path, provenance_path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not 1 <= path.stat().st_size <= 8192:
        raise Stopped('new_reviewed_clay_config_required')
    value = load(path.read_bytes())
    expected = dict(schema_version=1, task_id=TASK, source_id=SOURCE_ID, status='reviewed',
                    model=clay.MODEL, character='KAI', duration=6)
    if (not isinstance(value, dict) or set(value) != set(expected) | {'media_url','sha256','rebuild_run_id','rebuilt_at_utc','provenance_sha256'}
            or any(type(value.get(k)) is not type(v) or value.get(k) != v for k,v in expected.items())
            or not isinstance(value.get('sha256'), str) or not re.fullmatch(r'[a-f0-9]{64}', value['sha256'])):
        raise Stopped('new_reviewed_clay_config_invalid')
    try:
        clay.approved_clay_url(value['media_url'])
    except Exception:
        raise Stopped('new_reviewed_clay_config_invalid') from None
    if value['media_url'] == OLD_CLAY_URL:
        raise Stopped('old_clay_reference_refused')
    if (not isinstance(value['rebuild_run_id'], str) or not re.fullmatch(r'[0-9]{1,22}', value['rebuild_run_id'])
            or not isinstance(value['provenance_sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', value['provenance_sha256'])):
        raise Stopped('fresh_rebuild_provenance_invalid')
    try:
        rebuilt = datetime.fromisoformat(value['rebuilt_at_utc'].replace('Z','+00:00'))
        if (rebuilt.utcoffset().total_seconds() != 0
                or not datetime(2026,11,24,tzinfo=timezone.utc) <= rebuilt < DEADLINE):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise Stopped('fresh_rebuild_provenance_invalid') from None
    provenance_path = Path(provenance_path)
    if provenance_path.is_symlink() or not provenance_path.is_file() or not 1 <= provenance_path.stat().st_size <= 64*1024:
        raise Stopped('fresh_rebuild_provenance_required')
    raw = provenance_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != value['provenance_sha256']:
        raise Stopped('fresh_rebuild_provenance_hash_mismatch')
    manifest = load(raw)
    checks = dict(schema_version=1, task_id=TASK, source_id=SOURCE_ID, repository=REPO,
        workflow_run_id=value['rebuild_run_id'], workflow_run_attempt='1', rebuilt_at_utc=value['rebuilt_at_utc'],
        status='freshly_rebuilt_for_review', construction='fresh_original_download_to_pose_extraction_to_blender_clay_render',
        reused_existing_clay_video=False, source_pixels_in_artifact=False, paid_model_calls=0)
    if (not isinstance(manifest, dict)
            or any(type(manifest.get(k)) is not type(v) or manifest.get(k) != v for k,v in checks.items())
            or not isinstance(manifest.get('artifact_files'), dict)
            or not isinstance(manifest['artifact_files'].get('clay-bizzyboom.mp4'), dict)
            or manifest['artifact_files']['clay-bizzyboom.mp4'].get('sha256') != value['sha256']):
        raise Stopped('fresh_rebuild_provenance_invalid')
    return value


def baseline(run_id, config):
    # Exactly the original runner's submitted creative request; no rewrite or fallback.
    return {'idempotency_key':'github-clay-' + run_id, 'model':clay.MODEL, 'mode':'references',
        'prompt':clay.PROMPT, 'duration':6, 'resolution':'720p', 'aspect_ratio':'9:16',
        'generate_audio':False, 'consent':True, 'references':[
            {'type':'image_url','url':clay.IMAGE_URL}, {'type':'video_url','url':config['media_url']}]}


def receipt(run_id, config):
    document = baseline(run_id, config)
    document.pop('idempotency_key')
    return dict(schema_version=1, task_id=TASK, run_id=run_id,
                request_sha256=hashlib.sha256(canonical(document)).hexdigest(), clay_sha256=config['sha256'],
                config_sha256=hashlib.sha256(canonical(config)).hexdigest(),
                status='reserved_do_not_retry')


def write_exclusive(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as target:
        target.write(canonical(value))
        target.flush()
        os.fsync(target.fileno())


def redacted_message(value, secret=''):
    if not isinstance(value, str):
        return ''
    text = _safe_prompt(value[:8192], secret)
    if secret:
        text = text.replace(secret, '[redacted]')
    text = text.replace(clay.PROMPT, '[prompt redacted]')
    for match in reversed(SequenceMatcher(None, clay.PROMPT, text).get_matching_blocks()):
        if match.size >= 24:
            text = text[:match.b] + '[prompt redacted]' + text[match.b + match.size:]
    text = re.sub(r'\bsk-(?:or-)?[A-Za-z0-9_-]{8,}', '[redacted]', text, flags=re.I)
    text = re.sub(r'data:[^\s"\x27]+', '[media redacted]', text, flags=re.I)
    text = re.sub(r'https?://[^\s"\x27<>]+', '[url redacted]', text, flags=re.I)
    text = re.sub(r'(?:Bearer|Basic)\s+[^\s,;]+', '[authorization redacted]', text, flags=re.I)
    # Never retain echoed request objects or prompt fragments in a public diagnostic.
    text = re.split(r'(?i)(?:["\x27]?prompt["\x27]?\s*[:=]|input_references|frame_images|authorization\s*[:=]|api[_ -]?key\s*[:=])', text)[0]
    text = re.sub(r'[A-Za-z0-9+/=_-]{100,}', '[opaque data redacted]', text)
    text = re.sub(r'[\x00-\x1f\x7f]+', ' ', text)
    return text.strip()[:800]


class DiagnosticTransport(clay.ObservedTransport):
    def __init__(self):
        super().__init__()
        self.error_message = ''

    def observe(self, raw, status):
        super().observe(raw, status)
        if not isinstance(raw, bytes) or len(raw) > clay.MAX_ERROR_BYTES:
            return
        try:
            root = json.loads(raw)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return
        if status < 400 and (not isinstance(root, dict) or not root.get('error')):
            return
        if isinstance(root, dict) and isinstance(root.get('error'), str) and not root['error'].lstrip().startswith('{'):
            root = {'error':{'message':root['error']}}
        pending = [(root, 0)]
        while pending:
            node, depth = pending.pop()
            if depth > 6 or not isinstance(node, dict):
                continue
            for field in ('code','param'):
                value = node.get(field)
                if (isinstance(value, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,120}', value)
                        and not value.lower().startswith(('sk-','bearer')) and 'key' not in value.lower()
                        and not (self._secret and self._secret in value)):
                    self.details['provider_' + field] = value
            if isinstance(node.get('message'), str):
                message = redacted_message(node['message'], self._secret)
                if message:
                    self.error_message = message
            for name in ('error', 'metadata', 'raw', 'response'):
                child = node.get(name)
                if isinstance(child, str) and child.lstrip().startswith('{'):
                    try:
                        child = json.loads(child)
                    except (ValueError, TypeError, RecursionError):
                        continue
                if isinstance(child, dict):
                    pending.append((child, depth + 1))


def safe_summary(output, transport, return_code):
    result = dict(schema_version=1, task_id=TASK, status='stopped',
                  paid_post_attempted=bool(transport.paid_posts), baseline_runner_unchanged=True)
    path = Path(output) / 'clay-generation-summary.json'
    if path.is_file() and not path.is_symlink() and path.stat().st_size <= clay.MAX_ERROR_BYTES:
        try:
            prior = load(path.read_bytes())
            result.update(clay.safe_task(prior, transport._secret))
            usage = clay.numeric_usage(prior.get('usage'))
            if usage:
                result['usage'] = usage
        except Exception:
            pass
    if transport.last_error_status:
        result['provider_http_status'] = transport.last_error_status
    result.update(transport.details)
    if transport.diagnosis:
        result.update({k:v for k,v in transport.diagnosis.items() if k in ('reason','provider_code','provider_param')})
    if transport.error_message:
        result['error_message'] = transport.error_message
    if result['status'] == 'stopped' and transport.paid_posts:
        result['status'] = 'submission_uncertain'
    result['runner_return_code'] = return_code
    return result


def run_baseline(output, config):
    if datetime.now(timezone.utc) >= DEADLINE:
        raise Stopped('start_deadline_passed')
    transport = DiagnosticTransport()
    original = clay.ObservedTransport
    names = {'CLAY_REFERENCE_URL':config['media_url'], 'CLAY_REFERENCE_SHA256':config['sha256'],
        'CLAY_ALLOW_PAID':'true', 'CLAY_REVIEWED':'true', 'VIDEO_OUTPUT_DIR':str(output)}
    previous = {k:os.environ.get(k) for k in names}
    code = 1
    try:
        os.environ.update(names)
        clay.ObservedTransport = lambda: transport
        try:
            code = clay.main()
        except Exception:
            # Diagnostic evidence must survive an unexpected runner exception.
            code = 1
    finally:
        clay.ObservedTransport = original
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    write_exclusive(Path(output) / 'diagnostic.json', safe_summary(output, transport, code))
    return code


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['prepare','run'])
    parser.add_argument('--receipt', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--provenance', required=True)
    args = parser.parse_args(argv)
    try:
        run_id = context()
        config = clay_config(args.config, args.provenance)
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
            raise Stopped('media_verifier_unavailable')
        if args.phase == 'prepare':
            verify_reservation(artifacts())
            service = clay.VideoService(Path(args.output) / 'preflight-state')
            account = service._network('/key')
            if not isinstance(account.get('data'), dict):
                raise Stopped('api_configuration_unverified')
            _, _, payload, _ = service._request(baseline(run_id, config))
            service._capabilities(payload)
            write_exclusive(args.receipt, receipt(run_id, config))
            print('Baseline diagnostic ready; no generation submitted.')
            return 0
        file = Path(args.receipt)
        if file.is_symlink() or not file.is_file() or file.stat().st_size > 8192 or load(file.read_bytes()) != receipt(run_id, config):
            raise Stopped('reservation_receipt_mismatch')
        verify_reservation(artifacts(), run_id)
        return run_baseline(Path(args.output), config)
    except Exception as exc:
        reason = str(exc) if isinstance(exc, Stopped) else 'diagnostic_execution_stopped'
        print('Baseline diagnostic stopped: ' + reason, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
