import base64
import copy
from datetime import datetime
from email.message import Message
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from video_service import (VideoError, VideoService, BASE, MAX_IMAGE_BYTES,
                           _media_url, _transport, _validate_endpoint)

MODEL = dict(id='bytedance/seedance-2.5', name='Seedance 2.5',
             supported_durations=[4, 6], supported_resolutions=['480p', '720p'],
             supported_aspect_ratios=['9:16', '16:9'],
             supported_frame_images=['first_frame', 'last_frame'], generate_audio=True,
             pricing_skus={'per-video-second': '0.2'})
KEY = 'sk-or-test-secret-not-for-storage'
IMAGE = 'data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\nexample').decode()


def mp4():
    def box(kind, body):
        return (len(body) + 8).to_bytes(4, 'big') + kind + body
    return box(b'ftyp', b'isom\x00\x00\x00\x00isom') + box(b'moov', b'') + box(b'mdat', b'12345678')


class Transport:
    def __init__(self):
        self.calls = []
        self.models = [copy.deepcopy(MODEL), {'id': 'unrelated/other'}]
        self.submit = {'id': 'remote-123', 'status': 'pending', 'polling_url': 'https://evil.example/token'}
        self.poll = {'id': 'remote-123', 'status': 'completed', 'usage': {'cost': 1.25},
                     'unsigned_urls': ['https://evil.example/malicious.mp4?secret=value']}
        self.binary = mp4()
        self.content_type = 'video/mp4'
        self.failure = None
        self.status = 200

    def __call__(self, request, *, timeout, max_bytes):
        self.calls.append((request, timeout, max_bytes))
        path = request.full_url[len(BASE):]
        if self.failure and (self.failure[0] == path):
            value = self.failure[1]
            if isinstance(value, Exception):
                raise value
            return value, {'content-type': 'application/json'}, ('{"error":"' + KEY + '"}').encode()
        if path == '/key':
            result = {'data': {'label': 'never echo key label'}}
        elif path == '/videos/models':
            result = {'data': self.models}
        elif path == '/videos' and request.get_method() == 'POST':
            result = self.submit
        elif path.endswith('/content'):
            return self.status, {'content-type': self.content_type}, self.binary
        else:
            result = self.poll
        return 200, {'content-type': 'application/json'}, json.dumps(result).encode()

    def posts(self):
        return [item[0] for item in self.calls if item[0].get_method() == 'POST']


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.transport = Transport()
        self.time = [1000.0]
        self.env = patch.dict(os.environ, {'OPENROUTER_API_KEY': ''})
        self.env.start()
        self.service = VideoService(self.tmp.name, request_fn=self.transport, clock=lambda: self.time[0])

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def connect(self):
        return self.service.connect({'api_key': KEY})

    def body(self, **overrides):
        result = dict(idempotency_key='unique-request', model=MODEL['id'], prompt='A character dancing',
                      mode='text', duration=4, resolution='720p', aspect_ratio='9:16',
                      generate_audio=True, consent=True)
        result.update(overrides)
        return result

    def test_connection_verification_and_no_disk_or_key_echo(self):
        self.assertEqual(self.transport.calls, [])
        self.assertFalse(self.service.connection()['connected'])
        self.assertEqual(self.connect(), {'connected': True, 'provider': 'OpenRouter', 'key_storage': 'memory'})
        self.assertEqual(self.transport.calls[0][0].full_url, BASE + '/key')
        self.assertEqual(self.transport.calls[0][0].get_header('Authorization'), 'Bearer ' + KEY)
        self.assertFalse(self.service.path.exists())
        self.assertNotIn(KEY, json.dumps(self.service.connection()))
        self.assertFalse(self.service.disconnect()['connected'])

    def test_env_key_no_constructor_network_or_persistence(self):
        with patch.dict(os.environ, {'OPENROUTER_API_KEY': KEY}):
            service = VideoService(self.tmp.name, request_fn=self.transport)
        self.assertTrue(service.connection()['connected'])
        self.assertEqual(self.transport.calls, [])
        self.assertFalse(service.path.exists())

    def test_models_are_exact_seedance_metadata_cached_without_credentials(self):
        self.connect()
        result = self.service.models()
        self.assertEqual(result, {'models': [MODEL]})
        request = self.transport.calls[-1][0]
        self.assertIsNone(request.get_header('Authorization'))
        count = len(self.transport.calls)
        result['models'][0]['supported_durations'].append(100)
        self.assertEqual(self.service.models(), {'models': [MODEL]})
        self.assertEqual(len(self.transport.calls), count)

    def test_explicit_consent_and_safe_parameters_before_paid_call(self):
        self.connect()
        variants = [dict(consent=False), dict(model='openrouter/auto'), dict(duration=True),
                    dict(mode='unknown'), dict(generate_audio=1), dict(callback_url='https://evil.example'),
                    dict(idempotency_key=KEY), dict(prompt='My credential ' + KEY)]
        for changes in variants:
            with self.subTest(changes=changes), self.assertRaises(VideoError):
                self.service.submit(self.body(**changes))
        self.assertEqual(self.transport.posts(), [])
        self.assertFalse(self.service.path.exists())

    def test_submit_durable_before_send_and_idempotency(self):
        self.connect()
        original = self.transport.__call__
        def inspect(request, **kw):
            if request.get_method() == 'POST':
                saved = json.loads(self.service.path.read_text())
                self.assertEqual(saved['jobs'][0]['status'], 'submitting')
            return original(request, **kw)
        self.service._request_fn = inspect
        job = self.service.submit(self.body())
        self.assertEqual(job['status'], 'pending')
        self.assertEqual(job['remote_id'], 'remote-123')
        self.assertEqual(self.service.submit(self.body()), job)
        self.assertEqual(len(self.transport.posts()), 1)
        with self.assertRaises(VideoError) as conflict:
            self.service.submit(self.body(prompt='Different request'))
        self.assertEqual(conflict.exception.status, 409)
        saved = self.service.path.read_text()
        self.assertNotIn(KEY, saved)
        self.assertNotIn('evil.example', saved)
        self.assertNotIn('_request_hash', json.dumps(self.service.jobs()))
        self.assertEqual(self.service.path.stat().st_mode & 0o777, 0o600)

    def test_fresh_model_capabilities_required_on_each_new_submit(self):
        self.connect()
        self.service.models()
        self.transport.models[0]['supported_durations'] = [6]
        with self.assertRaises(VideoError):
            self.service.submit(self.body())
        self.assertEqual(self.transport.posts(), [])
        for field in ('supported_durations', 'supported_resolutions', 'supported_aspect_ratios'):
            self.transport.models = [copy.deepcopy(MODEL)]
            del self.transport.models[0][field]
            with self.subTest(field=field), self.assertRaises(VideoError):
                self.service.submit(self.body())
        self.assertEqual(self.transport.posts(), [])

    def test_no_silent_model_substitution_or_audio_capability_guess(self):
        self.connect()
        self.transport.models = []
        with self.assertRaises(VideoError):
            self.service.submit(self.body())
        self.transport.models = [dict(MODEL, generate_audio=False)]
        with self.assertRaises(VideoError):
            self.service.submit(self.body())
        self.assertEqual(self.transport.posts(), [])

    def test_frames_reference_modes_and_no_media_in_persistence(self):
        self.connect()
        job = self.service.submit(self.body(mode='frames', first_frame=IMAGE, last_frame='https://assets.example.com/last.png'))
        payload = json.loads(self.transport.posts()[-1].data)
        self.assertEqual([frame['frame_type'] for frame in payload['frame_images']], ['first_frame', 'last_frame'])
        self.assertEqual(payload['frame_images'][0]['image_url']['url'], IMAGE)
        self.assertEqual(job['media_count'], 2)
        saved = self.service.path.read_text()
        self.assertNotIn(IMAGE, saved)
        self.assertNotIn('assets.example.com', saved)
        refs = [{'type': 'image_url', 'url': IMAGE}, {'type': 'video_url', 'url': 'https://assets.example.com/motion.mp4'}]
        self.service.submit(self.body(idempotency_key='refs', mode='references', references=refs))
        payload = json.loads(self.transport.posts()[-1].data)
        self.assertEqual(payload['input_references'][1], {'type': 'video_url', 'video_url': {'url': refs[1]['url']}})
        self.assertEqual(payload['provider'], {'only': ['seed', 'byteplus']})
        self.assertNotIn('motion.mp4', self.service.path.read_text())

    def test_invalid_media_and_mode_combinations_never_submitted(self):
        self.connect()
        variants = [dict(first_frame=IMAGE), dict(mode='frames'),
                    dict(mode='frames', first_frame=IMAGE, references=[{'type': 'image_url', 'url': IMAGE}]),
                    dict(mode='references', references=[]),
                    dict(mode='references', references=[{'type': 'video_url', 'url': IMAGE}]),
                    dict(mode='references', references=[{'type': 'video_url', 'url': 'https://www.tiktok.com/@x/video/123'}]),
                    dict(mode='references', references=[{'type': 'video_url', 'url': 'https://example.com/watch/123'}]),
                    dict(mode='references', references=[{'type': 'image_url', 'url': IMAGE}] * 5)]
        for changes in variants:
            with self.subTest(changes=changes), self.assertRaises(VideoError):
                self.service.submit(self.body(**changes))
        self.assertEqual(self.transport.posts(), [])

    def test_advertised_frames_and_generation_two_references_required(self):
        self.connect()
        self.transport.models[0]['supported_frame_images'] = ['first_frame']
        with self.assertRaises(VideoError):
            self.service.submit(self.body(mode='frames', first_frame=IMAGE, last_frame=IMAGE))
        with self.assertRaises(VideoError):
            self.service.submit(self.body(model='bytedance/seedance-1.5-pro', mode='references', references=[{'type': 'image_url', 'url': IMAGE}]))
        self.assertEqual(self.transport.posts(), [])

    def test_timeout_uncertain_persisted_and_never_resubmitted(self):
        self.connect()
        self.transport.failure = ('/videos', TimeoutError(KEY))
        with self.assertRaises(VideoError) as failed:
            self.service.submit(self.body())
        self.assertNotIn(KEY, str(failed.exception))
        job = self.service.jobs()['jobs'][0]
        self.assertEqual(job['status'], 'submission_uncertain')
        self.service.submit(self.body())
        self.assertEqual(len(self.transport.posts()), 1)
        restarted = VideoService(self.tmp.name, request_fn=self.transport)
        self.assertEqual(restarted.submit(self.body())['status'], 'submission_uncertain')
        self.assertEqual(len(self.transport.posts()), 1)

    def test_interrupted_submitting_becomes_uncertain(self):
        self.connect()
        job = self.service.submit(self.body())
        data = json.loads(self.service.path.read_text())
        data['jobs'][0]['status'] = 'submitting'
        self.service.path.write_text(json.dumps(data))
        restarted = VideoService(self.tmp.name, request_fn=self.transport)
        self.assertEqual(restarted.jobs()['jobs'][0]['status'], 'submission_uncertain')
        self.assertEqual(restarted.submit(self.body())['id'], job['id'])
        self.assertEqual(len(self.transport.posts()), 1)

    def test_provider_rejections_sanitized_persisted_without_retry(self):
        self.connect()
        for status in (400, 401, 402, 403, 404, 422, 429, 500, 302):
            self.transport.failure = ('/videos', status)
            body = self.body(idempotency_key='status-' + str(status))
            with self.subTest(status=status), self.assertRaises(VideoError) as failed:
                self.service.submit(body)
            self.assertNotIn(KEY, str(failed.exception))
            job = self.service.submit(body)
            self.assertEqual(job['status'], 'submission_uncertain' if status in (500, 302) else 'failed')
        self.assertEqual(len(self.transport.posts()), 9)
        self.assertNotIn(KEY, self.service.path.read_text())

    def test_key_rejection_does_not_replace_previous_key(self):
        self.connect()
        self.transport.failure = ('/key', 401)
        with self.assertRaises(VideoError) as failed:
            self.service.connect({'api_key': 'another-valid-key'})
        self.assertEqual(failed.exception.status, 401)
        self.assertTrue(self.service.connection()['connected'])
        self.assertNotIn('another-valid-key', str(failed.exception))

    def test_poll_throttle_fixed_endpoint_and_safe_remote_status(self):
        self.connect()
        job = self.service.submit(self.body())
        before = len(self.transport.calls)
        self.assertEqual(self.service.refresh(job['id'])['status'], 'pending')
        self.assertEqual(len(self.transport.calls), before)
        self.time[0] += 31
        done = self.service.refresh(job['id'])
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(done['cost'], 1.25)
        self.assertEqual(self.transport.calls[-1][0].full_url, BASE + '/videos/remote-123')
        self.assertNotIn('evil.example', json.dumps(self.service.jobs()))
        self.service.refresh(job['id'])
        self.assertEqual(self.transport.calls[-1][0].full_url, BASE + '/videos/remote-123')

    def test_download_authenticated_fixed_content_and_atomic_local_mp4(self):
        self.connect()
        job = self.service.submit(self.body())
        self.time[0] += 31
        self.service.refresh(job['id'])
        result = self.service.download(job['id'])
        self.assertEqual(self.transport.calls[-1][0].full_url, BASE + '/videos/remote-123/content')
        self.assertEqual(self.transport.calls[-1][0].get_header('Authorization'), 'Bearer ' + KEY)
        self.assertEqual(result['result_url'], '/generated/' + job['id'] + '.mp4')
        output = self.service.generated / (job['id'] + '.mp4')
        self.assertEqual(output.read_bytes(), mp4())
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        before = len(self.transport.calls)
        self.assertEqual(self.service.download(job['id']), result)
        self.assertEqual(len(self.transport.calls), before)
        self.assertFalse(list(self.service.generated.glob('*.tmp')))

    def test_invalid_output_never_saved(self):
        self.connect()
        job = self.service.submit(self.body())
        self.time[0] += 31
        self.service.refresh(job['id'])
        for raw, mime in ((b'<html>secret</html>', 'text/html'), (b'\x00\x00\x00\x18ftypisom', 'video/mp4'), (mp4(), 'text/html')):
            self.transport.binary, self.transport.content_type = raw, mime
            with self.subTest(mime=mime), self.assertRaises(VideoError):
                self.service.download(job['id'])
            self.assertFalse(list(self.service.generated.iterdir()))
            self.assertIsNone(self.service.jobs()['jobs'][0]['result_url'])

    def test_recover_never_submits_and_requires_safe_remote_id(self):
        job = self.service.recover('remote-123', MODEL['id'])
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.service.recover('remote-123')['id'], job['id'])
        self.assertEqual(job['remote_id'], 'remote-123')
        for bad in ('https://evil.example/x', '../other', 'id?token=secret', KEY):
            with self.assertRaises(VideoError):
                self.service.recover(bad)
        self.connect()
        self.service.refresh(job['id'])
        self.assertEqual(self.transport.posts(), [])
        self.assertEqual(self.transport.calls[-1][0].full_url, BASE + '/videos/remote-123')

    def test_public_jobs_deepcopy_and_raw_urls_redacted_from_metadata(self):
        self.connect()
        self.service.submit(self.body(prompt='Reference https://assets.example.com/image.png and dance'))
        result = self.service.jobs()
        result['jobs'][0]['prompt'] = 'changed'
        self.assertNotEqual(self.service.jobs(), result)
        self.assertNotIn('assets.example.com', self.service.path.read_text())

    def test_malformed_submit_reply_is_uncertain_not_retried(self):
        self.connect()
        for index, reply in enumerate(({'status': 'pending'}, {'id': '../unsafe', 'status': 'pending'},
                                       {'id': 'remote-valid', 'status': 'unknown'})):
            self.transport.submit = reply
            body = self.body(idempotency_key='malformed-' + str(index))
            with self.subTest(reply=reply), self.assertRaises(VideoError):
                self.service.submit(body)
            self.assertEqual(self.service.submit(body)['status'], 'submission_uncertain')
        self.assertEqual(len(self.transport.posts()), 3)

    def test_failed_status_does_not_echo_provider_error_or_url(self):
        self.connect()
        job = self.service.submit(self.body())
        self.transport.poll = {'id': 'remote-123', 'status': 'failed', 'error': KEY + ' https://secret.example/x'}
        self.time[0] += 31
        failed = self.service.refresh(job['id'])
        self.assertEqual(failed['status'], 'failed')
        self.assertNotIn(KEY, json.dumps(failed))
        self.assertNotIn('secret.example', self.service.path.read_text())

    def test_cancelled_and_expired_are_terminal_without_download_or_resubmission(self):
        self.connect()
        for status in ('cancelled', 'expired'):
            with self.subTest(status=status):
                body = self.body(idempotency_key='terminal-' + status)
                job = self.service.submit(body)
                self.transport.poll = {'id': 'remote-123', 'status': status,
                                       'error': KEY + ' https://secret.example/private'}
                self.time[0] += 31
                terminal = self.service.refresh(job['id'])
                self.assertEqual(terminal['status'], status)
                self.assertTrue(terminal['error'])
                self.assertNotIn(KEY, json.dumps(terminal))
                calls = len(self.transport.calls)
                self.time[0] += 31
                self.assertEqual(self.service.refresh(job['id']), terminal)
                self.assertEqual(self.service.submit(body), terminal)
                with self.assertRaises(VideoError) as unavailable:
                    self.service.download(job['id'])
                self.assertEqual(unavailable.exception.status, 409)
                self.assertEqual(len(self.transport.calls), calls)
        self.assertEqual(len(self.transport.posts()), 2)
        self.assertNotIn('secret.example', self.service.path.read_text())
        self.assertFalse(list(self.service.generated.iterdir()))


