#!/usr/bin/env python3
"""One explicitly authorized film; encrypted brief, one reservation, one POST.

Only keys/preflight/generate are Actions entry points. No clear brief or provider
response is printed or included in artifacts. Unexpected errors fail closed.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from video_service import VideoService, VideoError, TERMINAL

TASK = 'idore-kai-concept-20261124'
REPO = 'yjr927/idore_tools'
BRANCH = 'codex/idore-kai-concept-20261124'
MODEL = 'bytedance/seedance-2.5'
DEADLINE = datetime(2026, 11, 26, tzinfo=timezone.utc)
REQUEST_PATH = 'requests/' + TASK + '.enc.json'
RESERVATION = TASK + '-paid-reservation'
IMAGE_URL = 'https://raw.githubusercontent.com/yjr927/idore_tools/58853770e09acaa4d0a7ffd92eb68c21590365ef/site/characters/kai/kai%E6%97%A5%E5%B8%B8.png'
IMAGE_SHA = 'd038bc09a24579ace20d028174a0932af300cd218f827a780f25eaf1c91b6285'
MAX_ENVELOPE = 96 * 1024
META = {'schema_version', 'task_id', 'repository', 'branch', 'key_run_id', 'public_key_sha256'}
KEY_FIELDS = META | {'salt', 'iv', 'ciphertext', 'tag'}
ENVELOPE_FIELDS = META | {'encrypted_key', 'iv', 'ciphertext', 'tag'}
READY_FIELDS = {'schema_version', 'task_id', 'run_id', 'status', 'request_sha256'}
BODY_FIELDS = {'idempotency_key', 'model', 'prompt', 'mode', 'duration', 'resolution', 'aspect_ratio', 'generate_audio', 'references', 'consent'}


class Stopped(Exception):
    """Fixed public codes only; never put upstream text in this exception."""


def stop(code):
    raise Stopped(code)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def b64(raw):
    return base64.b64encode(raw).decode('ascii')


def unb64(value, minimum, maximum):
    if not isinstance(value, str) or len(value) > ((maximum + 2) // 3) * 4:
        stop('invalid_envelope')
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        stop('invalid_envelope')
    if not minimum <= len(raw) <= maximum:
        stop('invalid_envelope')
    return raw


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            stop('invalid_json')
        result[key] = value
    return result


def parse(raw, maximum=MAX_ENVELOPE):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= maximum:
        stop('invalid_json')
    try:
        return json.loads(raw, object_pairs_hook=unique_pairs,
                          parse_constant=lambda _: stop('invalid_json'))
    except (UnicodeError, ValueError, TypeError):
        stop('invalid_json')


def read(path, maximum=MAX_ENVELOPE):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= maximum:
        stop('invalid_file')
    return path.read_bytes()


def write(path, raw):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as target:
        target.write(raw)
        target.flush()
        os.fsync(target.fileno())


def context(now=None):
    now = now or datetime.now(timezone.utc)
    if now >= DEADLINE:
        stop('start_deadline_passed')
    env = os.environ
    if (env.get('GITHUB_REPOSITORY') != REPO or env.get('GITHUB_REF') != 'refs/heads/' + BRANCH
            or env.get('GITHUB_EVENT_NAME') != 'push' or env.get('GITHUB_RUN_ATTEMPT') != '1'
            or not re.fullmatch(r'[0-9]{1,22}', env.get('GITHUB_RUN_ID', ''))):
        stop('invalid_execution_context')
    event = parse(read(env.get('GITHUB_EVENT_PATH', ''), 1024 * 1024), 1024 * 1024)
    if (not isinstance(event, dict) or event.get('created') is not False
            or event.get('deleted') is not False or event.get('ref') != 'refs/heads/' + BRANCH):
        stop('invalid_execution_context')
    key = env.get('OPENROUTER_API_KEY', '')
    if not VideoService._valid_key(key):
        stop('missing_api_configuration')
    return env['GITHUB_RUN_ID'], key


def metadata(run_id, public_pem):
    public = serialization.load_pem_public_key(public_pem)
    if not isinstance(public, rsa.RSAPublicKey) or public.key_size != 3072 or public.public_numbers().e != 65537:
        stop('invalid_public_key')
    der = public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return dict(schema_version=1, task_id=TASK, repository=REPO, branch=BRANCH,
                key_run_id=run_id, public_key_sha256=hashlib.sha256(der).hexdigest())


def verify_meta(value, expected, fields):
    if not isinstance(value, dict) or set(value) != fields:
        stop('invalid_envelope')
    if any(type(value.get(k)) is not type(v) or value.get(k) != v for k, v in expected.items()):
        stop('envelope_context_mismatch')


def private_kek(secret, salt, run_id):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                info=('idore-private-key-v1:' + TASK + ':' + run_id).encode('ascii')).derive(secret.encode('utf-8'))


def make_keys(directory, run_id, secret):
    private = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    public = private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    meta = metadata(run_id, public)
    salt, iv = os.urandom(32), os.urandom(12)
    raw = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    cipher = AESGCM(private_kek(secret, salt, run_id)).encrypt(iv, raw, canonical(meta))
    bundle = dict(meta, salt=b64(salt), iv=b64(iv), ciphertext=b64(cipher[:-16]), tag=b64(cipher[-16:]))
    write(Path(directory) / 'public-key.pem', public)
    write(Path(directory) / 'encrypted-private-key.json', canonical(bundle))
    return public, meta


def decrypt_request(bundle_dir, envelope_raw, run_id, secret):
    public_pem = read(Path(bundle_dir) / 'public-key.pem', 8192)
    meta = metadata(run_id, public_pem)
    bundle = parse(read(Path(bundle_dir) / 'encrypted-private-key.json', 16384), 16384)
    envelope = parse(envelope_raw)
    verify_meta(bundle, meta, KEY_FIELDS)
    verify_meta(envelope, meta, ENVELOPE_FIELDS)
    try:
        raw = AESGCM(private_kek(secret, unb64(bundle['salt'], 32, 32), run_id)).decrypt(
            unb64(bundle['iv'], 12, 12), unb64(bundle['ciphertext'], 1, 8192) + unb64(bundle['tag'], 16, 16), canonical(meta))
        private = serialization.load_pem_private_key(raw, password=None)
        if (not isinstance(private, rsa.RSAPrivateKey) or private.key_size != 3072
                or private.public_key().public_numbers() != serialization.load_pem_public_key(public_pem).public_numbers()):
            stop('invalid_private_key')
        aes = private.decrypt(unb64(envelope['encrypted_key'], 384, 384), padding.OAEP(
            mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(),
            label=('idore-request-v1:' + TASK + ':' + run_id).encode('ascii')))
        if len(aes) != 32:
            stop('invalid_envelope')
        clear = AESGCM(aes).decrypt(unb64(envelope['iv'], 12, 12),
            unb64(envelope['ciphertext'], 1, 64 * 1024) + unb64(envelope['tag'], 16, 16), canonical(meta))
        return validate_document(parse(clear, 64 * 1024))
    except Stopped:
        raise
    except Exception:
        stop('envelope_authentication_failed')


def validate_document(document):
    if (not isinstance(document, dict) or set(document) != {'task_id', 'image_sha256', 'request'}
            or document['task_id'] != TASK or document['image_sha256'] != IMAGE_SHA):
        stop('invalid_request')
    body = document['request']
    expected = dict(idempotency_key=TASK, model=MODEL, mode='references', duration=25,
                    resolution='720p', aspect_ratio='9:16', generate_audio=True, consent=True,
                    references=[{'type': 'image_url', 'url': IMAGE_URL}])
    if not isinstance(body, dict) or set(body) != BODY_FIELDS:
        stop('invalid_request')
    if any(type(body.get(k)) is not type(v) or body.get(k) != v for k, v in expected.items()):
        stop('unauthorized_request_parameters')
    prompt = body.get('prompt')
    if (not isinstance(prompt, str) or not 100 <= len(prompt.strip()) <= 12000
            or any(ord(c) < 32 and c not in '\n\r\t' for c in prompt)
            or not all(phrase.lower() in prompt.lower() for phrase in ('Kai', 'idore', '25', '9:16'))
            or 'sk-or-' in prompt or 'http://' in prompt or 'https://' in prompt):
        stop('invalid_prompt')
    return body


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get(url, *, token='', maximum=1024 * 1024):
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'idore-one-film/1.0'}
    if token:
        if not url.startswith('https://api.github.com/repos/' + REPO + '/'):
            stop('invalid_api_endpoint')
        headers['Authorization'] = 'Bearer ' + token
        headers['X-GitHub-Api-Version'] = '2022-11-28'
    try:
        with build_opener(NoRedirect).open(Request(url, headers=headers), timeout=30) as response:
            raw = response.read(maximum + 1)
            if response.geturl() != url or len(raw) > maximum:
                stop('invalid_network_response')
            return response.status, raw
    except HTTPError as exc:
        status = exc.code
        exc.close()
        return status, b''
    except Stopped:
        raise
    except Exception:
        stop('network_unavailable')


def github(path):
    token = os.environ.get('GITHUB_TOKEN', '')
    if not token or not path.startswith('/'):
        stop('missing_github_configuration')
    status, raw = get('https://api.github.com/repos/' + REPO + path, token=token)
    if status != 200:
        stop('github_read_failed')
    return parse(raw, 1024 * 1024)


def reservations(current_run=None):
    document = github('/actions/artifacts?name=' + RESERVATION + '&per_page=100')
    if (not isinstance(document, dict) or type(document.get('total_count')) is not int
            or not isinstance(document.get('artifacts'), list)):
        stop('invalid_reservation_response')
    rows = document['artifacts']
    if document['total_count'] != len(rows) or any(not isinstance(row, dict) or row.get('name') != RESERVATION for row in rows):
        stop('invalid_reservation_response')
    if current_run is None:
        if rows:
            stop('prior_paid_reservation_exists')
    elif (len(rows) != 1 or rows[0].get('expired') is not False
          or str((rows[0].get('workflow_run') or {}).get('id')) != current_run):
        stop('paid_reservation_not_confirmed')


def wait_envelope(path):
    token = os.environ.get('GITHUB_TOKEN', '')
    if not token:
        stop('missing_github_configuration')
    url = ('https://api.github.com/repos/' + REPO + '/contents/' + REQUEST_PATH
           + '?ref=' + BRANCH.replace('/', '%2F'))
    deadline = time.monotonic() + 15 * 60
    while time.monotonic() < deadline:
        if datetime.now(timezone.utc) >= DEADLINE:
            stop('start_deadline_passed')
        status, raw = get(url, token=token, maximum=2 * MAX_ENVELOPE)
        if status == 200:
            value = parse(raw, 2 * MAX_ENVELOPE)
            if (not isinstance(value, dict) or value.get('type') != 'file' or value.get('path') != REQUEST_PATH
                    or value.get('encoding') != 'base64' or type(value.get('size')) is not int
                    or not 1 <= value['size'] <= MAX_ENVELOPE or not isinstance(value.get('content'), str)):
                stop('invalid_envelope_download')
            encoded = value['content'].replace('\n', '')
            result = unb64(encoded, 1, MAX_ENVELOPE)
            if len(result) != value['size']:
                stop('invalid_envelope_download')
            write(path, result)
            return
        if status != 404:
            stop('github_read_failed')
        time.sleep(10)
    stop('private_request_timeout')


class OneFilmService(VideoService):
    def _request(self, body):
        ident, mode, payload, _ = super()._request(body)
        # Provider options are scoped by the official routing contract. req_key
        # is deliberately omitted: its idempotency semantics are undocumented.
        payload['provider']['options'] = {'seed': {'watermark': False}, 'byteplus': {'watermark': False}}
        digest = hashlib.sha256(canonical({'mode': mode, 'payload': payload})).hexdigest()
        return ident, mode, payload, digest

    def _capabilities(self, payload):
        super()._capabilities(payload)
        row = next((m for m in self._models() if m.get('id') == MODEL), {})
        params = row.get('allowed_passthrough_parameters')
        if (row.get('generate_audio') is not True or not isinstance(params, list)
                or 'watermark' not in params):
            stop('current_model_capabilities_unconfirmed')


def check_image():
    status, raw = get(IMAGE_URL, maximum=5 * 1024 * 1024)
    if status != 200 or not raw.startswith(b'\x89PNG\r\n\x1a\n') or hashlib.sha256(raw).hexdigest() != IMAGE_SHA:
        stop('identity_reference_hash_mismatch')


def preflight(body, state):
    if not shutil.which('ffprobe'):
        stop('media_verifier_unavailable')
    service = OneFilmService(state)
    if not service.connection()['connected']:
        stop('missing_api_configuration')
    account = service._network('/key')
    if not isinstance(account.get('data'), dict):
        stop('api_configuration_unverified')
    _, _, payload, _ = service._request(body)
    service._capabilities(payload)
    check_image()
    return service


def request_digest(body):
    return hashlib.sha256(canonical(body)).hexdigest()


def ready_document(body, run_id):
    return dict(schema_version=1, task_id=TASK, run_id=run_id, status='ready', request_sha256=request_digest(body))


def inspect_media(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)],
                            capture_output=True, timeout=45, check=False)
    if result.returncode or len(result.stdout) > 1024 * 1024:
        stop('media_verification_failed')
    doc = parse(result.stdout, 1024 * 1024)
    streams = doc.get('streams', []) if isinstance(doc, dict) else []
    video = [s for s in streams if isinstance(s, dict) and s.get('codec_type') == 'video']
    audio = [s for s in streams if isinstance(s, dict) and s.get('codec_type') == 'audio']
    try:
        duration = float(doc['format']['duration'])
    except (KeyError, TypeError, ValueError):
        stop('media_verification_failed')
    if (len(video) != 1 or not audio or video[0].get('width') != 720 or video[0].get('height') != 1280
            or not math.isfinite(duration) or not 24.5 <= duration <= 25.5):
        stop('media_verification_failed')
    return dict(duration=duration, width=720, height=1280, audio=True)


def safe_row(row):
    result = {}
    if not isinstance(row, dict):
        return result
    remote = row.get('remote_id')
    if isinstance(remote, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,160}', remote) and not remote.startswith('sk-'):
        result['remote_id'] = remote
    if row.get('status') in TERMINAL | {'pending', 'in_progress', 'submitting'}:
        result['status'] = row['status']
    cost = row.get('cost')
    if type(cost) in (int, float) and math.isfinite(cost) and 0 <= cost <= 1e6:
        result['cost'] = cost
    return result


def safe_error(exc):
    if isinstance(exc, Stopped):
        return {'error_code': str(exc)}
    if isinstance(exc, VideoError) and type(exc.status) is int:
        status = exc.status
        return {'error_code': 'provider_or_transport_stopped', 'http_status': status} if 100 <= status <= 599 else {'error_code': 'generation_stopped'}
    return {'error_code': 'generation_stopped'}


def generate(body, service, output):
    summary = dict(schema_version=1, task_id=TASK, status='stopped')
    try:
        if datetime.now(timezone.utc) >= DEADLINE:
            stop('start_deadline_passed')
        row = service.submit(body)  # Exactly one potentially billable POST; never retry.
        summary.update(safe_row(row))
        deadline = time.monotonic() + 25 * 60
        while row.get('status') not in TERMINAL and time.monotonic() < deadline:
            time.sleep(30)
            try:
                row = service.refresh(row['id'])  # GET only; no resubmission path.
                summary.update(safe_row(row))
            except VideoError:
                summary['error_code'] = 'status_query_failed'
        if row.get('status') != 'completed':
            stop('generation_' + (row.get('status') if row.get('status') in TERMINAL else 'poll_timeout'))
        row = service.download(row['id'])
        media = service.generated / (row['id'] + '.mp4')
        delivered = Path(output) / 'idore-kai-concept.mp4'
        shutil.copyfile(media, delivered)
        details = inspect_media(delivered)
        summary.update(safe_row(row), **details)
        summary.pop('error_code', None)
        return summary
    except Exception as exc:
        jobs = service.jobs().get('jobs', [])
        if jobs:
            summary.update(safe_row(jobs[0]))
        summary.update(safe_error(exc))
        if summary.get('status') == 'completed':
            summary['status'] = 'delivery_unverified'
        return summary


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['keys', 'wait', 'preflight', 'generate'])
    parser.add_argument('--bundle', default='key-bundle')
    parser.add_argument('--envelope', default='request.enc.json')
    parser.add_argument('--state', default='private-state')
    parser.add_argument('--ready', default='reservation/paid-reservation.json')
    parser.add_argument('--output', default='result')
    args = parser.parse_args(argv)
    output = Path(args.output)
    try:
        run_id, secret = context()
        if args.phase == 'keys':
            reservations()
            public, meta = make_keys(args.bundle, run_id, secret)
            print('Public request key for task ' + TASK + '; run ' + run_id)
            print(public.decode('ascii'), end='')
            print('Public key SHA-256: ' + meta['public_key_sha256'])
            return 0
        if args.phase == 'wait':
            wait_envelope(args.envelope)
            print('Encrypted request received.')
            return 0
        body = decrypt_request(args.bundle, read(args.envelope), run_id, secret)
        if args.phase == 'preflight':
            reservations()
            preflight(body, args.state)
            write(args.ready, canonical(ready_document(body, run_id)))
            print('Preflight ready; no generation submitted.')
            return 0
        receipt = parse(read(args.ready, 8192), 8192)
        if (not isinstance(receipt, dict) or set(receipt) != READY_FIELDS
                or receipt != ready_document(body, run_id)):
            stop('paid_reservation_mismatch')
        # Artifact must already be committed in GitHub before any billable POST.
        reservations(run_id)
        service = preflight(body, args.state)
        output.mkdir(parents=True, exist_ok=True)
        summary = generate(body, service, output)
        write(output / 'summary.json', canonical(summary))
        print('One-film result: ' + summary['status'])
        return 0 if summary['status'] == 'completed' and 'error_code' not in summary else 1
    except Exception as exc:
        code = str(exc) if isinstance(exc, Stopped) else 'execution_stopped'
        # All exception messages from network, crypto and provider code are discarded.
        print('One-film execution stopped: ' + code, file=sys.stderr)
        if args.phase == 'generate':
            output.mkdir(parents=True, exist_ok=True)
            if not (output / 'summary.json').exists():
                write(output / 'summary.json', canonical(dict(schema_version=1, task_id=TASK, status='stopped', **safe_error(exc))))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
