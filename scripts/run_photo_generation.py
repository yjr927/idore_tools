"""Generate one private reference-guided photo in an authorized Actions run."""
import base64
import binascii
from http.client import HTTPException
import json
import os
import re
import sys
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

WORKER = 'https://idore-tools.yjr927.workers.dev/api/photo/'
OPENROUTER = 'https://openrouter.ai/api/v1/images'
MODEL = 'bytedance-seed/seedream-4.5'
MAX_INPUT = 6 * 1024 * 1024
MAX_RESULT = 12 * 1024 * 1024
MIME_TYPES = {'image/jpeg', 'image/png', 'image/webp'}


WORKER_CODES = {'not_found', 'method_not_allowed', 'unauthorized', 'service_unavailable',
                'storage_unavailable', 'identity_unavailable',
                'forbidden', 'different_run', 'wrong_status', 'invalid_type', 'body_too_large',
                'invalid_input', 'invalid_result', 'invalid_code'}
OPERATIONS = {'init', 'status', 'input', 'claim', 'result', 'fail'}


class PhotoError(Exception):
    """Only fixed public codes are suitable for the UI and public Actions log."""

    def __init__(self, code, detail=''):
        super().__init__(code)
        self.detail = detail


class TransportError(Exception):
    def __init__(self, status=None, stage='transport', kind='HTTPError', worker_code=None):
        self.status = status
        self.detail = f'stage={stage} type={kind}'
        if isinstance(status, int):
            self.detail += f' http={status}'
        if isinstance(worker_code, str) and worker_code in WORKER_CODES:
            self.detail += ' worker=' + worker_code
        super().__init__(self.detail)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(url, token, method='GET', body=None, content_type=None, *, limit=16384, timeout=30):
    headers = {'Authorization': 'Bearer ' + token, 'User-Agent': 'idore-photo-studio/1.0',
               'Accept': 'application/json'}
    stage = 'provider' if url == OPENROUTER else 'oidc'
    if url.startswith(WORKER):
        op = url.rsplit('/', 1)[-1]
        stage = method + '_' + (op if op in OPERATIONS else 'worker')
        headers['Origin'] = 'https://idore-tools.yjr927.workers.dev'
    if content_type:
        headers['Content-Type'] = content_type
    req = Request(url, data=body, method=method, headers=headers)
    try:
        with build_opener(NoRedirect).open(req, timeout=timeout) as response:
            data = response.read(limit + 1)
            if response.geturl() != url or len(data) > limit:
                raise TransportError(stage=stage, kind='InvalidResponse')
            return data
    except HTTPError as error:
        status = error.code
        worker_code = None
        try:
            if url.startswith(WORKER):
                payload = json.loads(error.read(512))
                if isinstance(payload, dict):
                    worker_code = payload.get('error')
        except (OSError, HTTPException, TypeError, ValueError):
            pass
        finally:
            error.close()
        raise TransportError(status, stage, 'HTTPError', worker_code) from None
    except (OSError, HTTPException) as error:
        reason = getattr(error, 'reason', None)
        kind = type(reason if isinstance(reason, BaseException) else error).__name__
        raise TransportError(stage=stage, kind=kind) from None


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


