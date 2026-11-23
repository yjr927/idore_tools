import base64
import json
import unittest
from unittest.mock import Mock, patch

from scripts import run_photo_generation as photos


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGOwLZgJAAI0AUco0HPZAAAAAElFTkSuQmCC')
ENCODED_PNG = base64.b64encode(PNG).decode()
DOCUMENT = {'images': [{'mime': 'image/png', 'data': ENCODED_PNG}], 'prompt': '保留参考图的构图，使用日光。'}
DOCUMENT_BYTES = photos.dump(DOCUMENT)
OUTPUT = photos.dump({'data': [{'b64_json': ENCODED_PNG, 'media_type': 'image/png'}]})


class PhotoTests(unittest.TestCase):
    def test_photo_reference_and_returned_bytes(self):
        payload = photos.image_request(DOCUMENT_BYTES)
        self.assertEqual(payload['prompt'], DOCUMENT['prompt'])
        self.assertEqual(payload['model'], 'bytedance-seed/seedream-4.5')
        self.assertEqual(payload['n'], 1)
        self.assertEqual(payload['input_references'][0]['image_url']['url'], 'data:image/png;base64,' + ENCODED_PNG)
        with patch.object(photos, 'request', return_value=OUTPUT) as send:
            self.assertEqual(photos.generate(payload, 'test-key'), (PNG, 'image/png'))
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[:3], (photos.OPENROUTER, 'test-key', 'POST'))

    def test_invalid_input_never_becomes_provider_request(self):
        for patch_input in [
            {'images': []}, {'images': DOCUMENT['images'] * 5},
            {'images': [{'mime': 'image/jpeg', 'data': ENCODED_PNG}]},
            {'images': [{'mime': 'image/png', 'data': 'https://invalid.example/image.png'}]},
            {'images': [{'mime': 'image/svg+xml', 'data': ENCODED_PNG}]},
            {'prompt': '  '}, {'prompt': 'x' * 4001},
        ]:
            with self.subTest(patch_input=patch_input), self.assertRaisesRegex(photos.PhotoError, 'invalid_input'):
                photos.image_request(photos.dump(dict(DOCUMENT, **patch_input)))

    def test_reject_wrong_format_or_external_result(self):
        for response in [{'data': [{'b64_json': ENCODED_PNG, 'media_type': 'image/webp'}]},
                         {'data': [{'url': 'https://invalid.example/private'}]},
                         {'data': []}, {'data': [{'b64_json': 'AAAA'}]}]:
            with self.subTest(response=response), patch.object(photos, 'request', return_value=photos.dump(response)):
                with self.assertRaisesRegex(photos.PhotoError, 'invalid_result'):
                    photos.generate({}, 'test-key')

    def test_browser_upload_waits_and_delivery_reuses_the_same_paid_result(self):
        job = Mock()
        job.call.side_effect = [b'', photos.TransportError(409), DOCUMENT_BYTES, b'', photos.TransportError(503), b'']
        with patch.object(photos.time, 'sleep'), patch.object(photos, 'generate', return_value=(PNG, 'image/png')) as bill:
            photos.run(job, 'test-key')
        self.assertEqual([c.args[0] for c in job.call.call_args_list], ['init', 'input', 'input', 'claim', 'result', 'result'])
        self.assertEqual(job.call.call_args_list[-1], job.call.call_args_list[-2])
        bill.assert_called_once()

    def test_refused_claim_or_missing_key_never_generates(self):
        for key in ('', 'test-key'):
            job = Mock()
            job.call.side_effect = [b'', DOCUMENT_BYTES, photos.TransportError(409)]
            with patch.object(photos, 'generate') as bill, self.assertRaises(photos.PhotoError):
                photos.run(job, key)
            bill.assert_not_called()
            ops = [c.args[0] for c in job.call.call_args_list]
            self.assertEqual(ops, ['init', 'input', 'claim'] if key else ['init', 'fail'])

    def test_unknown_paid_response_is_reported_once_without_returning_upstream_details(self):
        job = Mock()
        job.call.side_effect = [b'', DOCUMENT_BYTES, b'', b'']
        with patch.object(photos, 'request', side_effect=photos.TransportError(504)) as send:
            with self.assertRaisesRegex(photos.PhotoError, 'submission_uncertain'):
                photos.run(job, 'test-key')
            send.assert_called_once()
        self.assertEqual(job.call.call_args.args, ('fail', 'POST', photos.dump({'code': 'submission_uncertain'}), 'application/json'))

    def test_private_job_id_and_fixed_auth_destinations(self):
        for bad in ['A' * 64, 'https://invalid.example', '', '0' * 65]:
            with self.assertRaises(photos.PhotoError):
                photos.PrivateJob(bad)
        job = photos.PrivateJob('f' * 64)
        with patch.dict(photos.os.environ, {'ACTIONS_ID_TOKEN_REQUEST_URL': 'https://invalid.example/'}), patch.object(photos, 'request') as send:
            with self.assertRaises(photos.PhotoError):
                job.call('init')
            send.assert_not_called()
        env = {'ACTIONS_ID_TOKEN_REQUEST_URL': 'https://pipelines.actions.githubusercontent.com/example?api-version=2',
               'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'test-runner-ticket'}
        calls = []
        def answer(url, token, *args, **kwargs):
            calls.append((url, token))
            return b'{"value":"header.claims.signature"}' if len(calls) == 1 else b'{}'
        with patch.dict(photos.os.environ, env), patch.object(photos, 'request', side_effect=answer):
            job.call('init')
            job.call('input', 'GET', None)
        self.assertEqual(len(calls), 3)
        self.assertIn('audience=idore-photo%3A' + 'f' * 64, calls[0][0])
        self.assertEqual(calls[1], (photos.WORKER + 'f' * 64 + '/init', 'header.claims.signature'))
        self.assertEqual(calls[2][0], photos.WORKER + 'f' * 64 + '/input')


if __name__ == '__main__':
    unittest.main()
