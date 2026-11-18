import base64
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request
from zoneinfo import ZoneInfo

from scripts.openrouter_copy import (DEFAULT_MODEL, ENDPOINT, OpenRouterCopyError,
                                    _recent, _transport, finalize_draft, generate_router_draft)

KEY = 'sk-or-test-not-a-real-private-key'
FRAME = 'data:image/jpeg;base64,' + base64.b64encode(b'\xff\xd8\xfftest\xff\xd9').decode()
SOURCE = 'https://example.com/dance-report'
TODAY = datetime.now(ZoneInfo('America/New_York')).date().isoformat()


def message(text, annotations=None):
    return {'type': 'message', 'content': [{'type': 'output_text', 'text': text,
                                          'annotations': annotations or []}]}


class FakeTransport:
    def __init__(self, directory):
        self.directory, self.calls, self.failure = Path(directory), [], None
        self.research = {'status': 'completed', 'usage': {'server_tool_use_details': {'web_search_requests': 1}},
            'output': [message('Relevant usage includes #HandDance, with no official live ranking.',
                [{'type': 'url_citation', 'url_citation': {'url': SOURCE, 'title': 'Dance report',
                    'content': 'Published ' + TODAY + '. #HandDance is used in gesture videos.'}}])]}
        self.draft = {'status': 'completed', 'output': [message(json.dumps({
            'captions': ['KAI caught the rhythm.', 'Small moves, big mood.', 'Your turn.'],
            'hashtags': [dict(tag='#HandDance', reason='Relevant dated usage.', category='trend', source_url=SOURCE),
                         dict(tag='#KAI', reason='Character name.', category='brand', source_url=None),
                         dict(tag='#Gesture', reason='Visible gestures.', category='content', source_url=None)],
            'trend_note': 'A dated source suggestion, not an official TikTok ranking.'}))]}

    def __call__(self, request, *, timeout, max_bytes):
        self.calls.append(request)
        self.assert_claim(request)
        if self.failure:
            if isinstance(self.failure, Exception): raise self.failure
            return self.failure, {}, b'{"error":{"message":"' + KEY.encode() + b'"}}'
        value = self.research if len(self.calls) == 1 else self.draft
        return 200, {}, json.dumps(value).encode()

    def assert_claim(self, request):
        records = list(self.directory.glob('router-copy-*.json'))
        assert len(records) == 1
        state = json.loads(records[0].read_text())
        step = 'search' if len(self.calls) == 1 else 'draft'
        assert state['steps'][step] == 'submitting'
        assert KEY not in records[0].read_text() and FRAME not in records[0].read_text()
        assert request.full_url == ENDPOINT and request.get_method() == 'POST'
        assert request.get_header('Authorization') == 'Bearer ' + KEY


class OpenRouterCopyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.transport = FakeTransport(self.tmp.name)
        self.args = dict(key=KEY, frames=[FRAME] * 4, character='KAI', title='Gesture dance',
                         state_dir=self.tmp.name, request_token='copy-test-123', request_fn=self.transport)

    def generate(self, **overrides):
        return generate_router_draft(**dict(self.args, **overrides))

    def test_two_bounded_calls_real_source_and_completed_idempotency(self):
        result = self.generate()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['sources'][0]['url'], SOURCE)
        self.assertEqual(result['draft']['hashtags'][0]['category'], 'trend')
        self.assertEqual(result['draft']['hashtags'][0]['observed_at'], result['sources'][0]['observed_at'])
        first, second = [json.loads(request.data) for request in self.transport.calls]
        self.assertEqual(first['max_tool_calls'], 1)
        parameters = first['tools'][0]['parameters']
        self.assertEqual(parameters['engine'], 'exa')
        self.assertEqual(parameters['max_uses'], 1)
        self.assertEqual(parameters['max_results'], 5)
        self.assertEqual(parameters['max_total_results'], 5)
        self.assertTrue(second['text']['format']['strict'])
        self.assertTrue(second['provider']['require_parameters'])
        for payload in (first, second):
            self.assertEqual(payload['model'], DEFAULT_MODEL)
            self.assertFalse(payload['store'])
            images = [p for p in payload['input'][0]['content'] if p['type'] == 'input_image']
            self.assertEqual([p['image_url'] for p in images], [FRAME] * 4)
        self.assertEqual(self.generate(), result)
        self.assertEqual(len(self.transport.calls), 2)
        self.assertNotIn(KEY, ''.join(p.read_text() for p in Path(self.tmp.name).glob('*.json')))

    def test_model_written_urls_or_no_executed_search_cannot_count_as_sources(self):
        self.transport.research['output'] = [message('I searched: #HandDance ' + SOURCE)]
        with self.assertRaises(OpenRouterCopyError) as caught: self.generate()
        self.assertEqual(caught.exception.code, 'search_unavailable')
        self.assertEqual(len(self.transport.calls), 1)

    def test_zero_search_usage_stops_before_draft(self):
        self.transport.research['usage']['server_tool_use_details']['web_search_requests'] = 0
        with self.assertRaises(OpenRouterCopyError) as caught: self.generate()
        self.assertEqual(caught.exception.code, 'search_unavailable')
        self.assertEqual(len(self.transport.calls), 1)

    def test_old_or_undated_today_prose_and_wrong_source_downgrade_trends(self):
        for index, excerpt in enumerate(('Published 2024-03-01. Share #HandDance today.',
                                         'A classic #HandDance tutorial. Try it today!',
                                         'Published ' + TODAY + '. A different tag, #OtherDance.')):
            with self.subTest(excerpt=excerpt), tempfile.TemporaryDirectory() as state:
                transport = FakeTransport(state)
                transport.research['output'][0]['content'][0]['annotations'][0]['url_citation']['content'] = excerpt
                result = self.generate(state_dir=state, request_token='old-' + str(index), request_fn=transport)
                tag = result['draft']['hashtags'][0]
                self.assertEqual(tag['category'], 'content')
                self.assertIsNone(tag['source_url'])
                self.assertIsNone(tag['observed_at'])
                self.assertIn('no current trend hashtag was verified', result['draft']['trend_note'])

    def test_misleading_note_and_fashiontok_brand_are_canonicalized_without_api(self):
        draft = {'captions': ['KAI style.', 'Second.', 'Third.'],
                 'hashtags': [dict(tag='#fashiontok', reason='A trend brand.', category='brand', source_url=None),
                              dict(tag='#KAI', reason='Character.', category='brand', source_url=None),
                              dict(tag='#idore', reason='Brand.', category='brand', source_url=None)],
                 'trend_note': 'Verified current trends from dated sources. Official real-time top tags. '
                               'No trend verified. No trend verified.'}
        result = finalize_draft(draft, {}, datetime.now(ZoneInfo('America/New_York')).date(), 'KAI')
        self.assertEqual(result['hashtags'][0]['category'], 'content')
        self.assertEqual(result['hashtags'][1]['category'], 'brand')
        self.assertEqual(result['hashtags'][2]['category'], 'brand')
        self.assertEqual(draft['hashtags'][0]['category'], 'brand')
        self.assertIn('no current trend hashtag was verified', result['trend_note'])
        self.assertNotIn('Official real-time top tags', result['trend_note'])
        self.assertEqual(result['trend_note'].count('no current trend hashtag was verified'), 1)
        self.assertIn('no audio was analyzed', result['trend_note'].lower())
        self.assertEqual(finalize_draft(result, {}, datetime.now(ZoneInfo('America/New_York')).date(), 'KAI'), result)
        self.assertEqual(self.transport.calls, [])

    def test_retained_trend_uses_bounded_honest_note_instead_of_model_claim(self):
        self.transport.draft['output'][0]['content'][0]['text'] = self.transport.draft['output'][0]['content'][0]['text'].replace(
            'A dated source suggestion, not an official TikTok ranking.', 'Official top viral ranking, guaranteed reach!')
        result = self.generate()
        self.assertEqual(result['draft']['hashtags'][0]['category'], 'trend')
        note = result['draft']['trend_note']
        self.assertLess(len(note), 500)
        self.assertNotIn('Official top viral ranking', note)
        self.assertIn('not an official or real-time TikTok', note)

    def test_adapter_downgrades_community_brand(self):
        draft = json.loads(self.transport.draft['output'][0]['content'][0]['text'])
        draft['hashtags'][1]['tag'] = '#fashiontok'
        self.transport.draft['output'][0]['content'][0]['text'] = json.dumps(draft)
        result = self.generate()
        self.assertEqual(result['draft']['hashtags'][1]['category'], 'content')
        self.assertIsNone(result['draft']['hashtags'][1]['source_url'])

    def test_timeout_is_uncertain_and_cannot_resubmit_after_restart(self):
        self.transport.failure = TimeoutError('private provider body ' + KEY)
        for _ in range(2):
            with self.assertRaises(OpenRouterCopyError) as caught: self.generate()
            self.assertEqual(caught.exception.code, 'submission_uncertain')
            self.assertNotIn(KEY, str(caught.exception))
        self.assertEqual(len(self.transport.calls), 1)

    def test_changed_input_same_token_rejected_without_another_post(self):
        self.generate()
        with self.assertRaises(OpenRouterCopyError) as caught: self.generate(title='Changed content')
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(self.transport.calls), 2)

    def test_errors_are_fixed_and_never_retry(self):
        for status, code in ((401, 'auth'), (402, 'insufficient_quota'), (429, 'rate_limited'),
                             (302, 'submission_uncertain'), (503, 'submission_uncertain')):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as state:
                transport = FakeTransport(state); transport.failure = status
                for _ in range(2):
                    with self.assertRaises(OpenRouterCopyError) as caught:
                        self.generate(state_dir=state, request_fn=transport)
                    self.assertEqual(caught.exception.code, code)
                    self.assertNotIn(KEY, str(caught.exception))
                self.assertEqual(len(transport.calls), 1)

    def test_invalid_inputs_never_call_provider(self):
        for overrides in (dict(key=''), dict(model='anthropic/claude'), dict(frames=[FRAME]),
                          dict(frames=['https://example.com/ref.jpg'] * 4), dict(title=KEY)):
            with self.subTest(overrides=list(overrides)):
                with self.assertRaises(OpenRouterCopyError): self.generate(**overrides)
        self.assertEqual(self.transport.calls, [])

    def test_transport_rejects_foreign_endpoint_and_redirect(self):
        with self.assertRaises(OpenRouterCopyError):
            _transport(Request('https://evil.example/responses', data=b'{}'), timeout=1, max_bytes=10)
        redirect = HTTPError(ENDPOINT, 302, 'Redirect', {'Location': 'https://evil.example/'}, io.BytesIO(b'private'))
        with patch('scripts.openrouter_copy.build_opener') as opener:
            opener.return_value.open.side_effect = redirect
            status, _, _ = _transport(Request(ENDPOINT, data=b'{}'), timeout=1, max_bytes=100)
        self.assertEqual(status, 302)
        self.assertEqual(opener.return_value.open.call_count, 1)


if __name__ == '__main__': unittest.main()
