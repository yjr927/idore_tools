import base64
import copy
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request
import zlib

from creative_service import CreativeError, CreativeService, BASE, _endpoint, _png, _transport

KEY = 'sk-test-creative-key-should-never-be-persisted'
IMAGE_MODEL = 'gpt-image-2.5-sunburst'
TEXT_MODEL = 'gpt-6.1'
SOURCE = 'https://www.tiktok.com/discover/kpop-dance'


def png():
    def chunk(kind, data):
        return len(data).to_bytes(4, 'big') + kind + data + (zlib.crc32(kind + data) & 0xffffffff).to_bytes(4, 'big')
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 2, 2, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\x00\x00\x00\x00\x00\x00\x00' * 2)) + chunk(b'IEND', b''))


IMAGE = 'data:image/png;base64,' + base64.b64encode(png()).decode()


def draft():
    return dict(captions=['A new look, same energy.', 'One beat changes everything.', 'Which look is your favorite?'],
        hashtags=[dict(tag='#KpopDance', reason='Recent usage in the cited source.', category='trend', source_url=SOURCE),
                  dict(tag='#VirtualIdol', reason='Matches the character.', category='content', source_url=None),
                  dict(tag='#LittleDeer', reason='Supplied character name.', category='brand', source_url=None)],
        trend_note='Suggestions for a US audience.')


def response(text, annotations=None):
    return {'status': 'completed', 'output': [{'type': 'message', 'content': [
        {'type': 'output_text', 'text': text, 'annotations': annotations or []}]}]}


class Transport:
    def __init__(self):
        self.calls = []
        self.model_ids = [IMAGE_MODEL, TEXT_MODEL, 'gpt-image-1', 'gpt-image-1-2025-04-15',
                          'gpt-5-mini', 'gpt-6-codex', 'gpt-4o-audio-preview', 'text-embedding-3-small']
        self.fail_path = None
        self.fail_number = 1
        self.failure = None
        self.image_data = base64.b64encode(png()).decode()
        self.research = response('Recent evidence explicitly names #KpopDance.', [{'type': 'url_citation', 'url': SOURCE, 'title': 'K-pop dance'}])
        self.research['output'].insert(0, {'type': 'web_search_call', 'action': {'sources': [{'url': SOURCE, 'title': 'K-pop dance'}]}})
        self.draft = response(json.dumps(draft()))

    def __call__(self, request, *, timeout, max_bytes):
        self.calls.append((request, timeout, max_bytes))
        path = request.full_url[len(BASE):]
        matching = sum(item[0].full_url == request.full_url for item in self.calls)
        if path == self.fail_path and matching >= self.fail_number:
            if isinstance(self.failure, Exception):
                raise self.failure
            return self.failure, {}, ('{"error":"' + KEY + ' https://private.example/a.png"}').encode()
        if path == '/models':
            result = {'data': [{'id': ident} for ident in self.model_ids]}
        elif path == '/images/edits':
            result = {'data': [{'b64_json': self.image_data}]}
        else:
            payload = json.loads(request.data)
            result = self.research if 'tools' in payload else self.draft
        return 200, {'content-type': 'application/json'}, json.dumps(result).encode()

    def posts(self):
        return [request for request, _, _ in self.calls if request.get_method() == 'POST']


class CreativeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {'OPENAI_API_KEY': ''})
        self.environment.start()
        self.transport = Transport()
        self.service = CreativeService(self.tmp.name, request_fn=self.transport)

    def tearDown(self):
        self.environment.stop()
        self.tmp.cleanup()

    def connect(self):
        return self.service.connect({'api_key': KEY})

    def images(self, **extra):
        result = dict(idempotency_key='image-pair-one', model=IMAGE_MODEL, reference_image=IMAGE,
                      before_prompt='White casual clothes and natural makeup.', after_prompt='Stage outfit and dramatic eyeliner.', consent=True)
        result.update(extra)
        return result

    def drafting(self, **extra):
        result = dict(idempotency_key='draft-one', model=TEXT_MODEL, description='A virtual character does a K-pop outfit transition.',
                      character='Little Deer', frames=[IMAGE], trend_context='', search_trends=False, consent=True)
        result.update(extra)
        return result

    def test_constructor_no_network_and_key_stays_memory_only(self):
        self.assertFalse(self.service.connection()['connected'])
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.connect(), {'connected': True, 'provider': 'OpenAI', 'key_storage': 'memory'})
        request = self.transport.calls[0][0]
        self.assertEqual(request.full_url, BASE + '/models')
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + KEY)
        self.assertFalse(self.service.path.exists())
        self.assertNotIn(KEY, json.dumps(self.service.connection()))
        self.assertFalse(self.service.disconnect()['connected'])

    def test_environment_key_never_read_back_or_stored(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': KEY}):
            service = CreativeService(self.tmp.name, request_fn=self.transport)
        self.assertTrue(service.connection()['connected'])
        self.assertFalse(service.path.exists())
        self.assertEqual(self.transport.calls, [])

    def test_models_exact_account_ids_and_shutdown_model_excluded(self):
        self.connect()
        result = self.service.models()
        self.assertEqual(result['image_models'], [{'id': IMAGE_MODEL}])
        self.assertEqual(result['text_models'], [{'id': 'gpt-5-mini'}, {'id': TEXT_MODEL}])
        self.transport.model_ids = []
        with self.assertRaises(CreativeError):
            self.service.generate_images(self.images())
        self.assertFalse(self.transport.posts())

    def test_two_real_edit_payloads_same_identity_and_saved_png_pair(self):
        self.connect()
        original = self.transport.__call__
        def check(request, **kw):
            if request.get_method() == 'POST':
                stored = json.loads(self.service.path.read_text())['jobs'][0]
                self.assertEqual(stored['status'], 'running')
                self.assertEqual(stored['steps'][stored['_active_step']], 'submitting')
            return original(request, **kw)
        self.service._request_fn = check
        result = self.service.generate_images(self.images())
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['kind'], 'images')
        requests = self.transport.posts()
        self.assertEqual(len(requests), 2)
        before, after = [json.loads(request.data) for request in requests]
        self.assertEqual(before['images'], [{'image_url': IMAGE}])
        self.assertEqual(after['images'], [{'image_url': IMAGE}, {'image_url': IMAGE}])
        self.assertIn('same character identity', before['prompt'])
        self.assertEqual(before['n'], 1)
        self.assertEqual(before['size'], '1024x1536')
        self.assertEqual(before['output_format'], 'png')
        self.assertEqual(before['quality'], 'medium')
        for label in ('before', 'after'):
            filename = result[label + '_url'].rsplit('/', 1)[1]
            self.assertRegex(filename, r'^style-[a-f0-9]{32}-' + label + r'\.png$')
            target = self.service.generated / filename
            self.assertEqual(target.read_bytes(), png())
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        stored = self.service.path.read_text()
        self.assertNotIn(KEY, stored)
        self.assertNotIn(IMAGE, stored)
        self.assertEqual(self.service.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.service.generated.glob('*.tmp')))

    def test_image_idempotency_changes_rejected_and_no_retry_after_restart(self):
        self.connect()
        result = self.service.generate_images(self.images())
        self.assertEqual(self.service.generate_images(self.images()), result)
        self.assertEqual(len(self.transport.posts()), 2)
        with self.assertRaises(CreativeError) as conflict:
            self.service.generate_images(self.images(after_prompt='Another look'))
        self.assertEqual(conflict.exception.status, 409)
        restarted = CreativeService(self.tmp.name, request_fn=self.transport)
        self.assertEqual(restarted.generate_images(self.images()), result)
        self.assertEqual(len(self.transport.posts()), 2)

    def test_invalid_inputs_and_missing_consent_do_not_pay(self):
        self.connect()
        variants = [dict(consent=False), dict(reference_image='https://127.0.0.1/private.png'),
                    dict(reference_image='data:image/svg+xml;base64,PHN2Zz4='), dict(model='gpt-image-1'),
                    dict(before_prompt=''), dict(before_prompt=KEY), dict(api_base_url='https://evil.example'),
                    dict(idempotency_key=KEY)]
        for changes in variants:
            with self.subTest(changes=changes), self.assertRaises(CreativeError):
                self.service.generate_images(self.images(**changes))
        self.assertFalse(self.transport.posts())
        self.assertFalse(self.service.path.exists())

    def test_cloud_public_image_reference_passed_not_fetched_or_persisted(self):
        self.connect()
        reference = 'https://media.example.com/character.png?signature=private-marker'
        self.service.generate_images(self.images(reference_image=reference))
        self.assertTrue(all(request.full_url.startswith(BASE) for request, _, _ in self.transport.calls))
        self.assertEqual(json.loads(self.transport.posts()[0].data)['images'][0]['image_url'], reference)
        self.assertNotIn('private-marker', self.service.path.read_text())
        self.assertNotIn(reference, json.dumps(self.service.jobs()))

    def test_after_rejection_preserves_before_as_partial_and_never_retries(self):
        self.connect()
        self.transport.fail_path, self.transport.fail_number, self.transport.failure = '/images/edits', 2, 402
        with self.assertRaises(CreativeError) as rejected:
            self.service.generate_images(self.images())
        self.assertEqual(rejected.exception.status, 402)
        result = self.service.jobs()['jobs'][0]
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(result['before_url'])
        self.assertIsNone(result['after_url'])
        self.assertEqual(self.service.generate_images(self.images()), result)
        self.assertEqual(len(self.transport.posts()), 2)
        self.assertNotIn(KEY, self.service.path.read_text())

    def test_timeout_keeps_uncertain_no_retry_and_recovers_interrupted_running(self):
        self.connect()
        self.transport.fail_path, self.transport.failure = '/images/edits', TimeoutError(KEY)
        with self.assertRaises(CreativeError) as uncertain:
            self.service.generate_images(self.images())
        self.assertNotIn(KEY, str(uncertain.exception))
        self.assertEqual(self.service.jobs()['jobs'][0]['status'], 'submission_uncertain')
        self.service.generate_images(self.images())
        self.assertEqual(len(self.transport.posts()), 1)
        stored = json.loads(self.service.path.read_text())
        stored['jobs'][0]['status'] = 'running'
        self.service.path.write_text(json.dumps(stored))
        restarted = CreativeService(self.tmp.name, request_fn=self.transport)
        self.assertEqual(restarted.jobs()['jobs'][0]['status'], 'submission_uncertain')
        restarted.generate_images(self.images())
        self.assertEqual(len(self.transport.posts()), 1)

    def test_invalid_generated_png_not_saved_or_automatically_regenerated(self):
        self.connect()
        self.transport.image_data = base64.b64encode(b'\x89PNG\r\n\x1a\nbroken').decode()
        with self.assertRaises(CreativeError):
            self.service.generate_images(self.images())
        self.assertFalse(list(self.service.generated.iterdir()))
        self.assertEqual(self.service.generate_images(self.images())['status'], 'submission_uncertain')
        self.assertEqual(len(self.transport.posts()), 1)

    def test_non_search_draft_english_schema_store_false_and_trend_downgrade(self):
        self.connect()
        result = self.service.generate_draft(self.drafting(trend_context='Use this unverified source ' + SOURCE))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['kind'], 'draft')
        self.assertEqual(len(result['draft']['captions']), 3)
        self.assertEqual(result['sources'], [])
        tag = result['draft']['hashtags'][0]
        self.assertEqual(tag['category'], 'content')
        self.assertIsNone(tag['source_url'])
        self.assertIsNone(tag['observed_at'])
        self.assertIn('No source-backed', result['draft']['trend_note'])
        request = self.transport.posts()[0]
        payload = json.loads(request.data)
        self.assertFalse(payload['store'])
        self.assertEqual(payload['text']['format']['type'], 'json_schema')
        self.assertTrue(payload['text']['format']['strict'])
        self.assertIn('TikTok US in English', payload['instructions'])
        self.assertEqual(payload['input'][0]['content'][-1]['image_url'], IMAGE)
        self.assertNotIn(IMAGE, self.service.path.read_text())
        self.assertEqual(len(self.transport.posts()), 1)

    def test_search_draft_two_calls_and_actual_source_provenance(self):
        self.connect()
        result = self.service.generate_draft(self.drafting(search_trends=True))
        first, second = [json.loads(request.data) for request in self.transport.posts()]
        self.assertFalse(first['store'])
        self.assertFalse(second['store'])
        self.assertEqual(first['tools'][0]['user_location'], {'type': 'approximate', 'country': 'US'})
        self.assertEqual(first['include'], ['web_search_call.action.sources'])
        self.assertIn('Today in America/New_York is ', first['instructions'])
        self.assertIn('past 7 days', first['instructions'])
        self.assertIn('30 days', first['instructions'])
        self.assertNotIn('tools', second)
        tag = result['draft']['hashtags'][0]
        self.assertEqual(tag['category'], 'trend')
        self.assertEqual(tag['source_url'], SOURCE)
        self.assertTrue(tag['observed_at'])
        self.assertEqual(result['sources'][0]['url'], SOURCE)
        self.assertIn('not an official live', result['draft']['trend_note'])

    def test_fabricated_or_unmentioned_trend_hashtags_never_source_backed(self):
        self.connect()
        for index, changes in enumerate(({'tag': '#InventedTrend'}, {'source_url': 'https://invented.example/new'})):
            value = draft()
            value['hashtags'][0].update(changes)
            self.transport.draft = response(json.dumps(value))
            result = self.service.generate_draft(self.drafting(idempotency_key='bad-trend-' + str(index), search_trends=True))
            self.assertEqual(result['draft']['hashtags'][0]['category'], 'content')
            self.assertIsNone(result['draft']['hashtags'][0]['source_url'])
        self.assertNotIn('invented.example', self.service.path.read_text())

    def test_search_without_actual_sources_cannot_claim_current_trend(self):
        self.connect()
        self.transport.research = response('Search found #KpopDance but no references.')
        result = self.service.generate_draft(self.drafting(search_trends=True))
        self.assertEqual(result['sources'], [])
        self.assertEqual(result['draft']['hashtags'][0]['category'], 'content')

    def test_draft_validation_rejects_extra_fields_or_bad_hashtags(self):
        self.connect()
        for index, change in enumerate(('bad_count', 'duplicate', 'invalid_tag', 'extra_field')):
            value = draft()
            if change == 'bad_count': value['captions'] = ['only one']
            elif change == 'duplicate': value['hashtags'][1]['tag'] = value['hashtags'][0]['tag']
            elif change == 'invalid_tag': value['hashtags'][0]['tag'] = '#bad tag'
            else: value['extra'] = KEY
            self.transport.draft = response(json.dumps(value))
            with self.subTest(change=change), self.assertRaises(CreativeError):
                self.service.generate_draft(self.drafting(idempotency_key='bad-draft-' + str(index)))
        self.assertNotIn(KEY, self.service.path.read_text())

    def test_draft_invalid_frames_and_credential_inputs_never_paid(self):
        self.connect()
        for changes in (dict(frames=[IMAGE] * 5), dict(frames=['https://example.com/image.png']),
                        dict(description=KEY), dict(search_trends='true'), dict(model='gpt-6-codex')):
            with self.subTest(changes=changes), self.assertRaises(CreativeError):
                self.service.generate_draft(self.drafting(**changes))
        self.assertFalse(self.transport.posts())

    def test_api_error_bodies_and_redirects_never_leak_or_auto_retry(self):
        self.connect()
        for status in (400, 401, 402, 403, 404, 422, 429, 302, 500):
            self.transport.fail_path, self.transport.failure = '/responses', status
            body = self.drafting(idempotency_key='status-' + str(status))
            with self.subTest(status=status), self.assertRaises(CreativeError) as rejected:
                self.service.generate_draft(body)
            self.assertNotIn(KEY, str(rejected.exception))
            self.assertNotIn('private.example', str(rejected.exception))
            result = self.service.generate_draft(body)
            self.assertEqual(result['status'], 'submission_uncertain' if status in (302, 500) else 'failed')
        self.assertEqual(len(self.transport.posts()), 9)
        self.assertNotIn(KEY, self.service.path.read_text())

    def test_jobs_are_deep_copies_and_private_fingerprint_not_returned(self):
        self.connect()
        self.service.generate_draft(self.drafting())
        jobs = self.service.jobs()
        self.assertNotIn('_request_hash', json.dumps(jobs))
        jobs['jobs'][0]['draft']['captions'][0] = 'Changed'
        self.assertNotEqual(jobs, self.service.jobs())

    def test_key_rejection_preserves_existing_connection(self):
        self.connect()
        self.transport.fail_path, self.transport.failure = '/models', 401
        with self.assertRaises(CreativeError):
            self.service.connect({'api_key': 'replacement-key'})
        self.assertTrue(self.service.connection()['connected'])
        self.assertEqual(self.transport.posts(), [])


class BoundaryTests(unittest.TestCase):
    def test_fixed_openai_endpoint_and_no_redirect_follow(self):
        for url, method in (('https://evil.example/models', 'GET'), (BASE + '/models?token=secret', 'GET'),
                            (BASE + '/responses', 'GET'), ('http://api.openai.com/v1/models', 'GET'),
                            ('https://u:p@api.openai.com/v1/models', 'GET')):
            with self.subTest(url=url), self.assertRaises(CreativeError):
                _endpoint(url, method)
        with patch('creative_service.build_opener') as opener:
            opener.return_value.open.side_effect = HTTPError(BASE + '/models', 302, 'move', {}, None)
            status, _, raw = _transport(Request(BASE + '/models'), timeout=1, max_bytes=100)
        self.assertEqual(status, 302)
        self.assertEqual(raw, b'')

    def test_png_structure_crc_and_trailing_bytes_checked(self):
        self.assertTrue(_png(png()))
        self.assertFalse(_png(png() + b'unsafe'))
        corrupt = bytearray(png()); corrupt[30] ^= 1
        self.assertFalse(_png(bytes(corrupt)))
        self.assertFalse(_png(b'<html>not an image</html>'))