class PrivateJob:
    def __init__(self, job_id):
        if not re.fullmatch(r'[0-9a-f]{64}', job_id):
            raise PhotoError('invalid_input')
        self.job_id = job_id
        self.token = ''
        self.refresh_at = 0

    def token_for_worker(self):
        if self.token and time.monotonic() < self.refresh_at:
            return self.token
        url = os.environ.get('ACTIONS_ID_TOKEN_REQUEST_URL', '')
        parsed = urlsplit(url)
        host = parsed.hostname or ''
        if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.fragment
                or not host.endswith(('.actions.githubusercontent.com', '.actions.github.com'))):
            raise PhotoError('runner_error', 'stage=oidc type=InvalidEndpoint')
        audience = quote('idore-photo:' + self.job_id, safe='')
        query = url + ('&' if parsed.query else '?') + 'audience=' + audience
        try:
            data = json.loads(request(query, os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']))
            token = data['value']
            if not isinstance(token, str) or not re.fullmatch(r'[\w-]+\.[\w-]+\.[\w-]+', token):
                raise ValueError()
        except TransportError as error:
            raise PhotoError('runner_error', error.detail) from None
        except (KeyError, TypeError, ValueError) as error:
            raise PhotoError('runner_error', 'stage=oidc type=' + type(error).__name__) from None
        self.token = token
        self.refresh_at = time.monotonic() + 90
        return token

    def call(self, operation, method='POST', body=b'', content_type=None, limit=16384):
        return request(WORKER + self.job_id + '/' + operation, self.token_for_worker(),
                       method, body, content_type, limit=limit)


def mime_for(data):
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    return None


def decode_image(encoded, maximum, code):
    if not isinstance(encoded, str) or not 0 < len(encoded) <= 4 * ((maximum + 2) // 3):
        raise PhotoError(code)
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise PhotoError(code) from None
    if len(data) > maximum or not mime_for(data):
        raise PhotoError(code)
    return data


def image_request(raw):
    try:
        document = json.loads(raw)
        prompt, images = document['prompt'], document['images']
    except (TypeError, KeyError, ValueError):
        raise PhotoError('invalid_input') from None
    if (not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4000
            or not isinstance(images, list) or not 1 <= len(images) <= 4):
        raise PhotoError('invalid_input')
    try:
        prompt.encode('utf-8')
    except UnicodeEncodeError:
        raise PhotoError('invalid_input') from None
    refs = []
    remaining = MAX_INPUT
    for image in images:
        if not isinstance(image, dict) or not isinstance(image.get('mime'), str) or image['mime'] not in MIME_TYPES:
            raise PhotoError('invalid_input')
        data = decode_image(image.get('data'), remaining, 'invalid_input')
        if mime_for(data) != image['mime']:
            raise PhotoError('invalid_input')
        remaining -= len(data)
        data_url = 'data:' + image['mime'] + ';base64,' + image['data']
        refs.append({'type': 'image_url', 'image_url': {'url': data_url}})
    return {'model': MODEL, 'n': 1, 'prompt': prompt, 'input_references': refs}


def generate(payload, key):
    try:
        reply = request(OPENROUTER, key, 'POST', dump(payload), 'application/json',
                        limit=4 * ((MAX_RESULT + 2) // 3) + 128 * 1024, timeout=240)
    except TransportError as error:
        codes = {400: 'provider_rejected', 401: 'provider_auth', 402: 'quota_exceeded',
                 403: 'provider_rejected', 404: 'provider_rejected', 422: 'provider_rejected',
                 429: 'rate_limited'}
        raise PhotoError(codes.get(error.status, 'submission_uncertain'), error.detail) from None
    try:
        results = json.loads(reply)['data']
        if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
            raise ValueError()
        image = results[0]
        data = decode_image(image['b64_json'], MAX_RESULT, 'invalid_result')
        mime = mime_for(data)
        if image.get('media_type') and image['media_type'] != mime:
            raise ValueError()
        return data, mime
    except (TypeError, KeyError, ValueError):
        raise PhotoError('invalid_result') from None


def wait_for_input(job):
    deadline = time.monotonic() + 360
    while time.monotonic() < deadline:
        try:
            raw = job.call('input', 'GET', None, limit=(MAX_INPUT * 4 // 3) + 64 * 1024)
            return image_request(raw)
        except TransportError as error:
            if error.status not in (None, 404, 409, 429, 500, 502, 503, 504):
                raise
        time.sleep(3)
    raise PhotoError('input_timeout')


def run(job, key):
    job.call('init')
    try:
        if not key:
            raise PhotoError('missing_key')
        payload = wait_for_input(job)
        # A conflicting or ambiguous claim must never be retried or reported
        # as a failure over another worker's successful reservation.
        try:
            job.call('claim')
        except TransportError as error:
            raise PhotoError('claim_unconfirmed', error.detail) from None
        data, mime = generate(payload, key)
        try:
            job.call('result', 'POST', data, mime)
        except TransportError as error:
            if error.status is not None and error.status < 500 and error.status != 429:
                raise PhotoError('delivery_failed', error.detail) from None
            # Re-send only the completed bytes. The bridge makes result writes idempotent.
            job.call('result', 'POST', data, mime)
    except (TransportError, PhotoError) as error:
        code = str(error) if isinstance(error, PhotoError) else 'runner_error'
        if code != 'claim_unconfirmed':
            try:
                job.call('fail', 'POST', dump({'code': code}), 'application/json')
            except (TransportError, PhotoError):
                pass
        raise PhotoError(code, error.detail) from None


def main():
    try:
        if (os.environ.get('GITHUB_REPOSITORY') != 'yjr927/idore_tools'
                or os.environ.get('GITHUB_REF') != 'refs/heads/main'
                or os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
                or os.environ.get('GITHUB_RUN_ATTEMPT') != '1'):
            raise PhotoError('runner_error')
        run(PrivateJob(os.environ.get('PHOTO_JOB_ID', '')), os.environ.get('OPENROUTER_API_KEY', ''))
    except PhotoError as error:
        print('Photo generation stopped:', str(error), error.detail, file=sys.stderr)
        return 1
    except TransportError as error:
        print('Photo generation stopped:', error.detail, file=sys.stderr)
        return 1
    except Exception:
        # The provider and upload responses can contain private image data.
        print('Photo generation stopped: runner_error', file=sys.stderr)
        return 1
    print('One image is ready in the private photo workspace.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
