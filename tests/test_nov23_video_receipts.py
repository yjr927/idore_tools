import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from scripts import nov23_video_receipts as n
from scripts.observed_dance import BEATS

KEY = 'fictional-test-key'
ID = 'gen-vid-1795482000-0123456789abcdefghij'
ROOT = 'https://raw.githubusercontent.com/yjr927/idore_tools/58853770e09acaa4d0a7ffd92eb68c21590365ef/site/characters/'
FRAMES = {
    '2026-11-23-bay': ROOT + 'Jace/Jace%E6%95%B4%E4%BD%93.png',
    '2026-11-23-momoko': ROOT + 'Danny%2B%E8%84%86%E8%84%86%E5%AE%9D%20%E5%90%8C%E5%89%A7%E6%BC%94%E5%91%98%E5%AE%88%E5%88%99/%E4%BC%8A%E8%8A%99%E7%90%B3%E6%97%A5%E5%B8%B8.png'}


class Response(io.BytesIO):
    status = 202

    def geturl(self):
        return n.BASE + '/videos'


def inputs(slot):
    src, url, photo, _, _ = n.BINDINGS[slot]
    role = 'role-' + photo[:16]
    row = {'id': 'agent-' + slot, 'model': n.MODEL, 'sequence_slot': slot, 'motion_reference_method': 'observed_description',
           'motion_binding': [src, url, role, photo]}
    source = {'id': src, 'url': url, 'motion_summary': ' '.join(BEATS[src][0])}
    character = {'id': role, 'sha256': photo, 'image_url': FRAMES[slot]}
    return row, source, character


class NamedTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.records = []
        self.dir = Path(self.temp.name)
        key = patch.dict(os.environ, OPENROUTER_API_KEY=KEY)
        key.start()
        self.addCleanup(key.stop)
        self.opener = self.enter_patch(patch.object(n, 'build_opener')).return_value
        self.get = self.enter_patch(patch.object(n, '_transport', side_effect=self.models))

    def enter_patch(self, setting):
        result = setting.start()
        self.addCleanup(setting.stop)
        return result

    def models(self, request, **_):
        self.assertEqual(request.full_url, n.BASE + '/videos/models')
        self.assertIsNone(request.get_header('Authorization'))
        obj = {'data': [{'id': n.MODEL, 'supported_durations': [6], 'supported_resolutions': ['720p'],
                          'supported_frame_images': ['first_frame']}]}
        return 200, {}, json.dumps(obj).encode()

    def submit(self, slot='2026-11-23-momoko', args=None):
        return n.submit_named(slot, *(inputs(slot) if args is None else args), self.dir, self.records.append)

    def test_both_real_bindings_preserve_unknown_status_response_id_before_parse(self):
        for slot in FRAMES:
            self.opener.open.return_value = Response(json.dumps({'id': ID, 'status':'new-state', 'unknown':KEY}).encode())
            self.assertEqual(self.submit(slot), ID)
            request = self.opener.open.call_args.args[0]
            body = json.loads(request.data)
            self.assertEqual(body['frame_images'][0]['image_url']['url'], FRAMES[slot])
            self.assertNotIn('aspect_ratio', body)
            self.assertEqual((body['duration'], body['resolution'], body['model'], body['generate_audio']),
                             (6, '720p', n.MODEL, False))
            self.assertEqual(self.records[-1], {'paid_posts':1, 'video_post_http_status':202, 'remote_id':ID})
        self.assertEqual(self.opener.open.call_count, 2)

    def test_rejects_swapped_photo_and_preexisting_remote_before_post(self):
        args = inputs('2026-11-23-momoko')
        args[2]['image_url'] = FRAMES['2026-11-23-bay']
        with self.assertRaisesRegex(n.NamedVideoError, 'binding_mismatch'):
            self.submit(args=args)
        args = inputs('2026-11-23-momoko')
        args[0]['remote_id'] = ID
        with self.assertRaisesRegex(n.NamedVideoError, 'binding_mismatch'):
            self.submit(args=args)
        self.opener.open.assert_not_called()

    def test_non_success_status_and_structured_privacy_rejection(self):
        err = {'error': {'metadata': {'raw': json.dumps({'error': {'code': 'InputImageSensitiveContentDetected.PrivacyInformation'}})},
                         'message': KEY}, 'id':ID}
        self.opener.open.side_effect = HTTPError(n.BASE+'/videos',422,'unused',{},io.BytesIO(json.dumps(err).encode()))
        with self.assertRaisesRegex(n.NamedVideoError, 'input_image_privacy_rejected'):
            self.submit()
        self.assertEqual(self.records, [{'paid_posts':1, 'video_post_http_status':422,
                                        'video_post_reason':'input_image_privacy_rejected'}])
        self.opener.open.assert_called_once()

    def test_only_known_structured_parameters_and_codes_are_recorded(self):
        err = {"code":"unsupported_parameter", "param":"aspect_ratio", "message":"private input"}
        self.assertEqual(n.diagnostic_for(422, {"error": err}), {
            "video_post_reason":"unsupported_parameter", "video_post_param":"aspect_ratio"})
        error = {"code": "private_account_token", "param": "arbitrary_secret", "message":
                 "InputImageSensitiveContentDetected.PrivacyInformation"}
        self.assertEqual(n.diagnostic_for(403, {"error": error}), {"video_post_reason":"access_denied"})
        for code, expected in [("invalid_image_url", "invalid_image_url"), ("insufficient_credits", "payment_required"),
                               ("image_fetch_failed", "image_download_failed")]:
            self.assertEqual(n.diagnostic_for(400, {"error":{"metadata":{"raw":json.dumps({"error":{
                "code":code, "param":"frame_images.0.image_url.url"}})}}}), {
                "video_post_reason":expected, "video_post_param":"frame_images.0.image_url.url"})

    def test_cached_matching_id_is_recovered_without_claiming_a_new_http_receipt(self):
        self.opener.open.return_value = Response(json.dumps({"id":ID,"status":"pending"}).encode())
        self.assertEqual(self.submit(),ID)
        self.assertEqual(self.submit(),ID)
        self.opener.open.assert_called_once()
        self.assertEqual(self.records[-1], {"paid_posts":0,"remote_id":ID})

    def test_malformed_remote_or_only_generation_id_not_persisted(self):
        self.opener.open.return_value = Response(json.dumps({'generation_id':ID, 'id':'sk-private', 'status':'pending'}).encode())
        with self.assertRaisesRegex(n.NamedVideoError, 'submission_uncertain'):
            self.submit()
        self.assertEqual(self.records, [{'paid_posts':1, 'video_post_http_status':202, 'video_post_status':'pending'}])

    def test_connection_interruption_counts_the_attempt_without_inventing_http_status(self):
        self.opener.open.side_effect = URLError('unexpected-private-diagnostic')
        with self.assertRaisesRegex(n.NamedVideoError, 'submission_uncertain'):
            self.submit()
        self.assertEqual(self.records, [{'paid_posts':1}])
        self.opener.open.assert_called_once()

    def test_free_preflight_failure_does_not_count_as_post(self):
        self.get.side_effect = URLError('test failure')
        with self.assertRaisesRegex(n.NamedVideoError, 'video_preflight_failed'):
            self.submit()
        self.assertEqual(self.records, [{'paid_posts':0}])
        self.opener.open.assert_not_called()

    def test_download_recovers_known_id_using_only_its_get_urls(self):
        def get(request, **_):
            self.assertEqual(request.get_method(), 'GET')
            if request.full_url == n.BASE + '/videos/' + ID:
                return 200, {}, json.dumps({'id':ID, 'status':'completed'}).encode()
            self.assertEqual(request.full_url, n.BASE + '/videos/' + ID + '/content')
            boxes = b''.join((8+len(data)).to_bytes(4,'big') + kind + data
                            for kind, data in [(b'ftyp', b'isom\0\0\0\0isom'), (b'moov',b'xx'),(b'mdat',b'xx')])
            return 200, {'content-type':'video/mp4'}, boxes
        self.get.side_effect = get
        out = n.download_named('2026-11-23-bay', ID, self.dir)
        self.assertTrue(out.is_file())
        self.assertEqual(self.get.call_count,2)
        self.opener.open.assert_not_called()


if __name__ == '__main__':
    unittest.main()
