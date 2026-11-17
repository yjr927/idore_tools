import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from creative_service import BASE, CreativeError, CreativeService, _transport
from scripts.pipeline_copy import DEFAULT_MODEL, PipelineCopyError, _child_env, extract_frames, generate_copy, validate_copy

KEY = 'sk-test-pipeline-not-a-real-secret'
SOURCE = 'https://www.example.com/dated-dance-report'
JPEG = b'\xff\xd8\xffmock-frame\xff\xd9'
FRAME = 'data:image/jpeg;base64,' + base64.b64encode(JPEG).decode()


def response(text, annotations=None):
    return {'status': 'completed', 'output': [{'type': 'message', 'content': [
        {'type': 'output_text', 'text': text, 'annotations': annotations or []}]}]}


class Transport:
    def __init__(self, failure=None):
        self.calls, self.failure = [], failure

    def __call__(self, request, **kwargs):
        self.calls.append(request)
        if request.get_method() == 'GET':
            return 200, {}, json.dumps({'data': [{'id': DEFAULT_MODEL}]}).encode()
        if self.failure:
            status, body = self.failure
            return status, {}, json.dumps(body).encode()
        body = json.loads(request.data)
        if 'tools' in body:
            result = response('Dated recent source usage explicitly names #GestureDance.',
                [{'type': 'url_citation', 'url': SOURCE, 'title': 'Dated source'}])
            result['output'].insert(0, {'type': 'web_search_call', 'action': {'sources': [{'url': SOURCE, 'title': 'Dated source'}]}})
        else:
            result = response(json.dumps({'captions': ['A little gesture, a lot of attitude.', 'Your move, KAI.', 'Catch the rhythm.'],
                'hashtags': [{'tag': '#GestureDance', 'reason': 'Named in the dated source.', 'category': 'trend', 'source_url': SOURCE},
                             {'tag': '#KAI', 'reason': 'Character name.', 'category': 'brand', 'source_url': None},
                             {'tag': '#HandDance', 'reason': 'Visible hand gestures.', 'category': 'content', 'source_url': None}],
                'trend_note': 'A source-linked suggestion, not an official popularity ranking.'}))
        return 200, {}, json.dumps(result).encode()


class PipelineCopyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.site = Path(self.tmp.name) / 'site'
        (self.site / 'generated').mkdir(parents=True)
        content = b'\x00\x00\x00\x18ftypisom' + b'\x00' * 32
        digest = hashlib.sha256(content).hexdigest()
        self.item = {'id': 'video-' + digest[:24], 'sha256': digest, 'character': 'KAI', 'title': 'Gesture dance'}
        self.item['video_path'] = 'generated/' + self.item['id'] + '.mp4'
        (self.site / self.item['video_path']).write_bytes(content)
        self.env = {'GITHUB_ACTIONS': 'true', 'GITHUB_RUN_ID': '12345', 'GITHUB_RUN_ATTEMPT': '1',
                    'OPENAI_API_KEY': KEY, 'OPENAI_MODEL': '', 'PATH': '/usr/bin'}
        self.transport = Transport()

    def generate(self, **kwargs):
        options = dict(site_dir=self.site, state_dir=Path(self.tmp.name) / 'state',
                       request_fn=self.transport, frame_extractor=lambda path, env: [FRAME] * 4)
        options.update(kwargs)
        return generate_copy(self.item, self.env, **options)

    def test_actual_frames_search_sources_default_model_and_plain_caption(self):
        result = self.generate()
        self.assertNotIn('#', result['caption'])
        self.assertIn('#KAI', result['publication_text'])
        self.assertEqual(result['caption_basis'], 'completed_video_frames')
        self.assertEqual(result['hashtags'][0]['source_url'], SOURCE)
        self.assertEqual(result['hashtags'][0]['observed_at'], result['sources'][0]['observed_at'])
        posts = [json.loads(request.data) for request in self.transport.calls if request.data]
        self.assertEqual(len(posts), 2)
        for payload in posts:
            self.assertEqual(payload['model'], DEFAULT_MODEL)
            images = [part for part in payload['input'][0]['content'] if part['type'] == 'input_image']
            self.assertEqual([image['image_url'] for image in images], [FRAME] * 4)
            self.assertIs(payload['store'], False)
        private = (Path(self.tmp.name) / 'state/creative-jobs.json').read_text()
        self.assertNotIn(KEY, private)
        self.assertNotIn(FRAME, private)
        self.assertEqual(self.generate(), result)
        self.assertEqual(len(self.transport.calls), 3)

    def test_missing_key_model_and_local_execution_never_call_api(self):
        for changes, options, code in [({'OPENAI_API_KEY': ''}, {}, 'missing_key'),
                                      ({'OPENAI_MODEL': ''}, {'default_model': None}, 'missing_model'),
                                      ({'GITHUB_ACTIONS': 'false'}, {}, 'cloud_only')]:
            with self.subTest(code=code):
                env = self.env.copy(); env.update(changes)
                with self.assertRaises(PipelineCopyError) as caught:
                    generate_copy(self.item, env, site_dir=self.site, request_fn=self.transport, **options)
                self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.transport.calls, [])

    def test_path_hash_and_bad_frames_fail_before_api(self):
        original = copy.deepcopy(self.item)
        cases = [dict(video_path='../private.mp4'), dict(sha256='0' * 64), dict(status='pending')]
        for change in cases:
            self.item = dict(original, **change)
            with self.assertRaises(PipelineCopyError) as caught: self.generate()
            self.assertEqual(caught.exception.code, 'invalid_video')
        self.item = original
        with self.assertRaises(PipelineCopyError): self.generate(frame_extractor=lambda path, env: ['https://evil.example/ref.jpg'] * 4)
        with self.assertRaises(PipelineCopyError): self.generate(frame_extractor=lambda path, env: [FRAME])
        self.assertEqual(self.transport.calls, [])

    def test_fabricated_source_membership_is_rejected(self):
        valid = self.generate()
        job = {'status': 'completed', 'draft': {name: valid[name] for name in ('captions', 'hashtags', 'trend_note')},
               'sources': valid['sources']}
        job['draft']['hashtags'][0]['source_url'] = 'https://invented.example/trend'
        with self.assertRaises(PipelineCopyError) as caught: validate_copy(job, KEY)
        self.assertEqual(caught.exception.code, 'invalid_output')

    def test_quota_rate_auth_are_sanitized_distinct_and_never_retried(self):
        for index, (status, error, code) in enumerate([
            (429, {'code': 'insufficient_quota', 'message': KEY}, 'insufficient_quota'),
            (429, {'code': 'credit_balance_exhausted', 'message': KEY}, 'insufficient_quota'),
            (429, {'code': 'project_spend_limit_exceeded', 'message': KEY}, 'insufficient_quota'),
            (429, {'code': 'rate_limit_exceeded', 'message': KEY}, 'rate_limited'),
            (429, {'message': KEY}, 'rate_limited'), (401, {'message': KEY}, 'auth')]):
            self.transport = Transport((status, {'error': error}))
            with self.assertRaises(PipelineCopyError) as caught:
                self.generate(state_dir=Path(self.tmp.name) / ('error-' + str(index)))
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn(KEY, str(caught.exception))
            self.assertEqual(len([request for request in self.transport.calls if request.data]), 1)

    def test_workflow_retry_cannot_start_new_paid_request(self):
        self.env['GITHUB_RUN_ATTEMPT'] = '2'
        with self.assertRaises(PipelineCopyError) as caught: self.generate()
        self.assertEqual(caught.exception.code, 'retry_blocked')
        self.assertEqual(self.transport.calls, [])

    def test_transport_inspects_bounded_error_code_without_exposing_body(self):
        payload = json.dumps({'error': {'code': 'insufficient_quota', 'message': KEY}}).encode()
        failure = HTTPError(BASE + '/responses', 429, 'Rejected', {}, io.BytesIO(payload))
        with patch('creative_service.build_opener') as opener:
            opener.return_value.open.side_effect = failure
            service = CreativeService(Path(self.tmp.name) / 'transport', api_key=KEY)
            with self.assertRaises(CreativeError) as caught: service._network('/responses', 'POST', {})
        self.assertEqual(caught.exception.code, 'insufficient_quota')
        self.assertNotIn(KEY, str(caught.exception))

    def test_frame_decoder_children_do_not_receive_provider_secrets(self):
        child = _child_env(dict(self.env, OPENROUTER_API_KEY='also-private', GH_TOKEN='private-github'))
        self.assertNotIn('OPENAI_API_KEY', child)
        self.assertNotIn('OPENROUTER_API_KEY', child)
        self.assertNotIn('GH_TOKEN', child)
        self.assertEqual(child['PATH'], '/usr/bin')


if __name__ == '__main__': unittest.main()