class BoundaryTests(unittest.TestCase):
    def test_endpoint_allowlist_and_redirects_never_followed(self):
        for url, method in [('https://evil.example/api/v1/key', 'GET'),
                            ('http://openrouter.ai/api/v1/key', 'GET'),
                            (BASE + '/videos/../key', 'GET'), (BASE + '/key?secret=x', 'GET'),
                            (BASE + '/videos/x/content', 'POST'), ('https://u:p@openrouter.ai/api/v1/key', 'GET')]:
            with self.subTest(url=url), self.assertRaises(VideoError):
                _validate_endpoint(url, method)
        from urllib.request import Request
        headers = Message()
        headers['Location'] = 'https://evil.example/key'
        with patch('video_service.build_opener') as opener:
            opener.return_value.open.side_effect = HTTPError(BASE + '/key', 302, 'move', headers, None)
            status, _, raw = _transport(Request(BASE + '/key'), timeout=1, max_bytes=100)
        self.assertEqual(status, 302)
        self.assertEqual(raw, b'')

    def test_private_share_and_credential_media_rejected(self):
        for url in ('http://assets.example.com/a.png', 'https://user:secret@example.com/a.png',
                    'https://127.0.0.1/a.png', 'https://127.1/a.png', 'https://10.0.0.1/a.png',
                    'https://169.254.169.254/a.png', 'https://localhost/a.png',
                    'https://device.local/a.png', 'https://www.tiktok.com/a.png',
                    'https://v.douyin.com/abc', 'https://assets.example.com/a.png#secret'):
            with self.subTest(url=url), self.assertRaises(VideoError):
                _media_url(url, 'image_url')
        self.assertEqual(_media_url('https://assets.example.com/a.mp4?signature=foo', 'video_url'),
                         'https://assets.example.com/a.mp4?signature=foo')

    def test_invalid_and_oversized_images_rejected(self):
        for value in ('data:image/png;base64,notbase64', 'data:image/svg+xml;base64,PHN2Zz4=',
                      'data:image/png;base64,' + base64.b64encode(b'bad').decode(),
                      'data:image/png;base64,' + 'A' * ((MAX_IMAGE_BYTES // 3 + 1) * 4 + 4)):
            with self.assertRaises(VideoError):
                _media_url(value, 'image_url')

    def test_official_tiktok_cdn_mp4_is_distinct_from_share_pages(self):
        url = 'https://v16-webapp-prime.us.tiktok.com/video/tos/alisg/clip123/?mime_type=video_mp4&signature=example'
        self.assertEqual(_media_url(url, 'video_url'), url)
        for invalid in (
                url.replace('.us.tiktok.com', '.us.tiktok.com.evil.example'),
                url.replace('v16-webapp-prime.us.tiktok.com', 'www.tiktok.com'),
                url.replace('/video/tos/alisg/clip123/', '/@bizzyboom/video/7706633304702848276'),
                url.replace('mime_type=video_mp4', 'mime_type=text_html'),
                url.replace('mime_type=video_mp4', 'mime_type=video_mp4&mime_type=text_html')):
            with self.subTest(invalid=invalid), self.assertRaises(VideoError):
                _media_url(invalid, 'video_url')

    def test_verified_us_tiktok_cdn_requires_hashed_path_and_mp4_mime(self):
        url = ('https://v16m.tiktokcdn-us.com/' + 'a' * 32 + '/' + 'b' * 8
               + '/video/tos/alisg/tos-alisg-pve-0068c001/clip_example/'
               + '?mime_type=video_mp4&signature=example')
        self.assertEqual(_media_url(url, 'video_url'), url)
        self.assertEqual(_media_url(url.replace('v16m.', 'v19.'), 'video_url'),
                         url.replace('v16m.', 'v19.'))
        for invalid in (
                url.replace('tiktokcdn-us.com', 'tiktokcdn-us.com.evil.example'),
                url.replace('v16m.', 'anything.'),
                url.replace('v16m.', 'v16many.'),
                url.replace('tiktokcdn-us.com', 'tiktokcdn-eu.com'),
                url.replace('a' * 32 + '/' + 'b' * 8 + '/', ''),
                url.replace('/video/tos/', '/@bizzyboom/video/'),
                url.replace('mime_type=video_mp4', 'mime_type=text_html'),
                url.replace('mime_type=video_mp4', 'mime_type=video_mp4&mime_type=text_html')):
            with self.subTest(invalid=invalid), self.assertRaises(VideoError):
                _media_url(invalid, 'video_url')
