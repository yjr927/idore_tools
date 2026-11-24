"""Run bounded synthetic OpenRouter probes without disclosing requests, replies or credentials."""
import argparse
import base64
from http.client import HTTPException
import json
import os
import re
import socket
import ssl
import struct
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zlib

ENDPOINT = 'https://openrouter.ai/api/v1/chat/completions'
DEFAULT_MODEL = 'openai/gpt-5.4-mini'
MAX_REPLY = 64 * 1024
HTTP_ERRORS = {400: 'invalid_request', 401: 'authentication_failed', 402: 'quota_exceeded',
               403: 'permission_denied', 404: 'model_or_endpoint_unavailable', 408: 'timed_out',
               413: 'payload_too_large', 422: 'unprocessable_request', 429: 'rate_limited',
               502: 'bad_gateway', 503: 'service_unavailable', 504: 'gateway_timeout'}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def synthetic_image():
    def chunk(name, data):
        return (struct.pack('>I', len(data)) + name + data +
                struct.pack('>I', zlib.crc32(name + data)))
    size = 96
    rows = b''.join(b'\x00' + b''.join(b'\x29\x6e\xff' if 24 <= x < 72 and 24 <= y < 72
                                       else b'\xff\xff\xff' for x in range(size)) for y in range(size))
    png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0)) +
           chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))
    return 'data:image/png;base64,' + base64.b64encode(png).decode('ascii')


def make_payload(model, use_image):
    prompt = 'Report the square color as a lowercase English word.' if use_image else 'Report blue as the color.'
    parts = [{'type': 'text', 'text': prompt}]
    if use_image:
        parts.append({'type': 'image_url', 'image_url': {'url': synthetic_image(), 'detail': 'low'}})
    return {
        'model': model,
        'messages': [{'role': 'system', 'content': 'Respond with the requested JSON.'},
                     {'role': 'user', 'content': parts}],
        'provider': {'require_parameters': True, 'allow_fallbacks': False},
        'max_tokens': 256,
        'response_format': {'type': 'json_schema', 'json_schema': {
            'name': 'probe', 'strict': True, 'schema': {'type': 'object',
                'properties': {'color': {'type': 'string'}}, 'required': ['color'], 'additionalProperties': False}}},
    }


def interpret(result, row):
    if not isinstance(result, dict):
        row['status'] = 'invalid_reply'
        return
    ident = result.get('id')
    row['generation_id_present'] = isinstance(ident, str) and bool(re.fullmatch(r'gen-[A-Za-z0-9_-]{1,150}', ident))
    if 'error' in result:
        err = result['error']
        code = err.get('code') if isinstance(err, dict) else None
        row['status'] = HTTP_ERRORS.get(code, 'provider_error') if type(code) is int else 'provider_error'
        if type(code) is int and 100 <= code <= 599:
            row['provider_status'] = code
        return
    try:
        choice = result['choices'][0]
        body = json.loads(choice['message']['content'])
        if choice.get('finish_reason') != 'stop' or body != {'color': 'blue'}:
            row['status'] = 'unexpected_result'
            return
    except (IndexError, KeyError, TypeError, ValueError, AttributeError):
        row['status'] = 'invalid_reply'
        return
    row['status'] = 'completed'


def probe(key, payload):
    row = {'http_status': None, 'status': 'transport_error', 'generation_id_present': False}
    start = time.monotonic()
    req = Request(ENDPOINT, method='POST', data=json.dumps(payload, separators=(',', ':')).encode(),
                  headers={'Authorization': 'Bearer ' + key, 'Accept': 'application/json', 'Content-Type': 'application/json'})
    try:
        with build_opener(NoRedirect).open(req, timeout=120) as reply:
            row['http_status'] = reply.status
            if reply.status != 200 or reply.geturl() != ENDPOINT:
                row['status'] = 'unexpected_response'
            else:
                raw = reply.read(MAX_REPLY + 1)
                if len(raw) > MAX_REPLY:
                    row['status'] = 'reply_too_large'
                else:
                    interpret(json.loads(raw), row)
    except HTTPError as error:
        row['http_status'] = error.code
        row['status'] = HTTP_ERRORS.get(error.code, 'http_error')
        try:
            body = error.read(16384)
            row['error_body_present'] = bool(body)
            if re.search(rb'\berror code:\s*1010\b', body, re.I):
                row['status'] = 'edge_rejected_1010'
            else:
                document = json.loads(body)
                item = document.get('error') if isinstance(document, dict) else None
                row['api_error_envelope'] = isinstance(item, dict)
                code = item.get('code') if isinstance(item, dict) else None
                if type(code) is int and 0 <= code <= 599:
                    row['api_error_code'] = code
        except (OSError, HTTPException, ValueError, TypeError):
            pass
        finally:
            error.close()
    except (OSError, HTTPException) as error:
        cause = error.reason if isinstance(error, URLError) else error
        if isinstance(cause, (TimeoutError, socket.timeout)):
            row['status'] = 'network_timeout'
        elif isinstance(cause, socket.gaierror):
            row['status'] = 'dns_error'
        elif isinstance(cause, ssl.SSLError):
            row['status'] = 'tls_error'
    except (TypeError, ValueError):
        row['status'] = 'invalid_reply'
    row['elapsed_ms'] = round((time.monotonic() - start) * 1000)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--modality', choices=('text', 'image', 'both'), default='image',
                        help='Choose both to check text first and then a separately submitted image.')
    parser.add_argument('--samples', type=int, choices=(1, 2),
                        help='A second independent sample requires a confirmed first reply with a generation ID.')
    args = parser.parse_args()
    if args.modality == 'both' and args.samples == 1:
        parser.error('--modality both requires two samples; omit --samples or specify --samples 2')
    modalities = ('text', 'image') if args.modality == 'both' else (args.modality,) * (args.samples or 1)
    key = os.environ.get('OPENROUTER_API_KEY', '').strip()
    model = os.environ.get('OPENROUTER_TEXT_MODEL') or DEFAULT_MODEL
    if not key or not re.fullmatch(r'openai/gpt-[a-z0-9]+(?:[.-][a-z0-9]+)*', model):
        print(json.dumps({'status': 'missing_key' if not key else 'invalid_model', 'paid_requests': 0}))
        return 1
    for i, modality in enumerate(modalities, start=1):
        result = probe(key, make_payload(model, modality == 'image'))
        print(json.dumps(dict(result, sample=i, modality=modality, model=model)), flush=True)
        if result['status'] != 'completed':
            return 1
        if i < len(modalities) and not result['generation_id_present']:
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
